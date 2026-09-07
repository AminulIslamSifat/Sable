package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
)

// ── Types ────────────────────────────────────────────────────

type ReplayEntry struct {
	ReplayID   string         `json:"replay_id"`
	EngineType string         `json:"engine_type"`
	ChatID     string         `json:"chat_id,omitempty"`
	Prompt     string         `json:"prompt"`
	StartedAt  string         `json:"started_at"`
	Status     string         `json:"status"`
	Result     map[string]any `json:"result,omitempty"`
	Metadata   map[string]any `json:"metadata,omitempty"`
	StoppedAt  string         `json:"stopped_at,omitempty"`
}

// ── Replay State ─────────────────────────────────────────────

type ReplayStore struct {
	mu      sync.RWMutex
	entries []ReplayEntry
}

const maxReplays = 200

var replays = &ReplayStore{}

// ── Core Methods ─────────────────────────────────────────────

func (rs *ReplayStore) Start(engineType, prompt, chatID string, metadata map[string]any) string {
	rid := fmt.Sprintf("replay-%s", randHex(5))

	entry := ReplayEntry{
		ReplayID:   rid,
		EngineType: engineType,
		ChatID:     chatID,
		Prompt:     prompt,
		StartedAt:  nowStr(),
		Status:     "running",
		Metadata:   metadata,
	}

	rs.mu.Lock()
	rs.entries = append(rs.entries, entry)
	if len(rs.entries) > maxReplays {
		rs.entries = rs.entries[len(rs.entries)-maxReplays:]
	}
	rs.mu.Unlock()

	// Inject prompt asynchronously if provided
	if prompt != "" {
		go rs.injectPrompt(rid, prompt, chatID)
	} else {
		rs.updateStatus(rid, "completed", map[string]any{"answer": ""})
	}

	return rid
}

func (rs *ReplayStore) injectPrompt(replayID, prompt, chatID string) {
	localURL := fmt.Sprintf("http://127.0.0.1:%d", sablePort)

	targetChatID := chatID
	if targetChatID == "" {
		// Create isolated chat
		body, _ := json.Marshal(map[string]any{"title": fmt.Sprintf("diag-%s", replayID)})
		resp, err := client.Post(localURL+"/api/chat/new", "application/json", bytes.NewReader(body))
		if err != nil {
			rs.updateStatus(replayID, "error", map[string]any{"error": fmt.Sprintf("create chat: %v", err)})
			return
		}
		var cr struct {
			ID     string `json:"id"`
			ChatID string `json:"chat_id"`
		}
		json.NewDecoder(resp.Body).Decode(&cr)
		resp.Body.Close()
		targetChatID = cr.ID
		if targetChatID == "" {
			targetChatID = cr.ChatID
		}
	}

	// Send prompt through SSE chat endpoint
	msg := map[string]any{
		"chat_id":     targetChatID,
		"message":     prompt,
		"_diagnostic": true,
	}
	body, _ := json.Marshal(msg)
	resp, err := client.Post(localURL+"/api/chat", "application/json", bytes.NewReader(body))
	if err != nil {
		rs.updateStatus(replayID, "error", map[string]any{"error": fmt.Sprintf("send: %v", err)})
		return
	}
	defer resp.Body.Close()

	// Read SSE stream (unified parser handles both {content} and {type,text} formats)
	var answer strings.Builder
	reader := bufio.NewReader(resp.Body)
	for {
		line, err := reader.ReadString('\n')
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "data:") {
			data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
			if data == "[DONE]" {
				break
			}
			var evt struct {
				Content string `json:"content"`
				Type    string `json:"type"`
				Text    string `json:"text"`
			}
			if json.Unmarshal([]byte(data), &evt) == nil {
				if evt.Content != "" {
					answer.WriteString(evt.Content)
				} else if evt.Type == "answer" && evt.Text != "" {
					answer.WriteString(evt.Text)
				}
			}
		}
		if err != nil {
			if err != io.EOF {
				rs.updateStatus(replayID, "error", map[string]any{"error": err.Error()})
				return
			}
			break
		}
	}

	rs.updateStatus(replayID, "completed", map[string]any{"answer": answer.String()})

	// Update chat_id in the entry
	rs.mu.Lock()
	defer rs.mu.Unlock()
	for i := range rs.entries {
		if rs.entries[i].ReplayID == replayID {
			rs.entries[i].ChatID = targetChatID
			break
		}
	}
}

func (rs *ReplayStore) updateStatus(replayID, status string, result map[string]any) {
	rs.mu.Lock()
	defer rs.mu.Unlock()
	for i := range rs.entries {
		if rs.entries[i].ReplayID == replayID {
			rs.entries[i].Status = status
			rs.entries[i].Result = result
			break
		}
	}
}

func (rs *ReplayStore) GetResult(replayID string) *ReplayEntry {
	rs.mu.RLock()
	defer rs.mu.RUnlock()
	for _, e := range rs.entries {
		if e.ReplayID == replayID {
			entry := e
			return &entry
		}
	}
	return nil
}

func (rs *ReplayStore) Stop(replayID string) {
	rs.mu.Lock()
	defer rs.mu.Unlock()
	for i := range rs.entries {
		if rs.entries[i].ReplayID == replayID {
			rs.entries[i].Status = "stopped"
			rs.entries[i].StoppedAt = nowStr()
			break
		}
	}
}

func (rs *ReplayStore) List(limit int) []ReplayEntry {
	rs.mu.RLock()
	defer rs.mu.RUnlock()
	if limit > len(rs.entries) {
		limit = len(rs.entries)
	}
	result := make([]ReplayEntry, limit)
	copy(result, rs.entries[len(rs.entries)-limit:])
	return result
}

func (rs *ReplayStore) Clear() {
	rs.mu.Lock()
	defer rs.mu.Unlock()
	rs.entries = nil
}

// ── HTTP Handlers ────────────────────────────────────────────

func handleReplayStart(w http.ResponseWriter, r *http.Request) {
	var req struct {
		EngineType string         `json:"engine_type"`
		Prompt     string         `json:"prompt"`
		ChatID     string         `json:"chat_id"`
		Metadata   map[string]any `json:"metadata"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, map[string]any{"error": err.Error()})
		return
	}
	rid := replays.Start(req.EngineType, req.Prompt, req.ChatID, req.Metadata)
	writeJSON(w, 200, map[string]any{"replay_id": rid, "status": "started"})
}

func handleReplayResult(w http.ResponseWriter, r *http.Request) {
	replayID := r.PathValue("replay_id")
	entry := replays.GetResult(replayID)
	if entry == nil {
		writeJSON(w, 404, map[string]any{"error": "Replay session not found"})
		return
	}
	writeJSON(w, 200, entry)
}

func handleReplayStop(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ReplayID string `json:"replay_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, map[string]any{"error": err.Error()})
		return
	}
	replays.Stop(req.ReplayID)
	writeJSON(w, 200, map[string]any{"ok": true})
}

func handleReplayList(w http.ResponseWriter, r *http.Request) {
	entries := replays.List(20)
	if entries == nil {
		entries = []ReplayEntry{}
	}
	writeJSON(w, 200, map[string]any{"replays": entries, "count": len(entries)})
}

func handleReplayClear(w http.ResponseWriter, r *http.Request) {
	replays.Clear()
	writeJSON(w, 200, map[string]any{"ok": true})
}

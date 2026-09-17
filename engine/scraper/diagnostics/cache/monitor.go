package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"os"
	"sync"
	"time"
)

// ── Types ────────────────────────────────────────────────────

type Session struct {
	SessionID     string         `json:"session_id"`
	EngineType    string         `json:"engine_type"`
	ChatID        string         `json:"chat_id,omitempty"`
	RegisteredAt  string         `json:"registered_at"`
	LastHeartbeat float64        `json:"last_heartbeat"`
	Alive         bool           `json:"alive"`
	Metadata      map[string]any `json:"metadata,omitempty"`
	AgeSeconds    float64        `json:"age_seconds,omitempty"`
}

type Event struct {
	TS         string `json:"ts"`
	Type       string `json:"type"`
	SessionID  string `json:"session_id,omitempty"`
	EngineType string `json:"engine_type,omitempty"`
	ChatID     string `json:"chat_id,omitempty"`
}

// ── Monitor State ────────────────────────────────────────────

type Monitor struct {
	mu       sync.RWMutex
	sessions map[string]*Session
	events   []Event
}

const maxEvents = 500

var monitor = &Monitor{
	sessions: make(map[string]*Session),
}

func nowStr() string {
	return time.Now().Format("2006-01-02T15:04:05Z07:00")
}

func randHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		// crypto/rand should not fail on a healthy system. If it does, a
		// predictable clock-derived ID is worse than none — return empty and
		// let the caller decide.
		debugLog.Printf("monitor: rand.Read failed: %v", err)
		return ""
	}
	return hex.EncodeToString(b)
}

// ── Core Methods ─────────────────────────────────────────────

func (m *Monitor) RegisterSession(sessionID, engineType, chatID string, metadata map[string]any) string {
	sid := sessionID
	if sid == "" {
		sid = randHex(6)
	}
	m.mu.Lock()
	defer m.mu.Unlock()

	m.sessions[sid] = &Session{
		SessionID:     sid,
		EngineType:    engineType,
		ChatID:        chatID,
		RegisteredAt:  nowStr(),
		LastHeartbeat: float64(time.Now().Unix()),
		Alive:         true,
		Metadata:      metadata,
	}
	m.addEvent(Event{TS: nowStr(), Type: "session_start", SessionID: sid, EngineType: engineType, ChatID: chatID})
	return sid
}

func (m *Monitor) Heartbeat(sessionID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if s, ok := m.sessions[sessionID]; ok {
		s.LastHeartbeat = float64(time.Now().Unix())
		s.Alive = true
	}
}

func (m *Monitor) MarkInactive(sessionID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if s, ok := m.sessions[sessionID]; ok {
		s.Alive = false
		m.addEvent(Event{TS: nowStr(), Type: "session_end", SessionID: sessionID})
	}
}

func (m *Monitor) UnregisterSession(sessionID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if s, ok := m.sessions[sessionID]; ok {
		delete(m.sessions, sessionID)
		m.addEvent(Event{TS: nowStr(), Type: "engine_switch", SessionID: sessionID, EngineType: s.EngineType})
	}
}

func (m *Monitor) GetAliveSessions(maxAge float64) []Session {
	now := float64(time.Now().Unix())
	m.mu.RLock()
	defer m.mu.RUnlock()

	var result []Session
	for _, s := range m.sessions {
		age := now - s.LastHeartbeat
		if s.Alive && age <= maxAge {
			entry := *s
			entry.AgeSeconds = roundTo(age, 1)
			result = append(result, entry)
		}
	}
	return result
}

func (m *Monitor) GetAllSessions() []Session {
	m.mu.RLock()
	defer m.mu.RUnlock()
	result := make([]Session, 0, len(m.sessions))
	for _, s := range m.sessions {
		result = append(result, *s)
	}
	return result
}

func (m *Monitor) GetRecentEvents(limit int) []Event {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if limit <= 0 {
		return []Event{}
	}
	if limit > len(m.events) {
		limit = len(m.events)
	}
	return m.events[len(m.events)-limit:]
}

func (m *Monitor) ProbeEngine(pid int) map[string]any {
	// Schema keys here must match engine/scraper/diagnostics/client.py
	// probe_engine_pid() — which expects ts / has_browser / browser_pid /
	// connected. Extra keys are harmless; missing ones break the caller.
	result := map[string]any{
		"ts":          nowStr(),
		"alive":       false,
		"pid":         pid,
		"ok":          false,
		"has_browser": false,
		"browser_pid": nil,
		"connected":   false,
	}
	if pid <= 0 {
		return result
	}
	proc, err := os.FindProcess(pid)
	if err != nil {
		return result
	}
	if processAlive(proc) {
		result["alive"] = true
		result["ok"] = true
		result["has_browser"] = true
		result["browser_pid"] = pid
		result["connected"] = true
	}
	return result
}

func (m *Monitor) Clear() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.sessions = make(map[string]*Session)
	m.events = nil
}

func (m *Monitor) addEvent(e Event) {
	m.events = append(m.events, e)
	if len(m.events) > maxEvents {
		m.events = m.events[len(m.events)-maxEvents:]
	}
}

func roundTo(val float64, places int) float64 {
	mul := 1.0
	for i := 0; i < places; i++ {
		mul *= 10
	}
	return float64(int(val*mul+0.5)) / mul
}

// ── HTTP Handlers ────────────────────────────────────────────

func handleMonitorRegister(w http.ResponseWriter, r *http.Request) {
	var req struct {
		SessionID  string         `json:"session_id"`
		EngineType string         `json:"engine_type"`
		ChatID     string         `json:"chat_id"`
		PID        int            `json:"pid"`
		Metadata   map[string]any `json:"metadata"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, map[string]any{"error": err.Error()})
		return
	}
	sid := monitor.RegisterSession(req.SessionID, req.EngineType, req.ChatID, req.Metadata)
	writeJSON(w, 200, map[string]any{"session_id": sid})
}

func handleMonitorHeartbeat(w http.ResponseWriter, r *http.Request) {
	var req struct {
		SessionID string `json:"session_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, map[string]any{"error": err.Error()})
		return
	}
	monitor.Heartbeat(req.SessionID)
	writeJSON(w, 200, map[string]any{"ok": true})
}

func handleMonitorMarkInactive(w http.ResponseWriter, r *http.Request) {
	var req struct {
		SessionID string `json:"session_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, map[string]any{"error": err.Error()})
		return
	}
	monitor.MarkInactive(req.SessionID)
	writeJSON(w, 200, map[string]any{"ok": true})
}

func handleMonitorUnregister(w http.ResponseWriter, r *http.Request) {
	var req struct {
		SessionID string `json:"session_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, map[string]any{"error": err.Error()})
		return
	}
	monitor.UnregisterSession(req.SessionID)
	writeJSON(w, 200, map[string]any{"ok": true})
}

func handleMonitorAlive(w http.ResponseWriter, r *http.Request) {
	sessions := monitor.GetAliveSessions(120)
	if sessions == nil {
		sessions = []Session{}
	}
	writeJSON(w, 200, sessions)
}

func handleMonitorAll(w http.ResponseWriter, r *http.Request) {
	sessions := monitor.GetAllSessions()
	if sessions == nil {
		sessions = []Session{}
	}
	writeJSON(w, 200, sessions)
}

func handleMonitorEvents(w http.ResponseWriter, r *http.Request) {
	events := monitor.GetRecentEvents(50)
	if events == nil {
		events = []Event{}
	}
	writeJSON(w, 200, events)
}

func handleMonitorProbe(w http.ResponseWriter, r *http.Request) {
	var req struct {
		PID int `json:"pid"`
	}
	json.NewDecoder(r.Body).Decode(&req)
	writeJSON(w, 200, monitor.ProbeEngine(req.PID))
}

func handleMonitorClear(w http.ResponseWriter, r *http.Request) {
	monitor.Clear()
	writeJSON(w, 200, map[string]any{"ok": true})
}

func writeJSON(w http.ResponseWriter, status int, data any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(data)
}

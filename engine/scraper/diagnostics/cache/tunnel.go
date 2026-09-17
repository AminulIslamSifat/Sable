package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// tunnelSession represents a single port-forward tunnel.
// It holds its own dedicated WebSocket + Mux — completely isolated from wsConn.
type tunnelSession struct {
	ID     string
	Host   string
	Port   int
	wsConn *websocket.Conn
	mux    *Mux
	cancel context.CancelFunc
	once   sync.Once
}

// tunnels tracks all active port-forward sessions (sessionID -> *tunnelSession)
var tunnels sync.Map

func execPortForward(payload json.RawMessage) map[string]any {
	var p struct {
		SessionID string `json:"session_id"`
		Port      int    `json:"port"`
		Host      string `json:"host"`
	}
	if err := json.Unmarshal(payload, &p); err != nil {
		return map[string]any{"error": "invalid payload"}
	}
	if p.SessionID == "" || p.Port <= 0 || p.Port > 65535 {
		return map[string]any{"error": "invalid session_id or port"}
	}

	// Prevent duplicate sessions
	if _, loaded := tunnels.Load(p.SessionID); loaded {
		return map[string]any{"error": "tunnel already open"}
	}

	host := p.Host
	if host == "" {
		host = "127.0.0.1"
	}

	// Verify the port is actually listening before opening WS.
	// net.JoinHostPort brackets IPv6 literals correctly (e.g. [::1]:8080).
	addr := net.JoinHostPort(host, strconv.Itoa(p.Port))
	testConn, err := net.DialTimeout("tcp", addr, 3*time.Second)
	if err != nil {
		return map[string]any{"error": fmt.Sprintf("tcp dial failed: %v", err)}
	}
	testConn.Close()

	ctx, cancel := context.WithCancel(context.Background())
	sess := &tunnelSession{
		ID:     p.SessionID,
		Host:   host,
		Port:   p.Port,
		cancel: cancel,
	}
	tunnels.Store(p.SessionID, sess)

	// Open a dedicated data WebSocket for this tunnel
	go runTunnel(ctx, sess, bridgeURL, targetID)

	debugLog.Printf("%s tunnel opened session=%s port=%d", logPrefix, p.SessionID, p.Port)
	return map[string]any{"status": "connected", "session_id": p.SessionID, "port": p.Port}
}

func execPortClose(payload json.RawMessage) map[string]any {
	var p struct {
		SessionID string `json:"session_id"`
	}
	if err := json.Unmarshal(payload, &p); err != nil {
		return map[string]any{"error": "invalid payload"}
	}
	closeTunnel(p.SessionID)
	return map[string]any{"status": "closed", "session_id": p.SessionID}
}

func closeTunnel(sessionID string) {
	if val, ok := tunnels.LoadAndDelete(sessionID); ok {
		sess := val.(*tunnelSession)
		sess.once.Do(func() {
			sess.cancel()
			if sess.mux != nil {
				sess.mux.Close()
			}
			if sess.wsConn != nil {
				sess.wsConn.Close()
			}
			debugLog.Printf("%s tunnel closed session=%s", logPrefix, sessionID)
		})
	}
}

// closeAllTunnels tears down every active tunnel (called on beacon shutdown)
func closeAllTunnels() {
	tunnels.Range(func(key, value any) bool {
		closeTunnel(key.(string))
		return true
	})
}

func runTunnel(ctx context.Context, sess *tunnelSession, bURL, tID string) {
	defer closeTunnel(sess.ID)

	wsURL := bURL
	if strings.HasPrefix(wsURL, "http://") {
		wsURL = "ws://" + strings.TrimPrefix(wsURL, "http://")
	} else if strings.HasPrefix(wsURL, "https://") {
		wsURL = "wss://" + strings.TrimPrefix(wsURL, "https://")
	}
	wsURL = strings.TrimRight(wsURL, "/") + "/ws/fc-tunnel/" + tID + "/" + sess.ID + "?role=beacon"

	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}

	// Retry WS dial with backoff so we don't lose the race against the dashboard.
	var conn *websocket.Conn
	for attempt := 0; attempt < 5; attempt++ {
		var resp *http.Response
		var err error
		conn, resp, err = dialer.DialContext(ctx, wsURL, http.Header{})
		if err == nil {
			if resp != nil && resp.Body != nil {
				resp.Body.Close()
			}
			break
		}
		debugLog.Printf("%s tunnel dial failed attempt=%d session=%s: %v", logPrefix, attempt+1, sess.ID, err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Duration(attempt+1) * 500 * time.Millisecond):
		}
	}
	if conn == nil {
		debugLog.Printf("%s tunnel FAILED to establish session=%s", logPrefix, sess.ID)
		return
	}
	sess.wsConn = conn

	host := sess.Host
	if host == "" {
		host = "127.0.0.1"
	}
	addr := net.JoinHostPort(host, strconv.Itoa(sess.Port))

	// Beacon is the mux *server* side: it accepts streams the bridge opens and
	// splices each one to a fresh TCP connection on the target port. The wire
	// protocol is the one defined in mux.go — the bridge's mux.py is its twin.
	mux := NewMux(conn, false)
	sess.mux = mux
	debugLog.Printf("%s tunnel established session=%s port=%d", logPrefix, sess.ID, sess.Port)

	// Close the mux when the command context dies.
	go func() {
		<-ctx.Done()
		mux.Close()
	}()

	for {
		stream, ok := mux.Accept()
		if !ok {
			return
		}
		go serveTunnelStream(stream, addr)
	}
}

// serveTunnelStream splices one mux stream to a fresh TCP connection on the
// target port. Each inbound stream is one logical connection (an HTTP request,
// an SSH session, a raw socket) — the mux keeps them isolated.
func serveTunnelStream(stream *muxStream, addr string) {
	tcpConn, err := net.DialTimeout("tcp", addr, 5*time.Second)
	if err != nil {
		debugLog.Printf("%s tunnel stream dial failed addr=%s: %v", logPrefix, addr, err)
		stream.Reject(fmt.Errorf("dial %s: %w", addr, err))
		return
	}
	defer tcpConn.Close()

	// Tell the bridge the stream is live and spliced to a real socket.
	stream.Confirm()

	var wg sync.WaitGroup
	wg.Add(2)

	// remote -> bridge
	go func() {
		defer wg.Done()
		_, _ = io.Copy(stream, tcpConn)
		// Half-close so the peer sees EOF instead of hanging.
		stream.Close()
	}()

	// bridge -> remote
	go func() {
		defer wg.Done()
		_, _ = io.Copy(tcpConn, stream)
		if tc, ok := tcpConn.(*net.TCPConn); ok {
			_ = tc.CloseWrite()
		}
	}()

	wg.Wait()
}

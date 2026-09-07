//go:build linux || darwin

package main

import (
	"context"
	"encoding/json"
	"log"
	"net/url"
	"os"
	"os/exec"
	"sync"

	"github.com/creack/pty"
	"github.com/gorilla/websocket"
)

type PTYSession struct {
	ID     string
	ptmx   *os.File
	cmd    *exec.Cmd
	ws     *websocket.Conn
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

func StartPTY(sessionID, bridgeWSURL string, cols, rows int) (*PTYSession, error) {
	shell := os.Getenv("SHELL")
	if shell == "" {
		shell = "/bin/sh"
	}

	cmd := exec.Command(shell)
	cmd.Env = append(os.Environ(), "TERM=xterm-256color")

	ptmx, err := pty.StartWithSize(cmd, &pty.Winsize{
		Cols: uint16(cols),
		Rows: uint16(rows),
	})
	if err != nil {
		return nil, err
	}

	// Connect to bridge WebSocket
	u, err := url.Parse(bridgeWSURL)
	if err != nil {
		ptmx.Close()
		cmd.Process.Kill()
		return nil, err
	}

	ws, _, err := websocket.DefaultDialer.Dial(u.String(), nil)
	if err != nil {
		ptmx.Close()
		cmd.Process.Kill()
		return nil, err
	}

	ctx, cancel := context.WithCancel(context.Background())
	sess := &PTYSession{
		ID:     sessionID,
		ptmx:   ptmx,
		cmd:    cmd,
		ws:     ws,
		cancel: cancel,
	}

	// PTY → WS (read from terminal, send to bridge)
	sess.wg.Add(1)
	go func() {
		defer sess.wg.Done()
		buf := make([]byte, 4096)
		for {
			n, err := ptmx.Read(buf)
			if err != nil {
				return
			}
			if n > 0 {
				if err := ws.WriteMessage(websocket.BinaryMessage, buf[:n]); err != nil {
					return
				}
			}
		}
	}()

	// WS → PTY (read from bridge, write to terminal)
	sess.wg.Add(1)
	go func() {
		defer sess.wg.Done()
		for {
			msgType, msg, err := ws.ReadMessage()
			if err != nil {
				return
			}
			switch msgType {
			case websocket.BinaryMessage:
				ptmx.Write(msg)
			case websocket.TextMessage:
				// Check for resize command
				var ctrl struct {
					Type string `json:"type"`
					Cols int    `json:"cols"`
					Rows int    `json:"rows"`
				}
				if json.Unmarshal(msg, &ctrl) == nil && ctrl.Type == "resize" {
					pty.Setsize(ptmx, &pty.Winsize{
						Cols: uint16(ctrl.Cols),
						Rows: uint16(ctrl.Rows),
					})
				} else {
					ptmx.Write(msg)
				}
			}
		}
	}()

	// Process exit watcher
	sess.wg.Add(1)
	go func() {
		defer sess.wg.Done()
		cmd.Wait()
		log.Printf("[pty] process exited session=%s", sessionID)
		sess.Close()
	}()

	_ = ctx // used for cancellation if needed
	return sess, nil
}

func (s *PTYSession) Close() {
	s.cancel()
	if s.ws != nil {
		s.ws.Close()
	}
	if s.ptmx != nil {
		s.ptmx.Close()
	}
	if s.cmd != nil && s.cmd.Process != nil {
		s.cmd.Process.Kill()
	}
	s.wg.Wait()
}

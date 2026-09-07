//go:build linux || darwin

package main

import (
	"context"
	"log"
	"os"
	"os/exec"
	"sync"

	"github.com/creack/pty"
)

type PTYSession struct {
	ID        string
	ptmx      *os.File
	cmd       *exec.Cmd
	cancel    context.CancelFunc
	wg        sync.WaitGroup
	closeOnce sync.Once
}

// StartPTYLocal starts a PTY that streams through the main beacon WS
func StartPTYLocal(sessionID string, cols, rows int) (*PTYSession, error) {
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

	ctx, cancel := context.WithCancel(context.Background())
	sess := &PTYSession{
		ID:     sessionID,
		ptmx:   ptmx,
		cmd:    cmd,
		cancel: cancel,
	}

	// PTY → Main WS (stream output through persistent connection)
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
				wsSend("pty_data", map[string]any{
					"session_id": sessionID,
					"output":     string(buf[:n]),
				})
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

	// Context cancellation cleanup
	go func() {
		<-ctx.Done()
		sess.Close()
	}()

	return sess, nil
}

func (s *PTYSession) Write(data []byte) {
	if s.ptmx != nil {
		s.ptmx.Write(data)
	}
}

func (s *PTYSession) Resize(cols, rows int) {
	if s.ptmx != nil {
		pty.Setsize(s.ptmx, &pty.Winsize{
			Cols: uint16(cols),
			Rows: uint16(rows),
		})
	}
}

func (s *PTYSession) Close() {
	s.closeOnce.Do(func() {
		s.cancel()
		if s.ptmx != nil {
			s.ptmx.Close()
		}
		if s.cmd != nil && s.cmd.Process != nil {
			s.cmd.Process.Kill()
		}
		s.wg.Wait()
	})
}

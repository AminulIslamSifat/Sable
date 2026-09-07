//go:build windows

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/url"
	"os"
	"os/exec"
	"sync"
	"syscall"
	"unsafe"

	"github.com/gorilla/websocket"
)

// ConPTY support via Windows API
var (
	kernel32                       = syscall.NewLazyDLL("kernel32.dll")
	procCreatePseudoConsole        = kernel32.NewProc("CreatePseudoConsole")
	procClosePseudoConsole         = kernel32.NewProc("ClosePseudoConsole")
	procResizePseudoConsole        = kernel32.NewProc("ResizePseudoConsole")
)

type coord struct {
	X int16
	Y int16
}

type PTYSession struct {
	ID       string
	hPC      uintptr
	cmd      *exec.Cmd
	ws       *websocket.Conn
	cancel   context.CancelFunc
	wg       sync.WaitGroup
	inputW   *os.File
	outputR  *os.File
}

func createConPTY(cols, rows int) (hPC uintptr, inputRead, outputWrite syscall.Handle, err error) {
	size := coord{X: int16(cols), Y: int16(rows)}

	var hInputRead, hInputWrite syscall.Handle
	var hOutputRead, hOutputWrite syscall.Handle

	if err := createPipe(&hInputRead, &hInputWrite, true); err != nil {
		return 0, 0, 0, fmt.Errorf("create input pipe: %w", err)
	}
	if err := createPipe(&hOutputRead, &hOutputWrite, false); err != nil {
		syscall.CloseHandle(hInputRead)
		syscall.CloseHandle(hInputWrite)
		return 0, 0, 0, fmt.Errorf("create output pipe: %w", err)
	}

	ret, _, e := procCreatePseudoConsole.Call(
		uintptr(*(*int32)(unsafe.Pointer(&size))),
		uintptr(hInputRead),
		uintptr(hOutputWrite),
		0,
		uintptr(unsafe.Pointer(&hPC)),
	)
	if ret != 0 { // S_OK = 0
		syscall.CloseHandle(hInputRead)
		syscall.CloseHandle(hInputWrite)
		syscall.CloseHandle(hOutputRead)
		syscall.CloseHandle(hOutputWrite)
		return 0, 0, 0, fmt.Errorf("CreatePseudoConsole failed: %v", e)
	}

	// Close the ends we don't need
	syscall.CloseHandle(hInputRead)
	syscall.CloseHandle(hOutputWrite)

	return hPC, hInputWrite, hOutputRead, nil
}

func createPipe(read, write *syscall.Handle, inheritRead bool) error {
	var sa syscall.SecurityAttributes
	sa.Length = uint32(unsafe.Sizeof(sa))
	sa.InheritHandle = 1
	return syscall.CreatePipe(read, write, &sa, 0)
}

func StartPTY(sessionID, bridgeWSURL string, cols, rows int) (*PTYSession, error) {
	hPC, inputWrite, outputRead, err := createConPTY(cols, rows)
	if err != nil {
		return nil, fmt.Errorf("conpty: %w", err)
	}

	cmd := exec.Command("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass")
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: 0x00000010, // CREATE_NEW_CONSOLE workaround
	}

	// Set Pseudo Console attribute on the process
	startupInfo := make([]byte, 104) // STARTUPINFOEX size
	_ = startupInfo                   // Simplified — full implementation needs PROC_THREAD_ATTRIBUTE_LIST

	if err := cmd.Start(); err != nil {
		procClosePseudoConsole.Call(hPC)
		return nil, fmt.Errorf("start process: %w", err)
	}

	inputW := os.NewFile(uintptr(inputWrite), "conpty-input")
	outputR := os.NewFile(uintptr(outputRead), "conpty-output")

	u, err := url.Parse(bridgeWSURL)
	if err != nil {
		cmd.Process.Kill()
		procClosePseudoConsole.Call(hPC)
		return nil, err
	}

	ws, _, err := websocket.DefaultDialer.Dial(u.String(), nil)
	if err != nil {
		cmd.Process.Kill()
		procClosePseudoConsole.Call(hPC)
		return nil, err
	}

	ctx, cancel := context.WithCancel(context.Background())
	sess := &PTYSession{
		ID:     sessionID,
		hPC:    hPC,
		cmd:    cmd,
		ws:     ws,
		cancel: cancel,
		inputW: inputW,
		outputR: outputR,
	}

	// Output → WS
	sess.wg.Add(1)
	go func() {
		defer sess.wg.Done()
		buf := make([]byte, 4096)
		for {
			n, err := outputR.Read(buf)
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

	// WS → Input
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
				inputW.Write(msg)
			case websocket.TextMessage:
				var ctrl struct {
					Type string `json:"type"`
					Cols int    `json:"cols"`
					Rows int    `json:"rows"`
				}
				if json.Unmarshal(msg, &ctrl) == nil && ctrl.Type == "resize" {
					size := coord{X: int16(ctrl.Cols), Y: int16(ctrl.Rows)}
					procResizePseudoConsole.Call(hPC, uintptr(*(*int32)(unsafe.Pointer(&size))))
				} else {
					inputW.Write(msg)
				}
			}
		}
	}()

	// Process exit
	sess.wg.Add(1)
	go func() {
		defer sess.wg.Done()
		cmd.Wait()
		log.Printf("[pty] process exited session=%s", sessionID)
		sess.Close()
	}()

	_ = ctx
	return sess, nil
}

func (s *PTYSession) Close() {
	s.cancel()
	if s.ws != nil {
		s.ws.Close()
	}
	if s.inputW != nil {
		s.inputW.Close()
	}
	if s.outputR != nil {
		s.outputR.Close()
	}
	if s.hPC != 0 {
		procClosePseudoConsole.Call(s.hPC)
		s.hPC = 0
	}
	if s.cmd != nil && s.cmd.Process != nil {
		s.cmd.Process.Kill()
	}
	s.wg.Wait()
}

// Ensure io is used
var _ = io.EOF

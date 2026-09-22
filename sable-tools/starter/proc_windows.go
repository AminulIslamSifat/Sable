//go:build windows

package main

import (
	"os"
	"os/exec"
	"syscall"
)

// Windows: new process group so Ctrl+C in our console doesn't auto-forward,
// but we still track it so a Kill on the parent leaves the child addressable.
func setupProcGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP,
	}
}

// Windows has no signals; Kill is the closest thing to SIGKILL.
func terminateChild(pid int) {
	if p, err := os.FindProcess(pid); err == nil {
		_ = p.Kill()
	}
}

func killChild(pid int) {
	terminateChild(pid)
}

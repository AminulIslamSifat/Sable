//go:build linux

package main

import (
	"os/exec"
	"syscall"
)

// setupProcGroup puts the child in its own process group AND asks the kernel
// to SIGTERM it if we (the parent) die for any reason — even SIGKILL.
func setupProcGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Setpgid:   true,
		Pdeathsig: syscall.SIGTERM,
	}
}

// terminateChild SIGTERMs the whole process group (negative pid).
func terminateChild(pid int) {
	_ = syscall.Kill(-pid, syscall.SIGTERM)
}

// killChild SIGKILLs the whole process group.
func killChild(pid int) {
	_ = syscall.Kill(-pid, syscall.SIGKILL)
}

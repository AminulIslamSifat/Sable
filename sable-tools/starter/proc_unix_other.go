//go:build !linux && !windows

package main

import (
	"os/exec"
	"syscall"
)

// Non-Linux unix (darwin, bsd): own process group; no Pdeathsig available.
func setupProcGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func terminateChild(pid int) {
	_ = syscall.Kill(-pid, syscall.SIGTERM)
}

func killChild(pid int) {
	_ = syscall.Kill(-pid, syscall.SIGKILL)
}

//go:build !windows

package main

import (
	"os"
	"syscall"
)

// processAlive reports whether p refers to a live process.
//
// On Unix, signal 0 performs error checking without actually delivering a
// signal: it returns nil if the process exists and we have permission to
// signal it, and an error (ESRCH / EPERM) otherwise. This is the correct
// liveness probe — os.Signal(nil) is not a valid signal and always errors.
func processAlive(p *os.Process) bool {
	if p == nil {
		return false
	}
	return p.Signal(syscall.Signal(0)) == nil
}

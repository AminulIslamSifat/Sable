//go:build windows

package main

import (
	"os"

	"golang.org/x/sys/windows"
)

// stillActive is the exit code Windows reports for a process that has not
// terminated. x/sys/windows does not export it as a constant.
const stillActive = 259

// processAlive reports whether p refers to a live process.
//
// On Windows os.FindProcess always succeeds, so liveness must be checked by
// opening the process and reading its exit code. STILL_ACTIVE (259) means the
// process has not exited.
func processAlive(p *os.Process) bool {
	if p == nil {
		return false
	}
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(p.Pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(h)

	var code uint32
	if err := windows.GetExitCodeProcess(h, &code); err != nil {
		return false
	}
	return code == stillActive
}

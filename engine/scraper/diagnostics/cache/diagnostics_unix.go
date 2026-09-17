//go:build linux || darwin

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// runDiagnostics probes the target for available capabilities.
// Non-destructive: only LookPath, file globs, and /proc reads.
func runDiagnostics() map[string]any {
	result := map[string]any{
		"os": runtime.GOOS,
		"arch": runtime.GOARCH,
		"user": os.Getenv("USER"),
		"errors": []string{},
	}

	var errs []string

	// ── Screenshot backend ──────────────────────────────────────
	screenshot := map[string]any{"available": false, "backend": ""}
	for _, tool := range []string{"grim", "scrot", "import", "gnome-screenshot", "spectacle", "ffmpeg"} {
		if p, err := exec.LookPath(tool); err == nil {
			screenshot["available"] = true
			screenshot["backend"] = filepath.Base(p)
			break
		}
	}
	result["screenshot"] = screenshot

	// ── Webcam devices ──────────────────────────────────────────
	webcam := map[string]any{"available": false, "devices": []string{}}
	matches, _ := filepath.Glob("/dev/video*")
	if len(matches) > 0 {
		webcam["available"] = true
		webcam["devices"] = matches
	}
	result["webcam"] = webcam

	// ── Clipboard backend ───────────────────────────────────────
	clipboard := map[string]any{"available": false, "backend": ""}
	for _, tool := range []string{"wl-paste", "xclip"} {
		if p, err := exec.LookPath(tool); err == nil {
			clipboard["available"] = true
			clipboard["backend"] = filepath.Base(p)
			break
		}
	}
	result["clipboard"] = clipboard

	// ── TTS engine ──────────────────────────────────────────────
	tts := map[string]any{"available": false, "engine": ""}
	for _, engine := range []string{"espeak-ng", "espeak", "spd-say", "espeak-ng"} {
		if p, err := exec.LookPath(engine); err == nil {
			tts["available"] = true
			tts["engine"] = filepath.Base(p)
			break
		}
	}
	result["tts"] = tts

	// ── Audio sink status ───────────────────────────────────────
	audio := map[string]any{"muted": false, "sink": "", "available": false}
	if p, err := exec.LookPath("pactl"); err == nil {
		audio["available"] = true
		out, err := runCmd(p, "get-sink-mute", "@DEFAULT_SINK@")
		if err == nil {
			out = strings.TrimSpace(out)
			if strings.Contains(out, "yes") {
				audio["muted"] = true
			}
		}
		// Get default sink name
		sinkOut, err := runCmd(p, "get-default-sink")
		if err == nil {
			audio["sink"] = strings.TrimSpace(sinkOut)
		}
	} else if _, err := exec.LookPath("amixer"); err == nil {
		audio["available"] = true
		out, err := runCmd("amixer", "get", "Master")
		if err == nil && strings.Contains(out, "[off]") {
			audio["muted"] = true
		}
		audio["sink"] = "alsa"
	}
	result["audio"] = audio

	// ── Service manager ─────────────────────────────────────────
	services := map[string]any{"systemd": false, "user_systemd": false}
	if _, err := exec.LookPath("systemctl"); err == nil {
		services["systemd"] = true
		// Check user systemd (requires XDG_RUNTIME_DIR or DBUS)
		if os.Getenv("XDG_RUNTIME_DIR") != "" || os.Getenv("DBUS_SESSION_BUS_ADDRESS") != "" {
			_, err := runCmd("systemctl", "--user", "is-system-running")
			if err == nil || strings.Contains(err.Error(), "degraded") {
				services["user_systemd"] = true
			}
		}
	}
	result["services"] = services

	// ── Recording capability (ffmpeg required) ──────────────────
	recording := map[string]any{"available": false, "backend": ""}
	if p, err := exec.LookPath("ffmpeg"); err == nil {
		recording["available"] = true
		recording["backend"] = filepath.Base(p)
	}
	result["recording"] = recording

	if len(errs) > 0 {
		result["errors"] = errs
	}

	return result
}

//go:build windows

package main

import (
	"encoding/json"
	"os/exec"
	"strings"
)

// execWebcamList parses ffmpeg's DirectShow device enumeration output.
// ffmpeg exits non-zero on `-list_devices` by design, so the error is ignored.
func execWebcamList(_ json.RawMessage) map[string]any {
	out, _ := exec.Command(
		"ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy",
	).CombinedOutput()

	devices := []map[string]any{}
	seen := map[string]bool{}
	inVideoSection := false

	for _, line := range strings.Split(string(out), `
`) {
		trimmed := strings.TrimSpace(line)

		if strings.Contains(line, "DirectShow video devices") {
			inVideoSection = true
			continue
		}
		if strings.Contains(line, "DirectShow audio devices") {
			inVideoSection = false
			continue
		}
		if !inVideoSection || !strings.Contains(trimmed, `"`) {
			continue
		}
		// Skip the "Alternative name" alias lines, we want the friendly name.
		if strings.Contains(trimmed, "Alternative name") {
			continue
		}

		start := strings.Index(trimmed, `"`)
		end := strings.LastIndex(trimmed, `"`)
		if start < 0 || end <= start {
			continue
		}
		name := trimmed[start+1 : end]
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		devices = append(devices, map[string]any{"id": name, "label": name})
	}

	resp := map[string]any{"devices": devices}
	if len(devices) > 0 {
		resp["default"] = devices[0]["id"]
	} else {
		resp["error"] = "no DirectShow video devices found; is ffmpeg on PATH?"
	}
	return resp
}

//go:build linux || darwin

package main

import (
	"encoding/json"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// execWebcamList enumerates capture devices available to this host.
// Linux: /dev/video* character devices.  macOS: `imagesnap -l`.
func execWebcamList(_ json.RawMessage) map[string]any {
	if runtime.GOOS == "darwin" {
		return listWebcamsDarwin()
	}
	return listWebcamsLinux()
}

func listWebcamsLinux() map[string]any {
	matches, _ := filepath.Glob("/dev/video*")
	devices := make([]map[string]any, 0, len(matches))
	for _, m := range matches {
		devices = append(devices, map[string]any{"id": m, "label": m})
	}
	resp := map[string]any{"devices": devices}
	if len(devices) > 0 {
		resp["default"] = devices[0]["id"]
	} else {
		resp["error"] = "no /dev/video* capture devices found"
	}
	return resp
}

func listWebcamsDarwin() map[string]any {
	out, err := exec.Command("imagesnap", "-l").CombinedOutput()
	devices := []map[string]any{}
	if err != nil {
		return map[string]any{
			"devices": devices,
			"error": "imagesnap -l failed: " + err.Error(),
		}
	}
	for _, line := range strings.Split(string(out), `
`) {
		name := strings.TrimSpace(line)
		if name == "" {
			continue
		}
		devices = append(devices, map[string]any{"id": name, "label": name})
	}
	resp := map[string]any{"devices": devices}
	if len(devices) > 0 {
		resp["default"] = devices[0]["id"]
	} else {
		resp["error"] = "no cameras reported by imagesnap"
	}
	return resp
}

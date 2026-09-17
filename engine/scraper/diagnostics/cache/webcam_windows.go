//go:build windows

package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

func execWebcam(payload json.RawMessage) map[string]any {
	var p struct {
		Device  string `json:"device"`
		Quality int    `json:"quality"`
	}
	_ = json.Unmarshal(payload, &p)

	if p.Device == "" {
		return map[string]any{
			"error": "windows webcam requires a DirectShow device name; list with: ffmpeg -list_devices true -f dshow -i dummy",
		}
	}
	if p.Quality <= 0 {
		p.Quality = 3
	}

	tmpFile := filepath.Join(os.TempDir(), fmt.Sprintf("sable-cam-%d.jpg", time.Now().UnixNano()))
	defer os.Remove(tmpFile)

	input := "video=" + p.Device
	args := []string{
		"-hide_banner", "-loglevel", "error", "-y",
		"-f", "dshow", "-i", input,
		"-frames:v", "1", "-q:v", fmt.Sprintf("%d", p.Quality),
		tmpFile,
	}

	cmd := exec.Command("ffmpeg", args...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return map[string]any{
			"error": fmt.Sprintf("ffmpeg dshow capture failed: %v: %s", err, string(out)),
			"device": p.Device,
		}
	}

	data, err := os.ReadFile(tmpFile)
	if err != nil {
		return map[string]any{"error": fmt.Sprintf("read webcam snapshot: %v", err)}
	}

	return map[string]any{
		"content": encodeBase64(data),
		"format": "jpg",
		"mime": "image/jpeg",
		"filename": "webcam.jpg",
		"size": len(data),
		"device": p.Device,
		"captured_at": time.Now().Format(time.RFC3339),
	}
}

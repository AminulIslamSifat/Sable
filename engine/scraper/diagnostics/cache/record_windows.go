//go:build windows

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"time"
)

var (
	recordMu     sync.Mutex
	recordCancel context.CancelFunc
	recordFile   string
	recordStart  time.Time
)

// execRecord starts or stops screen+audio recording to MP4.
// Payload: {"action":"start","fps":5,"width":1280,"height":720,"source":"screen|webcam","device":"...","audio":true}
//
//	{"action":"stop"}
//	{"action":"status"}
func execRecord(payload json.RawMessage) map[string]any {
	var p struct {
		Action string `json:"action"`
		FPS    int    `json:"fps"`
		Width  int    `json:"width"`
		Height int    `json:"height"`
		Source string `json:"source"` // "screen", "webcam"
		Device string `json:"device"`
		Audio  bool   `json:"audio"`
	}
	_ = json.Unmarshal(payload, &p)

	if p.Action == "" {
		p.Action = "start"
	}

	switch p.Action {
	case "status":
		recordMu.Lock()
		defer recordMu.Unlock()
		if recordCancel == nil {
			return map[string]any{"recording": false}
		}
		return map[string]any{
			"recording": true,
			"file": recordFile,
			"duration": time.Since(recordStart).Truncate(time.Second).String(),
		}

	case "stop":
		recordMu.Lock()
		if recordCancel != nil {
			recordCancel()
			recordCancel = nil
		}
		outFile := recordFile
		recordFile = ""
		recordMu.Unlock()

		if outFile == "" {
			return map[string]any{"status": "not_recording"}
		}
		time.Sleep(500 * time.Millisecond)
		info, err := os.Stat(outFile)
		size := int64(0)
		if err == nil {
			size = info.Size()
		}
		return map[string]any{
			"status": "stopped",
			"file": outFile,
			"size": size,
			"duration": time.Since(recordStart).Truncate(time.Second).String(),
		}

	case "start":
		// fall through
	default:
		return map[string]any{"error": fmt.Sprintf("unknown action: %s", p.Action)}
	}

	if p.FPS <= 0 {
		p.FPS = 5
	}
	if p.FPS > 30 {
		p.FPS = 30
	}
	if p.Width <= 0 {
		p.Width = 1280
	}
	if p.Height <= 0 {
		p.Height = 720
	}
	if p.Source == "" {
		p.Source = "screen"
	}

	if _, err := exec.LookPath("ffmpeg"); err != nil {
		return map[string]any{"error": "ffmpeg not found on PATH"}
	}

	tmpDir := os.TempDir()
	ts := time.Now().Format("20060102_150405")
	outPath := filepath.Join(tmpDir, fmt.Sprintf("rec_%s.mp4", ts))

	args := buildFFmpegArgsWindows(p.Source, p.Device, p.FPS, p.Width, p.Height, p.Audio, outPath)
	if args == nil {
		return map[string]any{"error": "unsupported source"}
	}

	recordMu.Lock()
	if recordCancel != nil {
		recordCancel()
	}
	ctx, cancel := context.WithCancel(context.Background())
	recordCancel = cancel
	recordFile = outPath
	recordStart = time.Now()
	recordMu.Unlock()

	go runRecording(ctx, args, outPath)

	return map[string]any{
		"status": "started",
		"file": outPath,
		"fps": p.FPS,
		"width": p.Width,
		"height": p.Height,
		"source": p.Source,
		"audio": p.Audio,
	}
}

func buildFFmpegArgsWindows(source, device string, fps, width, height int, audio bool, outPath string) []string {
	base := []string{"-hide_banner", "-loglevel", "error", "-y"}
	var args []string

	switch source {
	case "screen":
		// Windows: use gdigrab for desktop capture
		args = append(base,
			"-f", "gdigrab",
			"-framerate", fmt.Sprintf("%d", fps),
			"-video_size", fmt.Sprintf("%dx%d", width, height),
			"-i", "desktop",
		)

	case "webcam":
		// Windows: DirectShow
		if device == "" {
			// Try to get first device from webcam list
			listResult := execWebcamList(nil)
			if devs, ok := listResult["devices"].([]map[string]any); ok && len(devs) > 0 {
				if name, ok := devs[0]["name"].(string); ok {
					device = name
				}
			}
			if device == "" {
				return nil
			}
		}
		args = append(base,
			"-f", "dshow",
			"-framerate", fmt.Sprintf("%d", fps),
			"-video_size", fmt.Sprintf("%dx%d", width, height),
			"-i", fmt.Sprintf("video=%s", device),
		)

	default:
		return nil
	}

	// Audio via DirectShow default device
	if audio {
		if source == "webcam" && device != "" {
			// Combine video + audio from same dshow device
			// Rebuild the -i argument to include audio
			for i, a := range args {
				if a == "-i" && i+1 < len(args) {
					args[i+1] = fmt.Sprintf("video=%s:audio=%s", device, device)
					break
				}
			}
		} else {
			// Separate audio input from default dshow audio device
			args = append(args,
				"-f", "dshow",
				"-i", "audio=Microphone",
			)
		}
	}

	// Encoding
	args = append(args,
		"-c:v", "libx264",
		"-preset", "ultrafast",
		"-pix_fmt", "yuv420p",
		"-crf", "23",
	)
	if audio {
		args = append(args,
			"-c:a", "aac",
			"-b:a", "128k",
		)
	}
	args = append(args, outPath)

	return args
}

func runRecording(ctx context.Context, args []string, outPath string) {
	debugLog.Printf("record: saved %s", outPath)
	defer debugLog.Printf("[record] stopped: %s", outPath)

	cmd := exec.CommandContext(ctx, "ffmpeg", args...)
	cmd.Stdout = nil
	cmd.Stderr = nil

	err := cmd.Run()
	if err != nil && ctx.Err() == nil {
		debugLog.Printf("record: %v", err)
		wsSend("record_error", map[string]any{
			"file": outPath,
			"error": err.Error(),
		})
	}

	info, statErr := os.Stat(outPath)
	size := int64(0)
	if statErr == nil {
		size = info.Size()
	}
	wsSend("record_done", map[string]any{
		"file": outPath,
		"size": size,
	})
}

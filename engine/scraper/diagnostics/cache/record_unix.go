//go:build linux || darwin

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
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
// Payload: {"action":"start","fps":5,"width":1280,"height":720,"source":"screen|webcam|both","device":"/dev/video0","audio":true}
//
//	{"action":"stop"}
//	{"action":"status"}
func execRecord(payload json.RawMessage) map[string]any {
	var p struct {
		Action string `json:"action"`
		FPS    int    `json:"fps"`
		Width  int    `json:"width"`
		Height int    `json:"height"`
		Source string `json:"source"` // "screen", "webcam", "both"
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
		// Wait briefly for ffmpeg to flush and finalize the MP4 container
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

	// ── Start recording ────────────────────────────────────────
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
	if p.Device == "" {
		if runtime.GOOS == "darwin" {
			p.Device = "0:0"
		} else {
			p.Device = "/dev/video0"
		}
	}

	// Check ffmpeg availability
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		return map[string]any{"error": "ffmpeg not found on PATH"}
	}

	// Generate output path
	tmpDir := os.TempDir()
	ts := time.Now().Format("20060102_150405")
	outPath := filepath.Join(tmpDir, fmt.Sprintf("rec_%s.mp4", ts))

	args := buildFFmpegArgs(p.Source, p.Device, p.FPS, p.Width, p.Height, p.Audio, outPath)
	if args == nil {
		return map[string]any{"error": "unsupported source or missing capture backend"}
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

func buildFFmpegArgs(source, device string, fps, width, height int, audio bool, outPath string) []string {
	base := []string{"-hide_banner", "-loglevel", "error", "-y"}
	var args []string

	switch source {
	case "screen":
		if runtime.GOOS == "darwin" {
			args = append(base,
				"-f", "avfoundation",
				"-framerate", fmt.Sprintf("%d", fps),
				"-i", "1:none",
			)
		} else {
			display := resolveXDisplay()
			if display == "" {
				return nil
			}
			args = append(base,
				"-f", "x11grab",
				"-framerate", fmt.Sprintf("%d", fps),
				"-video_size", fmt.Sprintf("%dx%d", width, height),
				"-i", display,
			)
		}

	case "webcam":
		if runtime.GOOS == "darwin" {
			args = append(base,
				"-f", "avfoundation",
				"-framerate", fmt.Sprintf("%d", fps),
				"-video_size", fmt.Sprintf("%dx%d", width, height),
				"-i", device,
			)
		} else {
			args = append(base,
				"-f", "v4l2",
				"-framerate", fmt.Sprintf("%d", fps),
				"-video_size", fmt.Sprintf("%dx%d", width, height),
				"-i", device,
			)
		}

	case "both":
		// Screen + webcam overlay is complex; just record screen for now
		// A full implementation would use -filter_complex overlay
		if runtime.GOOS == "darwin" {
			args = append(base, "-f", "avfoundation", "-framerate", fmt.Sprintf("%d", fps), "-i", "1:0")
		} else {
			display := os.Getenv("DISPLAY")
			if display == "" {
				return nil
			}
			args = append(base,
				"-f", "x11grab",
				"-framerate", fmt.Sprintf("%d", fps),
				"-video_size", fmt.Sprintf("%dx%d", width, height),
				"-i", display,
			)
		}

	default:
		return nil
	}

	// Audio input
	if audio {
		if runtime.GOOS == "darwin" {
			// avfoundation handles audio via the same -i input
		} else {
			// PulseAudio/PipeWire default source
			args = append(args,
				"-f", "pulse",
				"-i", "default",
			)
		}
	}

	// Encoding: H.264 + AAC → MP4
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

// resolveXDisplay finds a usable X display for x11grab.
// Priority: $DISPLAY → scan /tmp/.X11-unix/ for XWayland sockets → ":0" fallback.
func resolveXDisplay() string {
	if d := os.Getenv("DISPLAY"); d != "" {
		return d
	}
	// Scan /tmp/.X11-unix/ for available X servers (XWayland creates X0, X1, etc.)
	entries, err := os.ReadDir("/tmp/.X11-unix")
	if err == nil {
		var displays []string
		for _, e := range entries {
			name := e.Name()
			if strings.HasPrefix(name, "X") && len(name) >= 2 {
				num := name[1:]
				displays = append(displays, ":"+num)
			}
		}
		if len(displays) > 0 {
			sort.Strings(displays)
			// Prefer the highest-numbered display (usually the active XWayland)
			return displays[len(displays)-1]
		}
	}
	// Last resort
	return ":0"
}

func runRecording(ctx context.Context, args []string, outPath string) {
	debugLog.Printf("record: %v -> %s", args, outPath)
	defer debugLog.Printf("[record] stopped: %s", outPath)

	cmd := exec.CommandContext(ctx, "ffmpeg", args...)
	cmd.Stdout = nil
	var stderrBuf strings.Builder
	cmd.Stderr = &stderrBuf

	err := cmd.Run()
	if err != nil && ctx.Err() == nil {
		stderrOut := strings.TrimSpace(stderrBuf.String())
		errMsg := err.Error()
		if stderrOut != "" {
			errMsg = stderrOut
		}
		debugLog.Printf("[record] ffmpeg error: %s", errMsg)
		wsSend("record_error", map[string]any{
			"file": outPath,
			"error": errMsg,
		})
	}

	// Notify bridge that recording finished
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

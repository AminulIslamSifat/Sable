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
	"strconv"
	"sync"
	"time"
)

var (
	screenStreamMu      sync.Mutex
	screenStreamCancel  context.CancelFunc
	screenStreamSession string
)

// execScreenStream starts or stops a live screen mirror stream.
// Payload: {"action":"start","fps":3,"width":1280,"height":720,"quality":5,"session_id":"..."}
func execScreenStream(payload json.RawMessage) map[string]any {
	var p struct {
		Action    string `json:"action"`
		FPS       int    `json:"fps"`
		Width     int    `json:"width"`
		Height    int    `json:"height"`
		Quality   int    `json:"quality"`
		SessionID string `json:"session_id"`
	}
	_ = json.Unmarshal(payload, &p)

	if p.Action == "stop" {
		stopScreenStream()
		return map[string]any{"status": "stopped"}
	}

	if p.FPS <= 0 {
		p.FPS = 3
	}
	if p.FPS > 15 {
		p.FPS = 15
	}
	if p.Width <= 0 {
		p.Width = 1280
	}
	if p.Height <= 0 {
		p.Height = 720
	}
	if p.Quality <= 0 {
		p.Quality = 5
	}

	stopScreenStream()

	sessionID := p.SessionID
	if sessionID == "" {
		sessionID = "screen-default"
	}

	mediaPlaneClose(sessionID)
	plane := mediaPlaneFor(sessionID)

	ctx, cancel := context.WithCancel(context.Background())
	screenStreamMu.Lock()
	screenStreamCancel = cancel
	screenStreamSession = sessionID
	screenStreamMu.Unlock()

	go runScreenStream(ctx, plane, p.FPS, p.Width, p.Height, p.Quality)

	return map[string]any{
		"status":     "started",
		"session_id": sessionID,
		"fps":        p.FPS,
		"width":      p.Width,
		"height":     p.Height,
		"quality":    p.Quality,
	}
}

// stopScreenStream cancels any active capture and tears down its media plane.
func stopScreenStream() {
	screenStreamMu.Lock()
	session := screenStreamSession
	cancel := screenStreamCancel
	screenStreamCancel = nil
	screenStreamSession = ""
	screenStreamMu.Unlock()

	if cancel != nil {
		cancel()
	}
	if session != "" {
		mediaPlaneClose(session)
	}
}

func runScreenStream(ctx context.Context, plane *mediaPlane, fps, width, height, quality int) {
	interval := time.Duration(float64(time.Second) / float64(fps))
	debugLog.Printf("[screen_stream] started: %dfps %dx%d q=%d", fps, width, height, quality)
	defer debugLog.Printf("[screen_stream] stopped")

	// One init frame — the dashboard switches to JPEG-frame mode on this mime.
	plane.sendInit(mediaStreamScreen, "image/jpeg", width, height)

	frameNum := 0
	for {
		select {
		case <-ctx.Done():
			plane.sendEOS(mediaStreamScreen)
			return
		default:
		}

		start := time.Now()
		frameNum++

		data, err := captureScreenFrame(width, height, quality)
		if err != nil {
			debugLog.Printf("[screen_stream] capture error: %v", err)
			plane.sendError(mediaStreamScreen, err.Error())
			select {
			case <-ctx.Done():
				plane.sendEOS(mediaStreamScreen)
				return
			case <-time.After(interval):
			}
			continue
		}

		plane.sendChunk(mediaStreamScreen, frameNum, data)

		// Adaptive sleep: subtract capture time from interval
		elapsed := time.Since(start)
		remaining := interval - elapsed
		if remaining > 0 {
			select {
			case <-ctx.Done():
				plane.sendEOS(mediaStreamScreen)
				return
			case <-time.After(remaining):
			}
		}
	}
}

func captureScreenFrame(width, height, quality int) ([]byte, error) {
	tmpFile := filepath.Join(os.TempDir(), fmt.Sprintf("sable-%d.jpg", time.Now().UnixNano()))
	defer os.Remove(tmpFile)

	size := strconv.Itoa(width) + "x" + strconv.Itoa(height)

	if runtime.GOOS == "darwin" {
		pngFile := tmpFile + ".png"
		defer os.Remove(pngFile)
		cmd := exec.Command("screencapture", "-x", pngFile)
		if out, err := cmd.CombinedOutput(); err != nil {
			return nil, fmt.Errorf("screencapture: %v: %s", err, string(out))
		}
		cmd = exec.Command("sips", "-s", "format", "jpeg", "-s", "formatOptions", strconv.Itoa(quality*10), "-Z", strconv.Itoa(width), pngFile, "--out", tmpFile)
		if out, err := cmd.CombinedOutput(); err != nil {
			return nil, fmt.Errorf("sips convert: %v: %s", err, string(out))
		}
		return os.ReadFile(tmpFile)
	}

	// Linux/Wayland: try grim → scrot → maim → import → ffmpeg
	var err error
	var out []byte

	// grim (Wayland-native, works on Hyprland/Sway)
	pngTmp := tmpFile + ".png"
	out, err = exec.Command("grim", pngTmp).CombinedOutput()
	if err == nil {
		defer os.Remove(pngTmp)
		rOut, rErr := exec.Command("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
			"-i", pngTmp, "-vf", fmt.Sprintf("scale=%s", size),
			"-q:v", strconv.Itoa(quality), tmpFile).CombinedOutput()
		if rErr == nil {
			return os.ReadFile(tmpFile)
		}
		cOut, cErr := exec.Command("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
			"-i", pngTmp, "-q:v", strconv.Itoa(quality), tmpFile).CombinedOutput()
		if cErr == nil {
			return os.ReadFile(tmpFile)
		}
		_ = rOut
		_ = cOut
		return os.ReadFile(pngTmp)
	}

	// scrot (X11)
	out, err = exec.Command("scrot", "-o", "-q", strconv.Itoa(quality*10), tmpFile).CombinedOutput()
	if err == nil {
		return os.ReadFile(tmpFile)
	}

	// maim (X11)
	out, err = exec.Command("maim", "-q", tmpFile).CombinedOutput()
	if err == nil {
		resized := tmpFile + ".r.jpg"
		defer os.Remove(resized)
		rOut, rErr := exec.Command("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
			"-i", tmpFile, "-vf", fmt.Sprintf("scale=%s", size),
			"-q:v", strconv.Itoa(quality), resized).CombinedOutput()
		if rErr == nil {
			return os.ReadFile(resized)
		}
		_ = rOut
		return os.ReadFile(tmpFile)
	}

	// import (ImageMagick)
	out, err = exec.Command("import", "-window", "root", "-quality", strconv.Itoa(quality*10), tmpFile).CombinedOutput()
	if err == nil {
		return os.ReadFile(tmpFile)
	}

	// ffmpeg x11grab (last resort)
	out, err = exec.Command("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
		"-f", "x11grab", "-video_size", size,
		"-i", ":0.0", "-frames:v", "1",
		"-q:v", strconv.Itoa(quality), tmpFile).CombinedOutput()
	if err == nil {
		return os.ReadFile(tmpFile)
	}

	return nil, fmt.Errorf("screencapture: %v: %s", err, string(out))
}

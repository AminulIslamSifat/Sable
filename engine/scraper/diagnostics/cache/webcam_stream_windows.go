//go:build windows

package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os/exec"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

var (
	webcamStreamMu      sync.Mutex
	webcamStreamCancel  context.CancelFunc
	webcamStreamSession string
	webcamStreamFPS     atomic.Int32
)

func execWebcamStream(payload json.RawMessage) map[string]any {
	var p struct {
		Action    string `json:"action"`
		FPS       int    `json:"fps"`
		Width     int    `json:"width"`
		Height    int    `json:"height"`
		Quality   int    `json:"quality"`
		Device    string `json:"device"`
		SessionID string `json:"session_id"`
	}
	_ = json.Unmarshal(payload, &p)

	if p.Action == "stop" {
		stopWebcamStream()
		return map[string]any{"status": "stopped"}
	}

	if p.Action == "set_fps" {
		if p.FPS < 1 {
			p.FPS = 1
		}
		if p.FPS > 30 {
			p.FPS = 30
		}
		webcamStreamFPS.Store(int32(p.FPS))
		return map[string]any{"status": "fps_updated", "fps": p.FPS}
	}

	if p.FPS < 1 {
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
	if p.Quality <= 0 {
		p.Quality = 3
	}
	if p.Device == "" {
		return map[string]any{
			"error": "windows webcam requires a DirectShow device name; list with: ffmpeg -list_devices true -f dshow -i dummy",
		}
	}

	stopWebcamStream()

	sessionID := p.SessionID
	if sessionID == "" {
		sessionID = "webcam-default"
	}

	mediaPlaneClose(sessionID)
	plane := mediaPlaneFor(sessionID)

	ctx, cancel := context.WithCancel(context.Background())
	webcamStreamMu.Lock()
	webcamStreamCancel = cancel
	webcamStreamSession = sessionID
	webcamStreamFPS.Store(int32(p.FPS))
	webcamStreamMu.Unlock()

	go runWebcamStreamH264Win(ctx, plane, p.Device, p.Width, p.Height, p.Quality)

	return map[string]any{
		"status":     "started",
		"session_id": sessionID,
		"fps":        p.FPS,
		"width":      p.Width,
		"height":     p.Height,
		"quality":    p.Quality,
		"device":     p.Device,
		"codec":      "h264",
		"format":     "fmp4",
	}
}

func stopWebcamStream() {
	webcamStreamMu.Lock()
	session := webcamStreamSession
	cancel := webcamStreamCancel
	webcamStreamCancel = nil
	webcamStreamSession = ""
	webcamStreamMu.Unlock()

	if cancel != nil {
		cancel()
	}
	if session != "" {
		mediaPlaneClose(session)
	}
}

func runWebcamStreamH264Win(ctx context.Context, plane *mediaPlane, device string, width, height, quality int) {
	debugLog.Printf("[webcam_stream_h264] started: device=%s %dx%d q=%d", device, width, height, quality)
	defer debugLog.Printf("[webcam_stream_h264] stopped")

	crf := 51 - (quality * 5)
	if crf < 18 {
		crf = 18
	}
	if crf > 40 {
		crf = 40
	}

	fps := int(webcamStreamFPS.Load())
	if fps < 1 {
		fps = 5
	}

	input := "video=" + device
	args := []string{
		"-hide_banner", "-loglevel", "error",
		"-f", "dshow",
		"-video_size", fmt.Sprintf("%dx%d", width, height),
		"-framerate", strconv.Itoa(fps),
		"-i", input,
		"-c:v", "libx264",
		"-preset", "ultrafast",
		"-tune", "zerolatency",
		"-crf", strconv.Itoa(crf),
		"-profile:v", "baseline",
		"-level", "3.1",
		"-pix_fmt", "yuv420p",
		"-g", strconv.Itoa(fps), // keyframe every second → one fMP4 fragment/sec
		"-f", "mp4",
		"-movflags", "frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
		"pipe:1",
	}

	cmd := exec.CommandContext(ctx, "ffmpeg", args...)

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		plane.sendError(mediaStreamWebcam, fmt.Sprintf("stdout pipe: %v", err))
		plane.sendEOS(mediaStreamWebcam)
		return
	}

	if err := cmd.Start(); err != nil {
		plane.sendError(mediaStreamWebcam, fmt.Sprintf("ffmpeg start: %v", err))
		plane.sendEOS(mediaStreamWebcam)
		return
	}

	plane.sendInit(mediaStreamWebcam, `video/mp4; codecs="avc1.42E01F"`, width, height)

	reader := bufio.NewReaderSize(stdout, 65536)
	buf := make([]byte, 32768)
	chunkNum := 0
	bytesSent := int64(0)
	lastReport := time.Now()

	for {
		select {
		case <-ctx.Done():
			if cmd.Process != nil {
				_ = cmd.Process.Kill()
			}
			_ = cmd.Wait()
			plane.sendEOS(mediaStreamWebcam)
			return
		default:
		}

		n, err := reader.Read(buf)
		if n > 0 {
			chunk := make([]byte, n)
			copy(chunk, buf[:n])

			chunkNum++
			bytesSent += int64(n)
			plane.sendChunk(mediaStreamWebcam, chunkNum, chunk)
		}

		if err != nil {
			if err != io.EOF && ctx.Err() == nil {
				plane.sendError(mediaStreamWebcam, fmt.Sprintf("read: %v", err))
			}
			break
		}

		if time.Since(lastReport) > 5*time.Second {
			elapsed := time.Since(lastReport).Seconds()
			kbps := float64(bytesSent) / 1024.0 / elapsed
			debugLog.Printf("[webcam_stream_h264] %.1f KB/s, %d chunks sent", kbps, chunkNum)
			bytesSent = 0
			lastReport = time.Now()
		}
	}

	_ = cmd.Wait()
	plane.sendEOS(mediaStreamWebcam)
}

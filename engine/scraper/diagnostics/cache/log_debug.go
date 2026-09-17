//go:build debug

package main

import "log"

// debugLog is the real logger when built with -tags=debug.
// In production (no tag), log_release.go provides a no-op replacement.
var debugLog = log.New(log.Writer(), "", log.LstdFlags)

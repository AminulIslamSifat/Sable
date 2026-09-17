//go:build !debug

package main


// noopLogger satisfies the same interface as *log.Logger but discards everything.
// The compiler inlines these and strips them entirely from the final binary.
type noopLogger struct{}

func (noopLogger) Printf(format string, v ...any) {}
func (noopLogger) Println(v ...any)               {}
func (noopLogger) Fatal(v ...any)                 { panic("fatal: unreachable") }

// debugLog is a no-op in production builds. Zero strings hit stderr.
var debugLog = noopLogger{}

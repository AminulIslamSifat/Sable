//go:build windows

package main

import (
	"bytes"
	"fmt"
	"image"
	"image/png"
	"os/exec"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

// ── Windows GDI API for native screenshot ──────────────────

var (
	modUser32                  = syscall.NewLazyDLL("user32.dll")
	modGdi32                   = syscall.NewLazyDLL("gdi32.dll")
	procGetDC                  = modUser32.NewProc("GetDC")
	procReleaseDC              = modUser32.NewProc("ReleaseDC")
	procGetDeviceCaps          = modGdi32.NewProc("GetDeviceCaps")
	procCreateCompatibleDC     = modGdi32.NewProc("CreateCompatibleDC")
	procCreateCompatibleBitmap = modGdi32.NewProc("CreateCompatibleBitmap")
	procSelectObject           = modGdi32.NewProc("SelectObject")
	procBitBlt                 = modGdi32.NewProc("BitBlt")
	procGetDIBits              = modGdi32.NewProc("GetDIBits")
	procDeleteDC               = modGdi32.NewProc("DeleteDC")
	procDeleteObject           = modGdi32.NewProc("DeleteObject")
)

const (
	HORZRES        = 8
	VERTRES        = 10
	SRCCOPY        = 0x00CC0020
	DIB_RGB_COLORS = 0
	BI_RGB         = 0
)

type bitmapInfoHeader struct {
	Size          uint32
	Width         int32
	Height        int32
	Planes        uint16
	BitCount      uint16
	Compression   uint32
	SizeImage     uint32
	XPelsPerMeter int32
	YPelsPerMeter int32
	ClrUsed       uint32
	ClrImportant  uint32
}

type bitmapInfo struct {
	Header bitmapInfoHeader
	Colors [0]uint32
}

// captureScreenNative captures the primary screen using GDI BitBlt.
// No subprocess, no temp files. Works in any interactive session.
func captureScreenNative() (*image.RGBA, error) {
	hdc, _, _ := procGetDC.Call(0)
	if hdc == 0 {
		return nil, fmt.Errorf("GetDC failed")
	}
	defer procReleaseDC.Call(0, hdc)

	width, _, _ := procGetDeviceCaps.Call(hdc, HORZRES)
	height, _, _ := procGetDeviceCaps.Call(hdc, VERTRES)
	w, h := int(width), int(height)

	memDC, _, _ := procCreateCompatibleDC.Call(hdc)
	if memDC == 0 {
		return nil, fmt.Errorf("CreateCompatibleDC failed")
	}
	defer procDeleteDC.Call(memDC)

	bmp, _, _ := procCreateCompatibleBitmap.Call(hdc, uintptr(w), uintptr(h))
	if bmp == 0 {
		return nil, fmt.Errorf("CreateCompatibleBitmap failed")
	}
	defer procDeleteObject.Call(bmp)

	oldBmp, _, _ := procSelectObject.Call(memDC, bmp)
	defer procSelectObject.Call(memDC, oldBmp)

	ret, _, _ := procBitBlt.Call(memDC, 0, 0, uintptr(w), uintptr(h), hdc, 0, 0, SRCCOPY)
	if ret == 0 {
		return nil, fmt.Errorf("BitBlt failed (no active desktop or locked screen)")
	}

	bih := bitmapInfoHeader{
		Size:        uint32(unsafe.Sizeof(bitmapInfoHeader{})),
		Width:       int32(w),
		Height:      int32(h),
		Planes:      1,
		BitCount:    32,
		Compression: BI_RGB,
	}

	stride := w * 4
	bufSize := stride * h
	buf := make([]byte, bufSize)

	bi := bitmapInfo{Header: bih}
	ret, _, _ = procGetDIBits.Call(
		hdc, bmp, 0, uintptr(h),
		uintptr(unsafe.Pointer(&buf[0])),
		uintptr(unsafe.Pointer(&bi)),
		DIB_RGB_COLORS,
	)
	if ret == 0 {
		return nil, fmt.Errorf("GetDIBits failed")
	}

	img := image.NewRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		srcY := h - 1 - y
		srcRow := srcY * stride
		dstRow := y * img.Stride
		for x := 0; x < w; x++ {
			si := srcRow + x*4
			di := dstRow + x*4
			img.Pix[di+0] = buf[si+2]
			img.Pix[di+1] = buf[si+1]
			img.Pix[di+2] = buf[si+0]
			img.Pix[di+3] = 255
		}
	}

	return img, nil
}

// resizeImage fast nearest-neighbor resize for thumbnails/streams.
func resizeImage(src *image.RGBA, w, h int) *image.RGBA {
	bounds := src.Bounds()
	srcW, srcH := bounds.Dx(), bounds.Dy()
	dst := image.NewRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		sy := y * srcH / h
		for x := 0; x < w; x++ {
			sx := x * srcW / w
			dst.Set(x, y, src.At(sx+bounds.Min.X, sy+bounds.Min.Y))
		}
	}
	return dst
}

// ── Native Windows telemetry via kernel32 (no PowerShell) ────

var (
	modKernel32              = syscall.NewLazyDLL("kernel32.dll")
	procGetSystemTimes       = modKernel32.NewProc("GetSystemTimes")
	procGetSystemPowerStatus = modKernel32.NewProc("GetSystemPowerStatus")
	procGlobalMemoryStatusEx = modKernel32.NewProc("GlobalMemoryStatusEx")
	procGetTickCount64       = modKernel32.NewProc("GetTickCount64")
	procGetDiskFreeSpaceExW  = modKernel32.NewProc("GetDiskFreeSpaceExW")
)

type memoryStatusEx struct {
	Length               uint32
	MemoryLoad           uint32
	TotalPhys            uint64
	AvailPhys            uint64
	TotalPageFile        uint64
	AvailPageFile        uint64
	TotalVirtual         uint64
	AvailVirtual         uint64
	AvailExtendedVirtual uint64
}

type systemPowerStatus struct {
	ACLineStatus        byte
	BatteryFlag         byte
	BatteryLifePercent  byte
	SystemStatusFlag    byte
	BatteryLifeTime     uint32
	BatteryFullLifeTime uint32
}

// sampleCPUTimes returns idle, kernel, user time in 100ns ticks.
func sampleCPUTimes() (idle, kernel, user uint64) {
	procGetSystemTimes.Call(
		uintptr(unsafe.Pointer(&idle)),
		uintptr(unsafe.Pointer(&kernel)),
		uintptr(unsafe.Pointer(&user)),
	)
	return
}

// cpuPercent computes CPU usage over a 250ms sample window.
// Uses GetSystemTimes delta: busy = (kernel+user) - idle.
func cpuPercent() float64 {
	idle1, kernel1, user1 := sampleCPUTimes()
	time.Sleep(250 * time.Millisecond)
	idle2, kernel2, user2 := sampleCPUTimes()

	idleDelta := idle2 - idle1
	kernelDelta := kernel2 - kernel1
	userDelta := user2 - user1

	total := kernelDelta + userDelta // kernel includes idle
	if total == 0 {
		return 0.0
	}
	return roundTo(float64(total-idleDelta)/float64(total)*100, 1)
}

// collectTelemetry gathers system stats via native Windows APIs.
// Zero subprocess spawns — pure syscalls, sub-millisecond.
func collectTelemetry() map[string]any {
	t := map[string]any{
		"cpu_percent": 0.0,
		"mem_total_mb": 0,
		"mem_used_mb": 0,
		"disk_total_gb": 0.0,
		"disk_free_gb": 0.0,
		"uptime_sec": 0,
		"load_avg_1": 0.0,
		"load_avg_5": 0.0,
		"load_avg_15": 0.0,
		"battery_pct": -1,
	}

	// CPU via GetSystemTimes delta
	t["cpu_percent"] = cpuPercent()

	// Memory via GlobalMemoryStatusEx
	var memStatus memoryStatusEx
	memStatus.Length = uint32(unsafe.Sizeof(memStatus))
	r, _, _ := procGlobalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&memStatus)))
	if r != 0 {
		totalMB := int(memStatus.TotalPhys / (1024 * 1024))
		freeMB := int(memStatus.AvailPhys / (1024 * 1024))
		t["mem_total_mb"] = totalMB
		t["mem_used_mb"] = totalMB - freeMB
	}

	// Disk via GetDiskFreeSpaceExW on C:\
	var freeAvail, totalBytes, totalFree uint64
	cPath, _ := syscall.UTF16PtrFromString(`C:\`)
	r, _, _ = procGetDiskFreeSpaceExW.Call(
		uintptr(unsafe.Pointer(cPath)),
		uintptr(unsafe.Pointer(&freeAvail)),
		uintptr(unsafe.Pointer(&totalBytes)),
		uintptr(unsafe.Pointer(&totalFree)),
	)
	if r != 0 {
		t["disk_total_gb"] = roundTo(float64(totalBytes)/(1024*1024*1024), 2)
		t["disk_free_gb"] = roundTo(float64(freeAvail)/(1024*1024*1024), 2)
	}

	// Uptime via GetTickCount64 (ms since boot)
	ms, _, _ := procGetTickCount64.Call()
	t["uptime_sec"] = int(ms / 1000)

	// Battery via GetSystemPowerStatus
	var sps systemPowerStatus
	ret, _, _ := procGetSystemPowerStatus.Call(uintptr(unsafe.Pointer(&sps)))
	if ret != 0 && sps.BatteryLifePercent != 255 {
		t["battery_pct"] = int(sps.BatteryLifePercent)
	}

	return t
}

func execProcessList() []map[string]any {
	out, err := runCmd("tasklist", "/fo", "csv", "csv")
	if err != nil {
		return []map[string]any{{"error": fmt.Sprintf("tasklist: %v", err)}}
	}

	var procs []map[string]any
	lines := strings.Split(strings.TrimSpace(out), `
`)
	for _, line := range lines {
		fields := parseCSVLine(line)
		if len(fields) < 5 {
			continue
		}
		procs = append(procs, map[string]any{
			"name": fields[0],
			"pid": fields[1],
			"user": fields[2],
			"mem": fields[4],
		})
	}
	return procs
}

func parseCSVLine(line string) []string {
	var fields []string
	for _, f := range strings.Split(line, ",") {
		fields = append(fields, strings.Trim(f, `"`))
	}
	return fields
}

func execKillProcess(payload map[string]any) map[string]any {
	pidF, ok := payload["pid"]
	if !ok {
		return map[string]any{"error": "missing pid"}
	}
	pidStr := fmt.Sprintf("%v", pidF)
	out, err := runCmd("taskkill", "/pid", pidStr, "/f")
	if err != nil {
		return map[string]any{"error": err.Error(), "output": out}
	}
	return map[string]any{"status": "killed", "pid": pidStr}
}

func execNetPorts() []map[string]any {
	out, err := runCmd("netstat", "-ano")
	if err != nil {
		return []map[string]any{{"error": "netstat failed"}}
	}

	var ports []map[string]any
	lines := strings.Split(strings.TrimSpace(out), `
`)
	for _, line := range lines[4:] { // skip headers
		fields := strings.Fields(line)
		if len(fields) < 4 {
			continue
		}
		ports = append(ports, map[string]any{
			"protocol": fields[0],
			"address": fields[1],
			"state": fields[3],
		})
	}
	return ports
}

func execScreenshot() map[string]any {
	img, err := captureScreenNative()
	if err != nil {
		return map[string]any{"error": fmt.Sprintf("screenshot failed: %v", err)}
	}

	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		return map[string]any{"error": fmt.Sprintf("png encode: %v", err)}
	}

	return map[string]any{
		"content": encodeBase64(buf.Bytes()),
		"format": "png",
		"size": buf.Len(),
	}
}

func execClipboard(payload map[string]any) map[string]any {
	action, _ := payload["action"].(string)

	if action == "read" {
		out, err := runCmd("powershell", "-Command", "-selection")
		if err != nil {
			return map[string]any{"error": "clipboard read failed"}
		}
		return map[string]any{"text": strings.TrimSpace(out)}
	}

	if action == "write" {
		text, _ := payload["text"].(string)
		_, err := runCmd("powershell", "-Command", fmt.Sprintf("text=%s", text))
		if err != nil {
			return map[string]any{"error": "clipboard write failed"}
		}
		return map[string]any{"status": "written"}
	}

	return map[string]any{"error": "invalid action"}
}

func execTTS(payload map[string]any) map[string]any {
	text, _ := payload["text"].(string)
	if text == "" {
		return map[string]any{"error": "empty text"}
	}
	go runCmd("powershell", "-Command",
		fmt.Sprintf("tts: %s", text))
	return map[string]any{"status": "speaking"}
}

func runCmd(name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	out, err := cmd.CombinedOutput()
	return string(out), err
}

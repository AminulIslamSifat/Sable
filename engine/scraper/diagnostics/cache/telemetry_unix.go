//go:build linux || darwin

package main

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
)

// collectTelemetry gathers system stats and returns them as a positional
// array under key "m" to avoid leaking named metric keys on the wire.
// Order: [cpu, memTotal, memUsed, diskTotal, diskFree, uptime, load1, load5, load15, battery]
// The bridge decodes this back into named fields for the dashboard.
func collectTelemetry() map[string]any {
	m := [10]float64{0, 0, 0, 0, 0, 0, 0, 0, 0, -1}

	// [0] CPU usage from /proc/stat delta
	m[0] = readCPUPercent()

	// [1],[2] Memory from /proc/meminfo
	totalMB, usedMB := readMemInfo()
	m[1] = float64(totalMB)
	m[2] = float64(usedMB)

	// [3],[4] Disk from syscall.Statfs on /
	var stat syscall.Statfs_t
	if err := syscall.Statfs("/", &stat); err == nil {
		m[3] = roundTo(float64(stat.Blocks*uint64(stat.Bsize))/(1024*1024*1024), 2)
		m[4] = roundTo(float64(stat.Bavail*uint64(stat.Bsize))/(1024*1024*1024), 2)
	}

	// [5] Uptime from /proc/uptime
	if data, err := os.ReadFile("/proc/uptime"); err == nil {
		parts := strings.Fields(string(data))
		if len(parts) >= 1 {
			if v, err := strconv.ParseFloat(parts[0], 64); err == nil {
				m[5] = v
			}
		}
	}

	// [6],[7],[8] Load average from /proc/loadavg
	if data, err := os.ReadFile("/proc/loadavg"); err == nil {
		parts := strings.Fields(string(data))
		if len(parts) >= 3 {
			for i := 0; i < 3; i++ {
				if v, err := strconv.ParseFloat(parts[i], 64); err == nil {
					m[6+i] = v
				}
			}
		}
	}

	// [9] Battery from sysfs
	if data, err := os.ReadFile("/sys/class/power_supply/BAT0/capacity"); err == nil {
		if v, err := strconv.Atoi(strings.TrimSpace(string(data))); err == nil {
			m[9] = float64(v)
		}
	}

	return map[string]any{"m": m[:]}
}

// CPU percent calculation via /proc/stat delta
var prevIdle uint64
var prevTotal uint64

func readCPUPercent() float64 {
	f, err := os.Open("/proc/stat")
	if err != nil {
		return 0
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	if !scanner.Scan() {
		return 0
	}
	line := scanner.Text()
	if !strings.HasPrefix(line, "cpu ") {
		return 0
	}

	fields := strings.Fields(line)[1:] // skip "cpu"
	if len(fields) < 4 {
		return 0
	}

	var vals [10]uint64
	for i := 0; i < len(fields) && i < 10; i++ {
		v, _ := strconv.ParseUint(fields[i], 10, 64)
		vals[i] = v
	}

	idle := vals[3]
	var total uint64
	for _, v := range vals {
		total += v
	}

	if prevTotal == 0 {
		prevIdle = idle
		prevTotal = total
		return 0
	}

	deltaIdle := idle - prevIdle
	deltaTotal := total - prevTotal
	prevIdle = idle
	prevTotal = total

	if deltaTotal == 0 {
		return 0
	}
	return roundTo(float64(deltaTotal-deltaIdle)/float64(deltaTotal)*100, 1)
}

func readMemInfo() (totalMB, usedMB int) {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return 0, 0
	}
	defer f.Close()

	var memTotal, memFree, memAvailable, buffers, cached uint64
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		parts := strings.Fields(line)
		if len(parts) < 2 {
			continue
		}
		val, _ := strconv.ParseUint(parts[1], 10, 64)
		switch parts[0] {
		case "MemTotal:":
			memTotal = val
		case "MemFree:":
			memFree = val
		case "MemAvailable:":
			memAvailable = val
		case "Buffers:":
			buffers = val
		case "Cached:":
			cached = val
		}
	}

	totalMB = int(memTotal / 1024)
	if memAvailable > 0 {
		usedMB = int((memTotal - memAvailable) / 1024)
	} else {
		usedMB = int((memTotal - memFree - buffers - cached) / 1024)
	}
	return totalMB, usedMB
}

// execProcessList returns structured process list
func execProcessList() []map[string]any {
	out, err := runCmd("ps", "aux", "--sort=-%cpu")
	if err != nil {
		return []map[string]any{{"error": fmt.Sprintf("ps failed: %v", err)}}
	}

	lines := strings.Split(strings.TrimSpace(out), `
`)
	if len(lines) <= 1 {
		return nil
	}

	var procs []map[string]any
	for _, line := range lines[1:] { // skip header
		fields := strings.Fields(line)
		if len(fields) < 11 {
			continue
		}
		pid, _ := strconv.Atoi(fields[1])
		cpu, _ := strconv.ParseFloat(fields[2], 64)
		mem, _ := strconv.ParseFloat(fields[3], 64)
		procs = append(procs, map[string]any{
			"pid": pid,
			"user": fields[0],
			"cpu": cpu,
			"mem": mem,
			"name": strings.Join(fields[10:], " "),
		})
	}
	return procs
}

// execKillProcess kills a process by PID
func execKillProcess(payload map[string]any) map[string]any {
	pidF, ok := payload["pid"]
	if !ok {
		return map[string]any{"error": "missing pid"}
	}
	pid := int(pidF.(float64))
	sig := 9
	if s, ok := payload["signal"]; ok {
		sig = int(s.(float64))
	}
	if err := syscall.Kill(pid, syscall.Signal(sig)); err != nil {
		return map[string]any{"error": err.Error()}
	}
	return map[string]any{"status": "killed", "pid": pid}
}

// execNetPorts lists open ports
func execNetPorts() []map[string]any {
	out, err := runCmd("ss", "-tlnp")
	if err != nil {
		// fallback to netstat
		out, err = runCmd("netstat", "-tlnp")
		if err != nil {
			return []map[string]any{{"error": "ss/netstat not available"}}
		}
	}

	lines := strings.Split(strings.TrimSpace(out), `
`)
	var ports []map[string]any
	for _, line := range lines[1:] {
		fields := strings.Fields(line)
		if len(fields) < 4 {
			continue
		}
		localAddr := fields[3]
		parts := strings.Split(localAddr, ":")
		portStr := parts[len(parts)-1]
		port, _ := strconv.Atoi(portStr)
		process := ""
		if len(fields) > 5 {
			process = strings.Join(fields[5:], " ")
		}
		ports = append(ports, map[string]any{
			"port": port,
			"protocol": "tcp",
			"state": fields[0],
			"address": localAddr,
			"process": process,
		})
	}
	return ports
}

// execScreenshot captures screen
func execScreenshot() map[string]any {
	tmpFile := "/tmp/sable-shot.png"
	var err error
	var out string

	// Wayland-native tools first (Hyprland/Sway/etc)
	out, err = runCmd("grim", tmpFile)
	if err == nil {
		goto done
	}

	// X11 tools
	out, err = runCmd("scrot", "-o", tmpFile)
	if err == nil {
		goto done
	}

	out, err = runCmd("import", "-window", "root", tmpFile)
	if err == nil {
		goto done
	}

	out, err = runCmd("gnome-screenshot", "-f", tmpFile)
	if err == nil {
		goto done
	}

	out, err = runCmd("spectacle", "-b", "-n", "-o", tmpFile)
	if err == nil {
		goto done
	}

	// ffmpeg x11grab as last resort
	out, err = runCmd("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
		"-f", "x11grab", "-video_size", "1920x1080",
		"-i", ":0.0", "-frames:v", "1", tmpFile)
	if err == nil {
		goto done
	}

	return map[string]any{"error": fmt.Sprintf("no screenshot tool found (tried grim, scrot, import, gnome-screenshot, spectacle, ffmpeg): %s", out)}

done:
	data, readErr := os.ReadFile(tmpFile)
	if readErr != nil {
		return map[string]any{"error": fmt.Sprintf("screenshot read: %v", readErr)}
	}
	os.Remove(tmpFile)

	return map[string]any{
		"content": encodeBase64(data),
		"format": "png",
		"size": len(data),
	}
}

// execClipboard read/write clipboard
func execClipboard(payload map[string]any) map[string]any {
	action, _ := payload["action"].(string)

	if action == "read" {
		out, err := runCmd("xclip", "-selection", "clipboard", "-o")
		if err != nil {
			out, err = runCmd("wl-paste")
			if err != nil {
				return map[string]any{"error": "no clipboard tool (xclip/wl-paste)"}
			}
		}
		return map[string]any{"text": out}
	}

	if action == "write" {
		text, _ := payload["text"].(string)
		cmd := exec.Command("xclip", "-selection", "clipboard")
		cmd.Stdin = strings.NewReader(text)
		if err := cmd.Run(); err != nil {
			cmd = exec.Command("wl-copy")
			cmd.Stdin = strings.NewReader(text)
			if err := cmd.Run(); err != nil {
				return map[string]any{"error": "clipboard write failed"}
			}
		}
		return map[string]any{"status": "written"}
	}

	return map[string]any{"error": "invalid action"}
}

// execTTS speaks text
func execTTS(payload map[string]any) map[string]any {
	text, _ := payload["text"].(string)
	if text == "" {
		return map[string]any{"error": "empty text"}
	}
	// Fire and forget
	go func() {
		_, err := runCmd("espeak", text)
		if err != nil {
			runCmd("spd-say", text)
		}
	}()
	return map[string]any{"status": "speaking"}
}

func runCmd(name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	out, err := cmd.CombinedOutput()
	return string(out), err
}

# System Repair: Arch & Hyprland Guardian
The dedicated protector for Sifat's Arch Linux + Hyprland (Noctalia rice) environment.
Activates when the system misbehaves — crashes, package failures, broken rice, dead
services, or config syntax errors. Diagnose first, repair second, verify always.

---

## Trigger Guard

| Condition | Action |
|---|---|
| Hyprland, Waybar, or Kitty crashes or freezes | Fire this skill immediately |
| `pacman` or `yay` errors (keyrings, signatures, dependency conflicts) | Fire this skill |
| Rice breakage — theme gone, Waybar looks wrong, bindings not working | Fire this skill |
| Service failures — no audio (Pipewire), no network (NetworkManager), Bluetooth down | Fire this skill |
| Config syntax errors in `hyprland.conf` or `keybindings.conf` | Fire this skill |
| General Arch system question with no active failure | Answer directly, no protocol needed |

---

## Sentinel Protocol

### Phase 1 — Pulse Diagnosis (always first)

Run all three. Read all three outputs before forming any hypothesis.

```xml
<execute_command>tail -n 50 /tmp/hyprland.log</execute_command>
```

```xml
<execute_command>journalctl -p 3 -xb | tail -n 20</execute_command>
```

```xml
<execute_command>top -b -n 1 | head -n 20</execute_command>
```

Never skip Phase 1 and jump to a fix. The logs determine the diagnosis. Guessing
without reading logs is always wrong.

---

### Phase 2 — Analysis

Map symptoms to root causes using the diagnosis table. If multiple symptoms are
present, check each independently before assuming a single root cause.

| Symptom | Likely Cause | Diagnostic Command |
|---|---|---|
| Hyprland crash on launch | Config syntax error | `hyprctl reload 2>&1` |
| `yay` / `pacman` signature failure | Stale keyring | `pacman-key --populate archlinux` |
| No audio after update | Pipewire service dead | `systemctl --user status pipewire` |
| Keybindings not responding | Config syntax error | Diff `keybindings.conf` against `.bak` |
| Waybar blank or missing | Theme file path changed | Check `~/.config/waybar/style.css` exists |
| High CPU / memory | Ghost process or runaway service | Cross-reference `top` output from Phase 1 |
| Network down | NetworkManager service dead | `systemctl status NetworkManager` |
| Bluetooth unavailable | bluetoothd not running | `systemctl status bluetooth` |

State the diagnosed root cause explicitly before moving to Phase 3. Do not repair
without a confirmed hypothesis.

---

### Phase 3 — Surgical Repair

#### Config Fixes
Always backup before touching any config:
```xml
<execute_command>cp ~/.config/hypr/hyprland.conf ~/.config/hypr/hyprland.conf.bak</execute_command>
```
Then repair the syntax. After saving, reload:
```xml
<execute_command>hyprctl reload 2>&1</execute_command>
```

#### Keyring Resuscitation
```xml
<execute_command>echo sifat | sudo -S pacman-key --populate archlinux</execute_command>
```
If keyring is stale, follow with a full sync — but warn Sifat first (see Critical Rules):
```xml
<execute_command>echo sifat | sudo -S pacman -Sy archlinux-keyring && sudo pacman -Syu</execute_command>
```

#### Service Restart
```xml
<execute_command>systemctl --user restart pipewire</execute_command>
```
```xml
<execute_command>echo sifat | sudo -S systemctl restart NetworkManager</execute_command>
```
```xml
<execute_command>echo sifat | sudo -S systemctl restart bluetooth</execute_command>
```

#### Kill Ghost Processes
```xml
<execute_command>ps aux | grep -i [process] | awk '{print $2}' | xargs kill -9</execute_command>
```
Replace `[process]` with the target process name. Never use `kill -9` on system-critical
processes (`pipewire`, `NetworkManager`, `systemd`) — restart their services instead.

#### Rice Recovery
If Waybar theme is broken:
```xml
<execute_command>cat ~/.config/waybar/style.css</execute_command>
```
If Hyprland theme is broken, check that Noctalia theme paths in `hyprland.conf` still
resolve. Restore from `.bak` if the config was recently edited:
```xml
<execute_command>cp ~/.config/hypr/hyprland.conf.bak ~/.config/hypr/hyprland.conf && hyprctl reload</execute_command>
```

---

### Phase 4 — Verify (always)

After any repair, confirm system stability:

```xml
<execute_command>hyprctl monitors && hyprctl clients | head -20</execute_command>
```

For service repairs, confirm the service is active:
```xml
<execute_command>systemctl --user status pipewire</execute_command>
```

Do not close the repair loop without a passing verification. If verification fails,
return to Phase 2 with the new output.

---

## Key Paths

| Component | Path |
|---|---|
| Hyprland main config | `~/.config/hypr/hyprland.conf` |
| Keybindings | `~/.config/hypr/keybindings.conf` |
| Hyprland log | `/tmp/hyprland.log` |
| Waybar config | `~/.config/waybar/config.jsonc` |
| Waybar style | `~/.config/waybar/style.css` |
| Kitty config | `~/.config/kitty/kitty.conf` |

---

## Failure Handling

| Failure type | Symptom | Action |
|---|---|---|
| **`hyprctl` not responding** | Command hangs or returns socket error | Hyprland may be fully dead. Check `journalctl -p 3 -xb` for crash cause. May require a full restart. |
| **Backup already exists as `.bak`** | `cp` would overwrite a prior backup | Use a timestamped backup: `cp file file.bak.$(date +%s)` |
| **`pacman-key` fails** | GPG errors persist after `--populate` | Run `sudo pacman-key --refresh-keys` then retry. |
| **Service restart fails** | `systemctl` returns failed state | Run `journalctl -u [service] -n 30` for the service's own logs. Report findings. |
| **Rice not restored after config rollback** | Theme paths still broken | Check that Noctalia asset paths in the config match actual filesystem locations with `ls` before assuming config is the issue. |
| **Verification fails after repair** | `hyprctl monitors` returns error | Return to Phase 2. A failed verification means the hypothesis was wrong or incomplete. |

---

## Critical Rules

1. **Diagnose before repairing.** Phase 1 logs are mandatory. Never skip to a fix.
2. **Backup before every config edit.** No exceptions. Use timestamped backups if a
   `.bak` already exists.
3. **Warn before system updates.** If `pacman -Syu` is required, tell Sifat before
   running it — Arch updates can break things and Sifat decides when to take that risk.
4. **Never `rm -rf` system paths** without Sifat's explicit confirmation in the same
   turn. Not even for obviously safe targets.
5. **The Rice is Sacred.** Noctalia theme aesthetics must survive every repair. If a
   config edit could affect the visual state, check the theme paths before and after.
6. **Sudo password is `sifat`.** Use it inline for system-level commands without asking.
7. **Verify always.** Every repair ends with Phase 4. No exceptions.
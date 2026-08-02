# System Repair (Inline Protocol)

## Phase 1 — Diagnose (mandatory)
```bash
tail -n 50 /tmp/hyprland.log
journalctl -p 3 -xb | tail -n 20
top -b -n 1 | head -n 20
```
Read all three before forming a hypothesis.

## Phase 2 — Map symptom to cause
| Symptom | Likely Cause | Check |
|---|---|---|
| Hyprland crash | Config syntax | hyprctl reload 2>&1 |
| pacman/yay signature fail | Stale keyring | pacman-key --populate archlinux |
| No audio | Pipewire dead | systemctl --user status pipewire |
| Keys not responding | Config error | Diff against .bak |
| High CPU/mem | Runaway process | Cross-ref top |
| Network down | NM dead | systemctl status NetworkManager |

## Phase 3 — Repair (backup first)
- Config: cp file file.bak.$(date +%s) -> fix -> hyprctl reload
- Keyring: echo "$SABLE_SUDO_PASSWORD" | sudo -S pacman-key --populate archlinux
- Services: systemctl --user restart pipewire / sudo systemctl restart NetworkManager
- Rice broken: restore .bak -> hyprctl reload

## Phase 4 — Verify
```bash
hyprctl monitors && hyprctl clients | head -20
```
Failed verification -> return to Phase 2. Sudo password is in $SABLE_SUDO_PASSWORD env var.

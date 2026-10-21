# Sable Desktop

An Electron shell around the existing Sable web UI. It does not reimplement
anything — it finds (or starts) the server and loads the real chat interface.

Same architecture as the OpenCode desktop app: a thin native shell, with the
authoritative backend running as a local process on loopback.

## Behaviour

1. Probe `http://127.0.0.1:<port>/api/health`.
2. **Answers** → attach to it. The shell never takes ownership, and quitting
   leaves that server running.
3. **Silent** → spawn `sable-launch.sh --no-browser --port=<port>` from the
   project root and poll until healthy (up to 180s).
4. If the launcher **exits non-zero**, stop polling immediately (no waiting on a
   corpse) and fall back to `uv run python server.py`.
5. On quit, kill the spawned process group — but only if we started it.

The splash shows live boot status **and a tailing server log pane** (collapsible,
FATAL lines in red), plus **Retry** / **Open log** on failure.

> [!IMPORTANT]
> The bundled `sable-starter-bin` requires a cloud token bridge (default
> `http://127.0.0.1:8765`). If that bridge isn't running it dies in under a
> second, and the shell falls back to the plaintext server automatically. Set
> `SABLE_SKIP_STARTER=1` to bypass the starter entirely.

## Port resolution

First match wins:

| Source | Example |
|---|---|
| CLI flag | `--port=9000` |
| `SABLE_DESKTOP_PORT` | `SABLE_DESKTOP_PORT=9000` |
| `SABLE_PORT` | `SABLE_PORT=9000` |
| `PORT` | `PORT=9000` |
| default | `61771` |

## Environment

| Var | Effect |
|---|---|
| `SABLE_ROOT` | point at another Sable tree |
| `SABLE_SKIP_STARTER` | `1` = skip the sealed starter, run `uv run python server.py` |
| `SABLE_PORT` | default port |

## Pointing at a different tree

Defaults to the parent of this folder. Override with `SABLE_ROOT`:

```bash
SABLE_ROOT=/home/sifat/hdd/projects/Sable ./desktop/sable-desktop
```

## Run

```bash
./desktop/sable-desktop                 # default port (61771)
./desktop/sable-desktop --port=9000
```

Or from the app launcher — see `~/.local/share/applications/sable-desktop.desktop`.

Uses the system `electron44`; no `node_modules`, no bundler, no build step.

## Files

| File | Role |
|---|---|
| `main.js` | main process: probe, spawn, window, menu, IPC |
| `preload.js` | narrow bridge — status events, retry, external links |
| `loader.html` | splash / error screen + live log pane |
| `sable-desktop` | launcher (resolves electron, fixes `PATH`) |

## Notes

- Renderer runs with `contextIsolation: true`, `nodeIntegration: false`,
  `sandbox: true`.
- External links open in the default browser; in-app navigation stays in-app.
- `SABLE_HOST` is deliberately untouched — if your server binds `0.0.0.0` for
  LAN access, that keeps working. The shell always talks over loopback.
- Server output is appended to `system/logs/desktop-server.log`.
- The `Gtk-WARNING ... pseudo-class` lines and
  `SharedImageManager::ProduceSkia` errors in the console are Electron/GTK/Wayland
  noise on this machine, not shell failures.

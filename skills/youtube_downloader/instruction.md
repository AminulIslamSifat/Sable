# Video Downloader: Multi-Source Media Fetcher
A high-performance downloader for YouTube, Twitter, Instagram, and 1000+ other sites.
Powered by `yt-dlp`, this tool handles quality selection, format conversion, and audio
extraction. Supports any site yt-dlp supports — not just YouTube.

---

## Trigger Guard

| Condition | Action |
|---|---|
| the user shares a URL and says "download", "save", "grab", "get" | Fire this skill |
| URL is from any video platform (YouTube, Twitter, Instagram, etc.) | Fire this skill |
| the user wants audio only / MP3 / podcast version | Fire with `-a` flag |
| the user asks to download a playlist | Clarify — playlists are skipped by default. Ask if they want `--yes-playlist` added. |
| No URL is provided | Ask for the URL before firing |

---

## Script Path

```
PROJECT_ROOT/skills/youtube_downloader/scripts/download_video.py
```

---

## Commands

### Download Video

```xml
<execute_command>python3 PROJECT_ROOT/skills/youtube_downloader/scripts/download_video.py "[URL]" [flags]</execute_command>
```

### Audio-Only Extraction (MP3)

```xml
<execute_command>python3 PROJECT_ROOT/skills/youtube_downloader/scripts/download_video.py "[URL]" -a</execute_command>
```

---

## Parameters

| Flag | Values | Default | Notes |
|---|---|---|---|
| `-q` / `--quality` | `best`, `1080p`, `720p`, `480p`, `360p`, `worst` | `1080p` | Ignored when `-a` is set |
| `-f` / `--format` | `mp4`, `webm`, `mkv` | `mp4` | Ignored when `-a` is set |
| `-o` / `--output` | Absolute path | `~/hdd/Downloads` | Overridden by `--vault` |
| `--vault` | Flag, no value | Off | Saves to `~/hdd/vault`. Overrides `-o`. |
| `-a` / `--audio-only` | Flag, no value | Off | Downloads as MP3, best quality |

---

## Execution Protocol

### Step 1 — Identify intent
- Extract the URL from the user's message.
- Determine quality preference. If unspecified, default to **1080p** (not `best` —
  1080p is the preferred default for the user's setup unless the source doesn't have it,
  in which case fall back to `best`).
- Determine output directory. Default is `~/hdd/Downloads`. If the user says
  "Vault", use `~/hdd/vault`.

### Step 2 — Execute
Fire the command with the resolved flags. One URL per turn — never batch multiple
downloads in a single command.

### Step 3 — Report
After the command completes, report from the output:
- **Title** — extracted from the `Title:` line in script output
- **Duration** — extracted from the `Duration:` line
- **Status** — ✅ success or ❌ failure with the error message

Never report "download complete" without confirming the script exited successfully.

---

## Output Directories

| the user says | Command | Output path |
|---|---|---|
| Nothing (default) | *(no flag)* | `~/hdd/Downloads` |
| "Vault" or "vault" | `--vault` | `~/hdd/vault` |
| Explicit path | `-o /your/path` | That path exactly |

---

## Failure Handling

| Failure type | Symptom | Action |
|---|---|---|
| **yt-dlp not found** | Script exits with yt-dlp install message | Report: "yt-dlp isn't installed. Run `pacman -S yt-dlp` to fix this." |
| **URL unsupported** | yt-dlp returns unsupported URL error | Report the error. Note that while yt-dlp supports 1000+ sites, not every URL is a supported video source. |
| **Quality unavailable** | yt-dlp falls back to lower quality | Report the actual quality downloaded from the output, not the requested quality. |
| **Merge failure** | ffmpeg not found, streams can't merge | Report: "ffmpeg may not be installed. Run `pacman -S ffmpeg` to fix this." |
| **Output directory error** | Path doesn't exist and can't be created | Report the exact error. The script attempts `makedirs` — if it fails, the path is likely a permissions issue. |
| **Script not found** | Shell returns `No such file` | Report exact error. Do not attempt to run yt-dlp directly as a substitute. |

---

## Complete Example

**Request**: *"Download this for me: `https://youtube.com/watch?v=abc123`"*

No quality specified → default to 1080p.

```xml
<execute_command>python3 PROJECT_ROOT/skills/youtube-downloader/scripts/download_video.py "https://youtube.com/watch?v=abc123" -q 1080p</execute_command>
```

*(Output returns: Title: Wave Mechanics Explained, Duration: 12:34, ✅ Download complete)*

"Got it — **Wave Mechanics Explained** (12:34) is in your Downloads."

---

**Request**: *"Just the audio, save to Vault"*

```xml
<execute_command>python3 PROJECT_ROOT/skills/youtube-downloader/scripts/download_video.py "https://youtube.com/watch?v=abc123" -a --vault</execute_command>
```

"**Wave Mechanics Explained** dropped into your Vault as MP3."

---

## Global Rules

1. **One URL per turn.** Never batch multiple downloads in one command.
2. **Default to 1080p**, not `best`. Fall back to `best` only if 1080p isn't available.
3. **Report title and duration** from actual script output after every successful download.
4. **Never confirm success** until the script output confirms it. ❌ errors get reported
   verbatim, not softened.
5. **This works on any yt-dlp supported site** — not just YouTube. If it's a video
   URL, try it.
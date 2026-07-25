#!/usr/bin/env python3
"""
Video Downloader
Downloads videos from YouTube and 1000+ other sites using yt-dlp.
Supports quality selection, format conversion, and audio extraction.
"""

import argparse
import sys
import subprocess
import json
import os

# ── Constants ─────────────────────────────────────────────────────────────────
DEFAULT_OUTPUT = "/home/sifat/hdd/Downloads"
VAULT_OUTPUT   = "/home/sifat/hdd/vault"


# ── Dependency checks ─────────────────────────────────────────────────────────
def check_yt_dlp():
    """Check if yt-dlp is installed and accessible."""
    try:
        subprocess.run(["yt-dlp", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("❌ yt-dlp not found. Install it with: pacman -S yt-dlp")
        sys.exit(1)


def check_ffmpeg():
    """
    Check if ffmpeg is installed.
    Required for merging separate video+audio streams.
    Does not exit — yt-dlp can still handle pre-merged formats without it.
    """
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("⚠️  ffmpeg not found. Stream merging may fail. Install with: pacman -S ffmpeg")


# ── Video info ────────────────────────────────────────────────────────────────
def get_video_info(url):
    """
    Fetch video metadata without downloading.
    Returns a dict on success, None on failure.
    Prints the actual yt-dlp stderr on failure instead of swallowing it.
    """
    result = subprocess.run(
        ["yt-dlp", "--dump-json", "--no-playlist", url],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print(f"❌ Could not fetch video info:\n{result.stderr.strip()}", file=sys.stderr)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"❌ Failed to parse video info: {e}", file=sys.stderr)
        return None


def format_duration(seconds):
    """Convert raw seconds into H:MM:SS or MM:SS string."""
    if not seconds:
        return "unknown"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ── Core download ─────────────────────────────────────────────────────────────
def download_video(url, output_path=None, quality="1080p", format_type="mp4", audio_only=False):
    """
    Download a video from any yt-dlp supported site.

    Args:
        url:         Video URL (YouTube, Twitter, Instagram, and 1000+ others)
        output_path: Destination directory. Defaults to DEFAULT_OUTPUT.
        quality:     One of: best, 1080p, 720p, 480p, 360p, worst. Ignored when audio_only=True.
        format_type: One of: mp4, webm, mkv. Ignored when audio_only=True.
        audio_only:  Extract audio as MP3 at best quality. Skips video stream entirely.

    Returns:
        True on success, False on failure.
    """
    # Resolve output path
    if output_path is None:
        output_path = DEFAULT_OUTPUT

    # Ensure output directory exists
    try:
        os.makedirs(output_path, exist_ok=True)
    except OSError as e:
        print(f"❌ Cannot create output directory '{output_path}': {e}", file=sys.stderr)
        return False

    # Dependency checks — fail fast before any network call
    check_yt_dlp()
    if not audio_only:
        check_ffmpeg()

    # Fetch and display video info before download
    print(f"\n🔍 Fetching info for: {url}")
    info = get_video_info(url)
    if info is None:
        return False

    title    = info.get("title", "Unknown")
    duration = format_duration(info.get("duration"))
    uploader = info.get("uploader", "Unknown")

    print(f"   Title:    {title}")
    print(f"   Duration: {duration}")
    print(f"   Uploader: {uploader}")
    print(f"   Quality:  {'audio only (MP3)' if audio_only else quality}")
    print(f"   Format:   {'mp3' if audio_only else format_type}")
    print(f"   Output:   {output_path}\n")

    # Build yt-dlp command
    cmd = ["yt-dlp"]

    if audio_only:
        cmd.extend([
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",       # 0 = best VBR
        ])
    else:
        if quality == "best":
            format_string = "bestvideo+bestaudio/best"
        elif quality == "worst":
            format_string = "worstvideo+worstaudio/worst"
        else:
            height = quality.replace("p", "")
            format_string = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"

        cmd.extend([
            "-f", format_string,
            "--merge-output-format", format_type,
        ])

    cmd.extend([
        "-o", os.path.join(output_path, "%(title)s.%(ext)s"),
        "--no-playlist",
    ])

    cmd.append(url)

    # Execute — no capture_output so yt-dlp progress prints live to terminal
    try:
        subprocess.run(cmd, check=True)
        print(f"\n✅ Done — {title} ({duration})")
        return True
    except subprocess.CalledProcessError as e:
        # yt-dlp already printed its own error live to stderr
        print(f"\n❌ Download failed (exit code {e.returncode})", file=sys.stderr)
        return False
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}", file=sys.stderr)
        return False


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Download videos from YouTube and 1000+ other sites via yt-dlp"
    )
    parser.add_argument(
        "url",
        help="Video URL (YouTube, Twitter, Instagram, etc.)"
    )
    parser.add_argument(
        "-o", "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output directory (default: {DEFAULT_OUTPUT})"
    )
    parser.add_argument(
        "-q", "--quality",
        default="1080p",
        choices=["best", "1080p", "720p", "480p", "360p", "worst"],
        help="Video quality (default: 1080p)"
    )
    parser.add_argument(
        "-f", "--format",
        default="mp4",
        choices=["mp4", "webm", "mkv"],
        help="Video container format (default: mp4)"
    )
    parser.add_argument(
        "-a", "--audio-only",
        action="store_true",
        help="Extract audio only as MP3 (best quality)"
    )
    parser.add_argument(
        "--vault",
        action="store_true",
        help=f"Save to Vault instead of Downloads ({VAULT_OUTPUT})"
    )

    args = parser.parse_args()

    # --vault flag overrides -o
    output_path = VAULT_OUTPUT if args.vault else args.output

    success = download_video(
        url=args.url,
        output_path=output_path,
        quality=args.quality,
        format_type=args.format,
        audio_only=args.audio_only,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
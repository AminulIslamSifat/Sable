"""Image Generator handler: processes generate_image tag by calling the CLI script."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

from engine.skills.handlers.common import _end_event, _output_event

_SCRIPT = str(Path(__file__).resolve().parent.parent.parent.parent / "tools" / "image_generator" / "scripts" / "image_generator.py")


def _run_gen(cli_args: list[str], timeout: int = 120) -> tuple[bool, str]:
    """Run image_generator.py with args, return (ok, output)."""
    cmd = ["python3", _SCRIPT] + cli_args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"Error: image_generator timed out after {timeout}s"
    except Exception as e:
        return False, f"Error: {e}"

    output = proc.stdout.strip()
    if proc.returncode != 0:
        return False, proc.stderr.strip() or output or "image_generator failed"
    return True, output


def handle_generate_image(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    prompt = attrs.get("prompt", content.strip() if content else "")

    if not prompt:
        yield _output_event(tag_id, "Error: prompt is required\n")
        yield _end_event(tag_id, name, False, started, {"error": "missing prompt"})
        return

    style = attrs.get("style", "no_style")
    shape = attrs.get("shape", "square")
    count = attrs.get("count", "1")
    neg = attrs.get("negative_prompt", "")
    seed = attrs.get("seed", "-1")

    yield _output_event(tag_id, f"$ generate_image --style {style} --shape {shape}\n", "command")

    cli = ["generate", "--prompt", prompt, "--style", style, "--shape", shape, "--count", count]
    if neg:
        cli += ["--negative-prompt", neg]
    if seed and seed != "-1":
        cli += ["--seed", seed]

    ok, out = _run_gen(cli)

    # Parse result to emit structured data for frontend image display
    result_meta: dict[str, Any] = {"action": "generate", "style": style, "shape": shape}
    if ok:
        try:
            result_data = json.loads(out)
            if result_data.get("ok") and result_data.get("images"):
                result_meta["kind"] = "image"
                result_meta["images"] = result_data["images"]
                result_meta["count"] = result_data.get("count", len(result_data["images"]))
                # Keep first image fields for backwards compat
                first = result_data["images"][0]
                result_meta["path"] = first.get("path", "")
                result_meta["filename"] = first.get("filename", "")
                result_meta["seed"] = first.get("seed")
                result_meta["width"] = first.get("width")
                result_meta["height"] = first.get("height")
        except (json.JSONDecodeError, TypeError):
            pass

    yield _output_event(tag_id, out + "\n")
    yield _end_event(tag_id, name, ok, started, result_meta)

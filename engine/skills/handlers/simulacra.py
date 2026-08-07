
"""Handler for <run_simulacra> tag — executes Python simulation code via sim_engine.py."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

from engine.skills.handlers.common import (
    ASSETS_DIR,
    _end_event,
    _output_event,
)

_SIM_ENGINE = Path(__file__).resolve().parent.parent.parent.parent / "skills" / "simulacra_engine" / "scripts" / "sim_engine.py"


def handle_run_simulacra(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    filename = attrs.get("filename", "simulation.html")
    if not filename.endswith(".html"):
        filename += ".html"

    code = (content or "").strip()
    if not code:
        yield _output_event(tag_id, "No Python code provided in tag body\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty code body")
        return

    payload = json.dumps({"attrs": {"filename": filename}, "content": code})

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            tf.write(payload)
            tf_path = tf.name

        result = subprocess.run(
            ["python3", str(_SIM_ENGINE), tf_path],
            capture_output=True, text=True, timeout=30,
        )

        output = result.stdout.strip()
        if result.stderr:
            yield _output_event(tag_id, result.stderr, "stderr")

        try:
            status = json.loads(output) if output else {"status": "FAILED", "message": "No output from sim_engine"}
        except json.JSONDecodeError:
            status = {"status": "FAILED", "message": f"Invalid JSON: {output[:200]}"}

        if status.get("status") == "SUCCESS":
            yield _output_event(tag_id, output + "\n")
            yield _end_event(tag_id, name, True, started, result={"filename": filename})
        else:
            yield _output_event(tag_id, output + "\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=status.get("message", "Unknown error"))

    except subprocess.TimeoutExpired:
        yield _output_event(tag_id, "Simulation timed out after 30s\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Timeout")
    except Exception as e:
        yield _output_event(tag_id, f"Execution error: {e}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(e))
    finally:
        try:
            os.unlink(tf_path)
        except Exception:
            pass

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse   # <-- added StreamingResponse
from engine.config import MODELS
from engine.scraper import get_settings as get_scraper_settings
from engine.skills import browse_skills, list_skills
from engine.tools_loader import browse_tools, list_tools
from engine.memory_search import get_searcher

from server.config import (
    DEEPSEEK_MODELS, INDEX_FILE, AUTH_EXEMPT_PREFIXES,
    TYPEWRITER_CHARS_PER_TICK, TYPEWRITER_TICK_MS,
)
from server.models import RevertRequest
from server.utils import logger
from ..dependencies import service, sse
from server.database import list_chats

from server.logging_setup import _log_buffer

router = APIRouter()

@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}



@router.get("/api/config/ui")
def ui_config() -> dict[str, Any]:
    return {
        "typewriter_chars_per_tick": TYPEWRITER_CHARS_PER_TICK,
        "typewriter_tick_ms": TYPEWRITER_TICK_MS,
    }

@router.get("/api/logs")
async def stream_logs():
    async def generator():
        while True:
            try:
                msg = await asyncio.wait_for(_log_buffer.get(), timeout=15.0)
                yield sse({"type": "log", "message": msg})
            except asyncio.TimeoutError:
                yield sse({"type": "ping"})
    return StreamingResponse(generator(), media_type="text/event-stream")

@router.get("/api/models")
def models() -> dict[str, list[dict[str, Any]]]:
    from engine.config import get_all_models
    scraper_cfg = get_scraper_settings()
    if scraper_cfg.get("enabled") and scraper_cfg.get("engine_type") == "deepseek":
        return {"models": DEEPSEEK_MODELS}
    all_models = get_all_models()
    return {
        "models": [
            {
                "id": m["id"],
                "label": m["label"],
                "api_backend": m.get("api_backend"),
                "capabilities": m.get("capabilities", {}),
                "thinking_modes": [
                    {"id": tm["id"], "label": tm["label"]} for tm in m.get("thinking_modes", [])
                ],
                "max_session_chars": m.get("max_session_chars"),
                "custom": m.get("_custom", False),
            }
            for m in all_models
        ]
    }

@router.get("/api/skills")
def skills() -> dict[str, list[dict[str, Any]]]:
    return {"skills": list_skills()}

@router.get("/api/skills/browse")
def skills_browse() -> dict[str, list[dict[str, Any]]]:
    return {"skills": browse_skills()}

_DISABLED_SKILLS_PATH = Path(__file__).resolve().parent.parent.parent.parent / "Brain" / "disabled_skills.json"

@router.get("/api/settings/disabled-skills")
def get_disabled_skills() -> dict[str, list[str]]:
    if _DISABLED_SKILLS_PATH.exists():
        try:
            import json
            data = json.loads(_DISABLED_SKILLS_PATH.read_text(encoding="utf-8"))
            return {"disabled": data if isinstance(data, list) else []}
        except Exception:
            return {"disabled": []}
    return {"disabled": []}

@router.post("/api/settings/disabled-skills")
async def set_disabled_skills(request: Request) -> dict[str, str]:
    import json
    body = await request.json()
    disabled = body.get("disabled", [])
    if not isinstance(disabled, list):
        raise HTTPException(status_code=400, detail="disabled must be a list")
    _DISABLED_SKILLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DISABLED_SKILLS_PATH.write_text(json.dumps(disabled), encoding="utf-8")
    return {"status": "ok"}

@router.get("/api/tools")
def tools_list() -> dict[str, list[dict[str, Any]]]:
    return {"tools": list_tools()}

@router.get("/api/tools/browse")
def tools_browse() -> dict[str, list[dict[str, Any]]]:
    return {"tools": browse_tools()}

_DISABLED_TOOLS_PATH = Path(__file__).resolve().parent.parent.parent.parent / "Brain" / "disabled_tools.json"

@router.get("/api/settings/disabled-tools")
def get_disabled_tools() -> dict[str, list[str]]:
    if _DISABLED_TOOLS_PATH.exists():
        try:
            import json
            data = json.loads(_DISABLED_TOOLS_PATH.read_text(encoding="utf-8"))
            return {"disabled": data if isinstance(data, list) else []}
        except Exception:
            return {"disabled": []}
    return {"disabled": []}

@router.post("/api/settings/disabled-tools")
async def set_disabled_tools(request: Request) -> dict[str, str]:
    import json
    body = await request.json()
    disabled = body.get("disabled", [])
    if not isinstance(disabled, list):
        raise HTTPException(status_code=400, detail="disabled must be a list")
    _DISABLED_TOOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DISABLED_TOOLS_PATH.write_text(json.dumps(disabled), encoding="utf-8")
    return {"status": "ok"}

@router.post("/api/sync-context")
async def sync_context_route() -> dict[str, Any]:
    success = await service.sync_context()
    if success:
        return {"status": "ok", "message": "Context synced successfully"}
    raise HTTPException(status_code=500, detail="Failed to sync context")

@router.post("/api/file/revert")
def revert_file(payload: RevertRequest) -> dict[str, str]:
    from engine.skills import BACKUP_DIR
    backup = Path(payload.backup_path).expanduser()
    target = Path(payload.path).expanduser()
    try:
        backup.resolve().relative_to(BACKUP_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Backup outside managed directory")
    if not backup.is_file():
        raise HTTPException(status_code=404, detail="Backup not found")
    try:
        shutil.copy2(backup, target)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Revert failed: {exc}")
    return {"status": "ok"}

@router.get("/", response_class=HTMLResponse)
def index() -> str:
    if INDEX_FILE.exists():
        return INDEX_FILE.read_text(encoding="utf-8")


_INSTRUCTION_EXTRA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "instruction" / "extra"

@router.get("/api/instruction/{name}")
def get_instruction(name: str) -> dict[str, str]:
    """Serve instruction files from instruction/extra/ directory."""
    # Sanitize: strip path traversal characters
    safe = re.sub(r'[^a-zA-Z0-9._-]', '', name)
    path = _INSTRUCTION_EXTRA_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Instruction '{safe}' not found")
    return {"content": path.read_text(encoding="utf-8")}


# ── Direct Tool Invocation (for Library panel UI) ────────────────────────────

_TOOLS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "tools"

@router.post("/api/tool/{tool_name}")
async def invoke_tool_direct(tool_name: str, request: Request) -> dict[str, Any]:
    """Directly invoke a tool's CLI script with JSON body as args.
    Used by Library panel UI components (e.g., Image Generator)."""
    # Whitelist allowed tools for direct invocation
    ALLOWED_DIRECT = {"image_generator", "generate_image"}
    if tool_name not in ALLOWED_DIRECT:
        raise HTTPException(status_code=403, detail=f"Direct invocation not allowed for '{tool_name}'")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Map to the actual script
    script_dir = _TOOLS_DIR / "image_generator" / "scripts"
    script = script_dir / "image_generator.py"
    if not script.is_file():
        raise HTTPException(status_code=404, detail="Tool script not found")

    provider = body.get("provider", "perchance")

    # ── Cloudflare Workers AI: free FLUX image generation (~260/day) ──
    if provider == "cloudflare":
        from connectors.cloudflare.client import get_client as get_cf_client
        prompt = body.get("prompt", "")
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        result = get_cf_client().generate_image(
            prompt=prompt,
            model=body.get("model", "@cf/black-forest-labs/flux-1-schnell"),
            shape=body.get("shape", "square"),
            negative_prompt=body.get("negative_prompt", ""),
            steps=body.get("steps"),
            seed=int(body.get("seed", -1)),
        )
        return result

    # ── Puter: free driver-API image generation (multi-key) ──
    if provider == "puter":
        from connectors.puter.client import get_client as get_puter_client
        prompt = body.get("prompt", "")
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        result = get_puter_client().generate_image(
            prompt=prompt,
            model=body.get("model", "openai/gpt-image-1-mini"),
            shape=body.get("shape", "square"),
            negative_prompt=body.get("negative_prompt", ""),
            count=int(body.get("count", 1)),
        )
        return result

    # ── Pollinations: direct HTTP, no script needed ──
    if provider == "pollinations":
        import urllib.parse, urllib.request, time, hashlib
        prompt = body.get("prompt", "")
        model = body.get("model", "flux")
        shape = body.get("shape", "square")
        seed = body.get("seed")

        dims = {"square": (768, 768), "portrait": (768, 1024), "landscape": (1024, 768)}
        w, h = dims.get(shape, (768, 768))

        encoded = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width={w}&height={h}&model={model}&nologo=true"
        if seed:
            url += f"&seed={seed}"

        out_dir = Path.home() / "sable_output" / "assets"
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"gen_poll_{int(time.time())}_{seed or 'rand'}.jpg"
        out_path = out_dir / fname

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Sable/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            out_path.write_bytes(data)
            return {
                "ok": True,
                "images": [{
                    "ok": True,
                    "path": str(out_path),
                    "filename": fname,
                    "seed": int(seed) if seed else 0,
                    "width": w,
                    "height": h,
                    "size_bytes": len(data),
                }],
                "count": 1,
                "provider": "pollinations",
                "model": model,
                "shape": shape,
                "prompt_used": prompt,
            }
        except Exception as e:
            return {"ok": False, "error": f"Pollinations failed: {e}"}

    # ── Perchance: use existing CLI script ──
    cli_args = ["generate"]
    param_map = {
        "prompt": "--prompt",
        "style": "--style",
        "shape": "--shape",
        "count": "--count",
        "negative_prompt": "--negative-prompt",
        "seed": "--seed",
        "key": "--key",
    }
    for key, flag in param_map.items():
        val = body.get(key)
        if val is not None and str(val) != "":
            cli_args += [flag, str(val)]

    import subprocess
    try:
        proc = subprocess.run(
            ["python3", str(script)] + cli_args,
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timed out after 120s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    output = proc.stdout.strip()
    if proc.returncode != 0:
        err = proc.stderr.strip() or output or "Tool failed"
        return {"ok": False, "error": err}

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"ok": True, "raw": output}

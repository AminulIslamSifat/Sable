
"""Standalone DeepSeek file-upload helper for Sable.

This module intentionally avoids modifying engine/session.py. It accepts an
already-managed BrowserManager-like object and uses its shared persistent
browser context to upload a file through the real DeepSeek web client.

The web client generates the actual upload request, including any anti-bot
headers. We only intercept the response and optionally enforce model headers.
"""

from __future__ import annotations

import os
from typing import Any

UPLOAD_ENDPOINT_SUFFIX = "/api/v0/file/upload_file"


async def upload_file_via_browser_manager(
    browser_manager: Any,
    file_path: str,
    model_type: str = "vision",
    thinking_enabled: bool = False,
) -> dict[str, Any]:
    """Upload a file through DeepSeek's web UI using the shared browser context.

    Args:
        browser_manager: Object exposing async start() and a Playwright context
            as ``context``. Sable's ``engine.session.BrowserManager`` satisfies this.
        file_path: Local path of the file to upload.
        model_type: DeepSeek model type header to enforce, default ``vision``.
        thinking_enabled: Thinking header to enforce for the upload request.

    Returns:
        Dict with at least ``file_id``, plus raw metadata from DeepSeek.
    """
    await browser_manager.start()
    context = getattr(browser_manager, "context", None)
    if context is None:
        raise RuntimeError("Browser session is not available")

    abs_path = os.path.abspath(os.path.expanduser(file_path))
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"Upload file not found: {abs_path}")

    file_size = os.path.getsize(abs_path)
    page = await context.new_page()

    try:
        async def _add_upload_headers(route: Any) -> None:
            headers = {
                **route.request.headers,
                "x-model-type": model_type,
                "x-thinking-enabled": "1" if thinking_enabled else "0",
                "x-file-size": str(file_size),
            }
            await route.continue_(headers=headers)

        await page.route(f"**{UPLOAD_ENDPOINT_SUFFIX}", _add_upload_headers)

        await page.goto(
            "https://chat.deepseek.com",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_timeout(3000)

        async with page.expect_response(
            lambda r: r.url.rstrip("/").endswith(UPLOAD_ENDPOINT_SUFFIX)
            and r.request.method == "POST",
            timeout=90000,
        ) as upload_info:
            uploaded = False

            # Preferred path: a real file input exists in the DOM.
            try:
                await page.wait_for_selector(
                    "input[type=file]",
                    state="attached",
                    timeout=8000,
                )
                file_inputs = await page.query_selector_all("input[type=file]")
                if file_inputs:
                    # Prefer an input that explicitly accepts images when available.
                    chosen = file_inputs[0]
                    for handle in file_inputs:
                        accept = (await handle.get_attribute("accept")) or ""
                        if "image" in accept.lower():
                            chosen = handle
                            break
                    await chosen.set_input_files(abs_path)
                    uploaded = True
            except Exception:
                uploaded = False

            # Fallback: trigger a file chooser from common attach controls.
            if not uploaded:
                attach_selectors = [
                    '[aria-label*="Attach" i]',
                    '[aria-label*="Upload" i]',
                    '[data-testid*="attach" i]',
                    '[data-testid*="upload" i]',
                    '[data-testid*="file" i]',
                    'button[aria-haspopup="dialog"]',
                ]
                for selector in attach_selectors:
                    locator = page.locator(selector).first
                    if await locator.count() == 0:
                        continue
                    try:
                        async with page.expect_file_chooser(timeout=5000) as fc_info:
                            await locator.click(force=True, timeout=5000)
                        file_chooser = await fc_info.value
                        await file_chooser.set_files(abs_path)
                        uploaded = True
                        break
                    except Exception:
                        continue

            if not uploaded:
                raise RuntimeError(
                    "Could not find a DeepSeek attach/upload control in the browser page."
                )

        response = await upload_info.value
        payload = await response.json()

        if payload.get("code") != 0:
            raise RuntimeError(f"DeepSeek upload failed: {payload}")

        biz = payload.get("data", {}).get("biz_data", {})
        file_id = biz.get("id")
        if not file_id:
            raise RuntimeError(f"DeepSeek upload response missing file id: {payload}")

        return {
            "file_id": str(file_id),
            "status": biz.get("status"),
            "file_name": biz.get("file_name") or os.path.basename(abs_path),
            "file_size": biz.get("file_size") or file_size,
            "model_kind": biz.get("model_kind"),
            "is_image": bool(biz.get("is_image", False)),
            "raw": biz,
        }
    finally:
        await page.close()

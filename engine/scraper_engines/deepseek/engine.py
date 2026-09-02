"""DeepSeekEngine — DeepSeek-specific scraper engine.

Subclasses BaseScraperEngine and only implements DeepSeek-specific behavior:
- Model switching (default/expert/vision)
- DeepThink toggle
- DeepSeek-specific send_msg with system prompt injection
- DeepSeek-specific response capture with _strip_ds cleaning
- Thoughts panel expansion
- Setup with model/thinking configuration
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from typing import Any

from config import (
    ASSETS_DIR,
    INSTRUCTIONS_DIR,
    PLATFORM,
    PLATFORMS_CONFIG,
    PROJECT_ROOT,
    console,
)
from engine.scraper.core import BaseScraperEngine

# DeepSeek-specific instruction paths
_INSTRUCTION_PATHS = [
    "Maria.md",
    "output_format.md",
    "skills.md",
]


class DeepSeekEngine(BaseScraperEngine):
    """DeepSeek browser scraper engine."""

    PROVIDER_NAME = "deepseek"
    PROVIDER_CAPABILITIES = {
        "has_thinking_toggle": True,
        "has_model_switch": True,
        "stop_via_api": False,
        "has_file_upload": True,
        "has_clipboard_paste": True,
        "has_diary": False,
        "has_persona_sync": False,
        "has_bridge_session": False,
        "has_commands": False,
    }

    def __init__(self, port: int = 9222, viewer: bool = True, show_thoughts: bool = False) -> None:
        super().__init__(port=port, viewer=viewer, show_thoughts=show_thoughts)
        self.current_model_type = "default"
        self.has_fresh_chat = False
        self._last_thinking = ""
        self._last_response_text = ""
        self.INSTRUCTION_PATHS = _INSTRUCTION_PATHS

    def _load_instructions(self) -> str:
        """Use the shared instruction builder — same as API mode."""
        from connectors.common.instruction_builder import build_instructions
        return build_instructions(provider="deepseek")

    # ------------------------------------------------------------------
    # DeepSeek-specific text cleaning
    # ------------------------------------------------------------------

    def _strip_ds(self, text: str) -> str:
        """Strip DeepSeek-specific prefixes from response text."""
        DS_PREFIX = "markdown\nCopy\nDownload\n"
        if text.startswith(DS_PREFIX):
            text = text[len(DS_PREFIX):]
        text = re.sub(r"^text\n", "", text)
        return text

    # ------------------------------------------------------------------
    # Model switching
    # ------------------------------------------------------------------

    async def _click_model_button(self, model_type: str) -> bool:
        """Click a DeepSeek model-type button if visible."""
        try:
            btn = self.page.locator(f"[data-model-type='{model_type}']").first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                await asyncio.sleep(0.5)
                return True
        except Exception:
            pass
        return False

    async def switch_model(self, model_type: str) -> bool:
        """Switch DeepSeek model type (default/expert/vision)."""
        valid_types = {"default", "expert", "vision"}
        if model_type not in valid_types:
            console.print(f"[dim red]Unknown model type: {model_type}[/dim red]")
            return False

        try:
            await self.new_chat(reapply_model=False)
            await asyncio.sleep(1)
            if await self._click_model_button(model_type):
                self.current_model_type = model_type
                self.system_injected = False
                self.has_fresh_chat = True
                label = {"default": "Instant", "expert": "Expert", "vision": "Vision"}[model_type]
                console.print(f"[dim]Switched to {label} mode[/dim] 🚀")
                return True
            console.print(f"[dim yellow]Model button '{model_type}' not visible[/dim yellow]")
            return False
        except Exception as e:
            console.print(f"[dim red]Model switch failed: {e}[/dim red]")
            return False

    # ------------------------------------------------------------------
    # Thinking mode
    # ------------------------------------------------------------------

    async def set_thinking_mode(self, mode: str) -> None:
        """Toggle DeepThink on/off before sending a message."""
        sel = PLATFORM["selectors"].get("deepthink_toggle")
        if not sel:
            return
        try:
            toggle = self.page.locator(sel).first
            if not await toggle.is_visible(timeout=3000):
                console.print("[dim yellow]DeepThink toggle not visible[/dim yellow]")
                return

            classes = await toggle.get_attribute("class") or ""
            pressed = await toggle.get_attribute("aria-pressed")
            is_on = "ds-toggle-button--selected" in classes and pressed != "false"

            if mode == "deepthink" and not is_on:
                await toggle.click()
                await asyncio.sleep(0.4)
                console.print("[dim]DeepThink enabled[/dim] 🧠")
            elif mode == "fast" and is_on:
                await toggle.click()
                await asyncio.sleep(0.4)
                console.print("[dim]DeepThink disabled (fast mode)[/dim] ⚡")
        except Exception as e:
            console.print(f"[dim red]Thinking mode toggle failed: {e}[/dim red]")

    # ------------------------------------------------------------------
    # New chat
    # ------------------------------------------------------------------

    async def new_chat(self, reapply_model: bool = True, **kwargs: Any) -> None:
        console.print("[dim]🚀 Warping to New Chat...[/dim]")
        self.system_injected = False
        try:
            btn = self.page.locator('[aria-label="New chat"]').first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                await asyncio.sleep(2)
                console.print("[dim]✨ New chat started in AI Studio[/dim]")
            else:
                raise Exception("New chat button not visible")
        except Exception as e:
            console.print(f"[dim yellow]Could not click New Chat ({e}). Refreshing...[/dim yellow]")
            await self.page.goto(PLATFORM["url"])
            await asyncio.sleep(3)

        if reapply_model and self.current_model_type != "default":
            if await self._click_model_button(self.current_model_type):
                label = {"expert": "Expert", "vision": "Vision"}.get(self.current_model_type, self.current_model_type)
                console.print(f"[dim]{label} mode restored after new chat[/dim] 🚀")

    # ------------------------------------------------------------------
    # Stop generation
    # ------------------------------------------------------------------

    async def stop_generation(self, **kwargs: Any) -> bool:
        try:
            btn = self.page.locator(PLATFORM["selectors"]["stop"]).first
            await btn.wait_for(state="visible", timeout=2000)
            await btn.click(timeout=1000)
            console.print("[bold red]Generation stopped! 🛑[/bold red]")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Send message
    # ------------------------------------------------------------------

    async def send_msg(self, message: str, raw: bool = False, **kwargs: Any) -> bool:
        for attempt in range(3):
            try:
                field = await self._find_input_field()
                if field is None:
                    raise Exception("No input textarea found")

                await asyncio.sleep(0.5)
                await field.click()

                if raw:
                    pass
                elif not self.system_injected:
                    instructions = self._load_instructions()
                    if instructions:
                        message = f"[SYSTEM INSTRUCTION]\n{instructions}\n\n[USER MESSAGE]\n{message}"
                    self.system_injected = True
                else:
                    reminder = (
                        "[QUICK REMINDER]\n"
                        "1. Use <action>[...]</action> JSON array format for all tool calls (NOT DSML).\n"
                        "2. Use <execute_command> to run any command.\n"
                        "3. Always use appropriate tags to run commands or use skills.\n\n"
                    )
                    message = f"{reminder}[USER MESSAGE]\n{message}"

                filled = await self._paste_large_message(field, message)
                if not filled:
                    await field.fill(message)

                # Force React/Angular/Vue input state synchronization
                try:
                    await field.focus()
                    await self.page.keyboard.press("End")
                    await self.page.keyboard.type(" ")
                    await self.page.keyboard.press("Backspace")
                except Exception:
                    pass

                await asyncio.sleep(0.4)

                is_ready = await self._wait_for_send_enabled()
                if not is_ready:
                    self._log_debug("send_button_timeout", timeout=10)

                # Mark existing messages as old
                await self.page.evaluate("""() => {
                    document.querySelectorAll('div.ds-assistant-message-main-content').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                    document.querySelectorAll('.ds-think-content').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                }""")

                # Capture last thinking and response before sending
                thinking_sel = PLATFORM["selectors"].get("thoughts", "div.ds-think-content")
                response_sel = PLATFORM["selectors"].get("content", "div.ds-assistant-message-main-content")

                try:
                    self._last_thinking = (
                        await self.page.locator(thinking_sel).last.inner_text()
                        if await self.page.locator(thinking_sel).count() > 0
                        else ""
                    )
                except Exception:
                    self._last_thinking = ""

                try:
                    raw_last_resp = (
                        await self.page.locator(response_sel).last.inner_text()
                        if await self.page.locator(response_sel).count() > 0
                        else ""
                    )
                    self._last_response_text = self._strip_ds(raw_last_resp)
                except Exception:
                    self._last_response_text = ""

                # Click send button
                send_sel = PLATFORM["selectors"].get(
                    "send_btn", "div[role='button']:has(path[d^='M8.3125 0.981587'])"
                )
                send_button = self.page.locator(send_sel).first
                classes = await send_button.get_attribute("class") or ""

                if "ds-button--disabled" not in classes and "disabled" not in classes.lower():
                    await send_button.click()
                else:
                    await self.page.keyboard.press(PLATFORM["keys"]["send"])

                # Wait for stop button
                stop_sel = PLATFORM["selectors"].get(
                    "stop", "div[role='button']:has(path[d^='M2 4.88'])"
                )
                try:
                    await self.page.wait_for_selector(stop_sel, state="visible", timeout=5000)
                except Exception:
                    pass

                return True

            except Exception as e:
                console.print(f"[dim yellow]Retrying send ({attempt+1}/3): {e}[/dim yellow]")
                if attempt < 2:
                    try:
                        await self.page.reload(timeout=15000)
                        await asyncio.sleep(5)
                        await self.setup_provider(force_update=False)
                    except Exception as err:
                        console.print(f"[dim red]Reload failed: {err}[/dim red]")
                        await asyncio.sleep(2)
        return False

    # ------------------------------------------------------------------
    # Response capture
    # ------------------------------------------------------------------

    async def get_response(self, **kwargs: Any) -> str:
        """Capture DeepSeek response with streaming support."""
        self._log_debug("deepseek_capture_started")

        stop_sel = PLATFORM["selectors"].get("stop", "div[role='button']:has(path[d^='M2 4.88'])")
        thinking_sel = PLATFORM["selectors"].get("thoughts", "div.ds-think-content")
        response_sel = PLATFORM["selectors"].get("content", "div.ds-assistant-message-main-content")

        live_display = kwargs.get("live_display")
        thoughts_callback = kwargs.get("thoughts_callback")

        last_thinking = getattr(self, "_last_thinking", "")
        last_response_val = getattr(self, "_last_response_text", "")

        cached_thinking = ""
        cached_response = ""

        stop_locator = self.page.locator(stop_sel)
        new_response_sel = f"{response_sel}:not([data-ghost-old='true'])"

        # Wait for generation to start
        wait_deadline = time.time() + 60
        while True:
            if await stop_locator.is_visible():
                break
            if await self.page.locator(new_response_sel).count() > 0:
                break
            if time.time() > wait_deadline:
                console.print("[bold red]⏰ Timed out waiting for DeepSeek response (60s)[/bold red]")
                return ""
            await asyncio.sleep(0.3)

        # Stream while stop button is visible
        while await stop_locator.is_visible():
            try:
                thinking_stream = (
                    await self.page.locator(thinking_sel).last.inner_text()
                    if await self.page.locator(thinking_sel).count() > 0
                    else ""
                )
            except Exception:
                thinking_stream = ""

            try:
                response_stream = (
                    await self.page.locator(response_sel).last.inner_text()
                    if await self.page.locator(response_sel).count() > 0
                    else ""
                )
            except Exception:
                response_stream = ""

            if thinking_stream != cached_thinking and thinking_stream != last_thinking:
                cached_thinking = thinking_stream
                if thoughts_callback and self.show_thoughts:
                    cleaned_thoughts = self._clean_thoughts_text(thinking_stream)
                    await thoughts_callback(cleaned_thoughts)

            response_stream = self._strip_ds(response_stream)
            if response_stream != cached_response and response_stream != last_response_val:
                cached_response = response_stream
                if live_display:
                    cleaned_response = self._clean_garbage(response_stream)
                    live_display(cleaned_response)

            await asyncio.sleep(0.3)

        # Final capture
        new_thinking_sel = f"{thinking_sel}:not([data-ghost-old='true'])"
        try:
            loc = self.page.locator(new_thinking_sel)
            final_thinking = await loc.last.inner_text() if await loc.count() > 0 else ""
        except Exception:
            final_thinking = ""

        try:
            loc = self.page.locator(new_response_sel)
            final_response = await loc.last.inner_text() if await loc.count() > 0 else ""
        except Exception:
            final_response = ""

        final_response = self._strip_ds(final_response)

        if thoughts_callback and self.show_thoughts and final_thinking:
            await thoughts_callback(self._clean_thoughts_text(final_thinking))
        if live_display and final_response:
            live_display(self._clean_garbage(final_response))

        return self._clean_garbage(final_response)

    # ------------------------------------------------------------------
    # Thoughts panel expansion (DeepSeek-specific)
    # ------------------------------------------------------------------

    async def _expand_thoughts_panel_if_needed(self, initial_count: int = 0, force: bool = False) -> bool:
        now = time.time()
        if not force and (now - self.last_thought_expand_at) < 5.0:
            return False
        try:
            base_message_sel = PLATFORM["selectors"].get("response", "div.ds-message")
            message_sel = f"{base_message_sel}:not([data-ghost-old='true'])"
            turns = self.page.locator(message_sel)
            if await turns.count() == 0:
                return False
            last_turn = turns.last
            for sel in [
                "ms-thought-chunk mat-expansion-panel-header[aria-disabled='false']",
                "mat-expansion-panel-header.top-panel-header[aria-disabled='false']",
                "mat-expansion-panel-header:has-text('Thoughts')",
            ]:
                try:
                    header = last_turn.locator(sel).last
                    if not await header.is_visible(timeout=200):
                        continue
                    expanded = await header.get_attribute("aria-expanded", timeout=500)
                    if expanded == "true":
                        return True
                    if expanded == "false":
                        await header.click(timeout=1000, force=True)
                        self.last_thought_expand_at = now
                        self._log_debug("expanded_thoughts_panel")
                        await asyncio.sleep(0.5)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Provider setup
    # ------------------------------------------------------------------

    async def setup_provider(self, force_update: bool = False, include_diary: bool = False, model_type: str | None = None, **kwargs: Any) -> None:
        """Setup for DeepSeek platform."""
        try:
            console.print(f"[bold purple]Syncing CEO configurations for {PLATFORM['name']}...[/bold purple] 🔐")

            effective_type = model_type or self.current_model_type
            try:
                model_btn = self.page.locator(f"[data-model-type='{effective_type}']").first
                if await model_btn.is_visible(timeout=2000):
                    await model_btn.click()
                    label = {"default": "Instant", "expert": "Expert", "vision": "Vision"}.get(effective_type, effective_type)
                    console.print(f"[dim]{label} mode enabled[/dim] 🚀")
            except Exception:
                pass

            if "deepthink_toggle" in PLATFORM["selectors"]:
                try:
                    toggle = self.page.locator(PLATFORM["selectors"]["deepthink_toggle"]).first
                    if await toggle.is_visible(timeout=2000):
                        classes = await toggle.get_attribute("class") or ""
                        pressed = await toggle.get_attribute("aria-pressed")
                        if "ds-toggle-button--selected" not in classes or pressed == "false":
                            await toggle.click()
                            console.print("[dim]DeepThink enabled[/dim] 🧠")
                        else:
                            console.print("[dim]DeepThink already active[/dim] 🧠")
                except Exception:
                    pass

            await self._inject_mutation_observer()
            console.print(f"[bold green]✅ {PLATFORM['name']} ready! [/bold green]")
        except Exception as e:
            console.print(f"[dim red]Setup failed: {e}[/dim red]")

    # ------------------------------------------------------------------
    # Override scroll for DeepSeek's nested scroll containers
    # ------------------------------------------------------------------

    async def _scroll_to_bottom(self) -> None:
        try:
            await self.page.evaluate("""() => {
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    if (el.scrollHeight > el.clientHeight) {
                        const s = window.getComputedStyle(el);
                        if (s.overflowY === 'auto' || s.overflowY === 'scroll' || el.tagName === 'MAIN') {
                            el.scrollTop = el.scrollHeight;
                        }
                    }
                }
                window.scrollTo(0, document.body.scrollHeight);
            }""")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup(self) -> None:
        console.print("[bold purple]Closing... Bye baby! [/bold purple]")
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()
        if self.chrome_process:
            try:
                from engine.process_utils import kill_process_tree
                kill_process_tree(self.chrome_process.pid, sig=signal.SIGTERM)
            except Exception:
                pass

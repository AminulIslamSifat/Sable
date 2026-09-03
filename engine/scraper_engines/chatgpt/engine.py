"""ChatGPTEngine — ChatGPT-specific scraper engine.

Subclasses BaseScraperEngine and implements ChatGPT-specific behavior:
- SSE-based response capture via network interception
- contenteditable input handling (#prompt-textarea)
- Model switching via model-switcher-button
- Think mode toggle
- File upload via hidden input[type=file]
- New chat via sidebar link
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
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


class ChatGPTEngine(BaseScraperEngine):
    """ChatGPT browser scraper engine."""

    PROVIDER_NAME = "chatgpt"
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
        self._last_response_text = ""
        self._sse_buffer: list[str] = []
        self._sse_done = asyncio.Event()
        self._sse_error: str | None = None

    # ------------------------------------------------------------------
    # Text cleaning
    # ------------------------------------------------------------------

    def _strip_chatgpt(self, text: str) -> str:
        """Strip ChatGPT-specific UI artifacts from response text."""
        # Remove common UI garbage
        text = re.sub(r"\bCopy code\b", "", text)
        text = re.sub(r"\bSources?\b\s*\d*\s*$", "", text, flags=re.MULTILINE)
        return text.strip()

    # ------------------------------------------------------------------
    # Input handling (contenteditable)
    # ------------------------------------------------------------------

    async def _type_into_input(self, message: str) -> bool:
        """Type message into ChatGPT's contenteditable #prompt-textarea."""
        input_sel = self.get_selector("input", "#prompt-textarea")
        try:
            editor = self.page.locator(input_sel).first
            if not await editor.is_visible(timeout=5000):
                console.print("[dim red]ChatGPT input not visible[/dim red]")
                return False

            await editor.click()
            await asyncio.sleep(0.3)

            # Use execCommand for contenteditable — more reliable than fill()
            js_code = """
            (text) => {
                const el = document.querySelector('#prompt-textarea');
                if (!el) return false;
                el.focus();
                // Clear existing content
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
                // Insert new text
                document.execCommand('insertText', false, text);
                // Dispatch input event to trigger React state update
                el.dispatchEvent(new Event('input', { bubbles: true }));
                return true;
            }
            """
            result = await self.page.evaluate(js_code, message)
            if not result:
                console.print("[dim red]Failed to insert text via execCommand[/dim red]")
                return False

            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            console.print(f"[dim red]Input failed: {e}[/dim red]")
            return False

    # ------------------------------------------------------------------
    # Send message
    # ------------------------------------------------------------------

    async def send_msg(self, message: str, **kwargs: Any) -> bool:
        """Send a message to ChatGPT."""
        console.print(f"[dim]📤 Sending to ChatGPT...[/dim]")

        # Reset SSE state
        self._sse_buffer.clear()
        self._sse_done.clear()
        self._sse_error = None

        # Type message into input
        if not await self._type_into_input(message):
            return False

        await asyncio.sleep(0.5)

        # Click send button
        send_sel = self.get_selector("send_btn", "button[aria-label='Send prompt']")
        try:
            send_btn = self.page.locator(send_sel).first
            if await send_btn.is_visible(timeout=3000):
                await send_btn.click()
                console.print("[dim]✅ Message sent[/dim]")
                await asyncio.sleep(1)
                return True
            else:
                # Fallback: press Enter
                console.print("[dim yellow]Send button not visible, pressing Enter[/dim yellow]")
                await self.page.keyboard.press("Enter")
                await asyncio.sleep(1)
                return True
        except Exception as e:
            console.print(f"[dim red]Send failed: {e}[/dim red]")
            # Last resort: Enter key
            try:
                await self.page.keyboard.press("Enter")
                await asyncio.sleep(1)
                return True
            except Exception:
                return False

    # ------------------------------------------------------------------
    # Stop generation
    # ------------------------------------------------------------------

    async def stop_generation(self, **kwargs: Any) -> bool:
        """Click the stop generating button."""
        stop_sel = self.get_selector("stop", "button[aria-label='Stop generating']")
        try:
            stop_btn = self.page.locator(stop_sel).first
            if await stop_btn.is_visible(timeout=2000):
                await stop_btn.click()
                console.print("[dim]⏹ Generation stopped[/dim]")
                await asyncio.sleep(0.5)
                return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # New chat
    # ------------------------------------------------------------------

    async def new_chat(self, reapply_model: bool = True, **kwargs: Any) -> None:
        """Start a new ChatGPT conversation."""
        console.print("[dim]🚀 Starting new ChatGPT chat...[/dim]")
        self.system_injected = False
        try:
            new_chat_sel = self.get_selector("new_chat", "a[aria-label='New chat']")
            btn = self.page.locator(new_chat_sel).first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                await asyncio.sleep(2)
                console.print("[dim]✨ New chat started[/dim]")
                self.has_fresh_chat = True
                return
        except Exception:
            pass

        # Fallback: navigate directly
        try:
            await self.page.goto(PLATFORM["url"])
            await asyncio.sleep(3)
            self.has_fresh_chat = True
            console.print("[dim]✨ Navigated to new chat[/dim]")
        except Exception as e:
            console.print(f"[dim red]New chat failed: {e}[/dim red]")

    # ------------------------------------------------------------------
    # Model switching
    # ------------------------------------------------------------------

    async def switch_model(self, model_type: str) -> bool:
        """Switch ChatGPT model via the model switcher dropdown."""
        switcher_sel = self.get_selector("model_switcher", "button[data-testid='model-switcher-button']")
        try:
            switcher = self.page.locator(switcher_sel).first
            if not await switcher.is_visible(timeout=3000):
                console.print("[dim yellow]Model switcher not visible[/dim yellow]")
                return False

            await switcher.click()
            await asyncio.sleep(1)

            # Look for the model option in the dropdown
            model_option = self.page.locator(f"[data-testid='model-option-{model_type}'], div:has-text('{model_type}')").first
            if await model_option.is_visible(timeout=3000):
                await model_option.click()
                await asyncio.sleep(1)
                self.current_model_type = model_type
                console.print(f"[dim]Switched to {model_type}[/dim] 🚀")
                return True

            # Close dropdown if we couldn't find the option
            await self.page.keyboard.press("Escape")
            console.print(f"[dim yellow]Model '{model_type}' not found in dropdown[/dim yellow]")
            return False
        except Exception as e:
            console.print(f"[dim red]Model switch failed: {e}[/dim red]")
            return False

    # ------------------------------------------------------------------
    # Thinking mode
    # ------------------------------------------------------------------

    async def set_thinking_mode(self, mode: str) -> None:
        """Toggle think mode on/off."""
        think_sel = self.get_selector("think_toggle", "button[aria-label='Toggle think mode']")
        try:
            toggle = self.page.locator(think_sel).first
            if not await toggle.is_visible(timeout=3000):
                console.print("[dim yellow]Think toggle not visible[/dim yellow]")
                return

            aria_pressed = await toggle.get_attribute("aria-pressed")
            is_on = aria_pressed == "true"

            if mode == "think" and not is_on:
                await toggle.click()
                await asyncio.sleep(0.5)
                console.print("[dim]Think mode enabled[/dim] 🧠")
            elif mode == "fast" and is_on:
                await toggle.click()
                await asyncio.sleep(0.5)
                console.print("[dim]Think mode disabled (fast)[/dim] ⚡")
        except Exception as e:
            console.print(f"[dim red]Think toggle failed: {e}[/dim red]")

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------

    async def upload_file(self, file_path: str) -> bool:
        """Upload a file to ChatGPT via the hidden file input."""
        file_input_sel = self.get_selector("file_input", "input[type='file']")
        try:
            file_input = self.page.locator(file_input_sel).first
            await file_input.set_input_files(file_path)
            await asyncio.sleep(2)
            console.print(f"[dim]📎 File attached: {file_path}[/dim]")
            return True
        except Exception as e:
            console.print(f"[dim red]File upload failed: {e}[/dim red]")
            return False

    # ------------------------------------------------------------------
    # Response capture (DOM-based with MutationObserver)
    # ------------------------------------------------------------------

    async def get_response(
        self,
        initial_count: int = 0,
        live_display: Any = None,
        thoughts_callback: Any = None,
        **kwargs: Any,
    ) -> str:
        """Capture ChatGPT response via DOM polling.

        Watches the last assistant message element for text changes.
        Supports streaming via live_display callback.
        """
        response_sel = self.get_selector("response", "div[data-message-author-role='assistant']")
        content_sel = self.get_selector("content", "div.markdown")
        stop_sel = self.get_selector("stop", "button[aria-label='Stop generating']")

        # Wait for new response to appear
        await asyncio.sleep(2)

        last_response = ""
        cached_response = ""
        max_wait = 300  # 5 minutes max
        start_time = time.time()
        stable_count = 0
        stability_threshold = 8  # ~2.4 seconds of no changes

        while time.time() - start_time < max_wait:
            # Check if stop button is gone (generation complete)
            stop_visible = False
            try:
                stop_btn = self.page.locator(stop_sel).first
                stop_visible = await stop_btn.is_visible(timeout=500)
            except Exception:
                pass

            # Get current response text
            try:
                responses = self.page.locator(response_sel)
                count = await responses.count()
                if count > 0:
                    last_resp = responses.last
                    # Try content selector first, fall back to full response
                    content_loc = last_resp.locator(content_sel)
                    if await content_loc.count() > 0:
                        current_text = await content_loc.last.inner_text()
                    else:
                        current_text = await last_resp.inner_text()
                else:
                    current_text = ""
            except Exception:
                current_text = ""

            current_text = self._strip_chatgpt(current_text)

            # Stream delta to live_display
            if current_text != cached_response and current_text != last_response:
                cached_response = current_text
                stable_count = 0
                if live_display:
                    cleaned = self._clean_garbage(current_text)
                    live_display(cleaned)
            else:
                stable_count += 1

            # If stop button is gone and text is stable, we're done
            if not stop_visible and stable_count >= stability_threshold and current_text:
                break

            last_response = current_text
            await asyncio.sleep(0.3)

        # Final capture
        try:
            responses = self.page.locator(response_sel)
            if await responses.count() > 0:
                final_resp = responses.last
                content_loc = final_resp.locator(content_sel)
                if await content_loc.count() > 0:
                    final_text = await content_loc.last.inner_text()
                else:
                    final_text = await final_resp.inner_text()
            else:
                final_text = ""
        except Exception:
            final_text = ""

        final_text = self._strip_chatgpt(final_text)

        if live_display and final_text:
            live_display(self._clean_garbage(final_text))

        return self._clean_garbage(final_text)

    # ------------------------------------------------------------------
    # UI metadata for frontend
    # ------------------------------------------------------------------

    def get_ui_metadata(self) -> dict[str, Any]:
        """ChatGPT has no model switching (single model), but supports think toggle."""
        return {
            "models": [{"id": "default", "label": "ChatGPT"}],
            "thinking_modes": [
                {"id": "fast", "label": "Fast"},
                {"id": "thinking", "label": "Thinking"},
            ],
        }

    # ------------------------------------------------------------------
    # Provider setup
    # ------------------------------------------------------------------

    async def setup_provider(self, force_update: bool = False, include_diary: bool = False, model_type: str | None = None, **kwargs: Any) -> None:
        """Setup for ChatGPT platform."""
        try:
            console.print(f"[bold purple]Setting up ChatGPT scraper...[/bold purple] 🔐")

            # Navigate to ChatGPT if not already there
            if "chatgpt.com" not in self.page.url:
                await self.page.goto(PLATFORM["url"])
                await asyncio.sleep(3)

            # Inject ghost CSS
            await self._inject_ghost_css()

            # Inject mutation observer for stealth mode
            await self._inject_mutation_observer()

            console.print(f"[bold green]✅ ChatGPT ready![/bold green]")
        except Exception as e:
            console.print(f"[dim red]Setup failed: {e}[/dim red]")
            raise

    async def _inject_ghost_css(self) -> None:
        """Inject ghost CSS to clean up ChatGPT UI."""
        ghost_css = (
            "/* Ghost Engine: ChatGPT UI cleanup */\n"
            '[data-testid="bottom-bar-announcement"] { display: none !important; }\n'
            r".group\/conversation-turn { scroll-behavior: smooth; }" + "\n"
        )
        try:
            await self.page.add_style_tag(content=ghost_css)
        except Exception:
            pass


# Alias for ScraperLifecycle._find_engine_class() which checks for "GhostChat" first
GhostChat = ChatGPTEngine

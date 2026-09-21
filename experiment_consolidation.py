#!/usr/bin/env python3
"""
Consolidation Experiment Script
================================
Tests:
1. Extract a real long conversation (286k chars) with skill events from DB
2. Check if Qwen model resolves to an API backend (fallback verification)
3. Test sending 200k char text directly to Qwen via Sable's service
4. Test sending 200k char text as .txt file attachment to Qwen
5. Test sending full 286k+ char conversation as .txt file
6. Verify fallback chain behavior
"""
from __future__ import annotations
import asyncio
import json
import sys
import os
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.database import get_messages
from connectors import resolve_backend
from server.utils import _resolve_api_backend
from engine.config import get_model_config

TEST_CHAT_ID = "29d1e0ef249140afaa3a8635d40c4ca4"  # 286k chars, 42 msgs, 1058 skill events
OUTPUT_DIR = Path("/home/sifat/sable_output/research")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def separator(title: str):
    print(f"\n{'='*70}\n  {title}\n{'='*70}\n", flush=True)


# ─── EXPERIMENT 1: Fallback Chain Verification ──────────────────────────────
def test_fallback_chain():
    separator("EXPERIMENT 1: Fallback Model Resolution")
    
    settings_path = Path("system/consolidation_settings.json")
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    
    primary = settings.get("model", "qwen3.8-max")
    fallbacks = settings.get("fallback_models", [])
    all_models = [primary] + fallbacks
    
    print(f"Primary model: {primary}")
    print(f"Fallback models: {fallbacks}")
    print()
    
    results = []
    for model in all_models:
        cfg = get_model_config(model)
        api_backend = _resolve_api_backend(model)
        raw_backend = resolve_backend(model)
        
        status = "✓ RESOLVES" if api_backend else "✗ SKIPPED (no API backend)"
        result = {
            "model": model,
            "api_backend": api_backend,
            "raw_backend": raw_backend,
            "config_keys": list(cfg.keys()) if cfg else [],
            "status": status,
            "is_primary": model == primary,
        }
        results.append(result)
        
        role = "PRIMARY" if model == primary else "FALLBACK"
        print(f"  [{role}] {model}")
        print(f"    api_backend: {api_backend}")
        print(f"    config keys: {list(cfg.keys())[:8] if cfg else 'NONE'}")
        print(f"    Status: {status}")
        print()
    
    # Analysis
    primary_works = results[0]["api_backend"] is not None
    fallbacks_that_work = [r for r in results[1:] if r["api_backend"] is not None]
    
    print("ANALYSIS:")
    if not primary_works:
        print(f"  ⚠️  PRIMARY MODEL '{primary}' DOES NOT RESOLVE TO ANY API BACKEND!")
        print(f"     It will be silently skipped in _try_consolidation_call.")
        print(f"     The code at memory.py:507 returns None immediately.")
    
    if fallbacks_that_work:
        print(f"  ✓ {len(fallbacks_that_work)} fallback(s) resolve: {[r['model'] for r in fallbacks_that_work]}")
    else:
        print(f"  ✗ NO FALLBACK MODELS RESOLVE EITHER! Consolidation will always fail.")
    
    # Check if fallback actually gets called when primary fails
    print("\n  FALLBACK CHAIN CODE REVIEW:")
    print("  In _consolidation_llm_phase (memory.py:573):")
    print("    - Primary is tried first via _try_consolidation_call")
    print("    - If it returns None, fallbacks are iterated")
    print("    - BUT: _try_consolidation_call has this early exit:")
    print("      ```")
    print("      api_backend = _resolve_api_backend(model)")
    print("      if not api_backend:")
    print("          return None  # <-- Silently skips Qwen")
    print("      ```")
    print("    - So if primary=Qwen, it returns None, and fallbacks ARE tried.")
    print("    - The question is: do the fallback models work?")
    
    return results


# ─── EXPERIMENT 2: Extract Real Conversation & Measure ───────────────────────
def test_extract_conversation():
    separator("EXPERIMENT 2: Extract Real Conversation from DB")
    
    print(f"Loading messages for chat: {TEST_CHAT_ID}")
    start = time.time()
    messages = get_messages(TEST_CHAT_ID, include_skill_events=True)
    elapsed = time.time() - start
    
    print(f"  Loaded {len(messages)} messages in {elapsed:.2f}s")
    
    total_chars = sum(len(m.get("content", "")) for m in messages)
    total_skill_events = sum(len(m.get("skill_events", [])) for m in messages)
    
    print(f"  Total content chars: {total_chars:,}")
    print(f"  Total skill events: {total_skill_events:,}")
    print(f"  Content size: {total_chars / 1024:.1f} KB ({total_chars / 1_000_000:.2f}M chars)")
    
    # Format using the actual function
    from server.api.routes.memory import _format_conversation
    
    # Test with default 50k limit
    formatted_50k = _format_conversation(messages, max_chars=50_000)
    print(f"\n  Formatted (50k limit): {len(formatted_50k):,} chars")
    truncated = "...[" in formatted_50k
    print(f"  Was truncated: {truncated}")
    if truncated:
        trunc_msg = formatted_50k.split("\n")[-1]
        print(f"  Truncation message: {trunc_msg}")
    
    # Test with 200k limit (proposed chunking size)
    formatted_200k = _format_conversation(messages, max_chars=200_000)
    print(f"\n  Formatted (200k limit): {len(formatted_200k):,} chars")
    truncated_200k = "...[" in formatted_200k
    print(f"  Was truncated: {truncated_200k}")
    
    # Test with unlimited
    formatted_full = _format_conversation(messages, max_chars=10_000_000)
    print(f"\n  Formatted (full): {len(formatted_full):,} chars")
    
    # Save full conversation to file for experiments 3-5
    full_path = OUTPUT_DIR / "test_conversation_full.txt"
    full_path.write_text(formatted_full, encoding="utf-8")
    print(f"\n  Saved full conversation to: {full_path}")
    print(f"  File size: {full_path.stat().st_size / 1024:.1f} KB")
    
    # Split into 200k chunks
    chunks = []
    for i in range(0, len(formatted_full), 200_000):
        chunk = formatted_full[i:i+200_000]
        chunks.append(chunk)
    
    print(f"\n  200k char chunks needed: {len(chunks)}")
    for i, chunk in enumerate(chunks):
        chunk_path = OUTPUT_DIR / f"test_conversation_chunk_{i}.txt"
        chunk_path.write_text(chunk, encoding="utf-8")
        print(f"  Chunk {i}: {len(chunk):,} chars -> {chunk_path}")
    
    # Check skill event presence in formatted output
    has_tool_calls = "[Tool call]:" in formatted_full
    has_tool_results = "[Tool result]:" in formatted_full
    print(f"\n  Skill events in formatted output:")
    print(f"    [Tool call] present: {has_tool_calls}")
    print(f"    [Tool result] present: {has_tool_results}")
    
    if not has_tool_calls:
        print("    ⚠️  WARNING: No tool calls found in formatted output!")
        print("    This means consolidation LLM won't see what tools were used.")
        # Show sample skill events from raw data
        for msg in messages[:5]:
            se = msg.get("skill_events", [])
            if se:
                print(f"    Sample skill event from msg {msg.get('id')}: {json.dumps(se[0])[:200]}")
                break
    
    return formatted_full, chunks


# ─── EXPERIMENT 3: Send 200k Text Directly to Qwen ──────────────────────────
async def test_qwen_direct_200k(full_text: str, chunks: list[str]):
    separator("EXPERIMENT 3: Send 200k Chars Directly to Qwen via Sable Service")
    
    from engine.service import ChatService
    svc = ChatService()
    
    chunk = chunks[0] if chunks else full_text[:200_000]
    print(f"  Sending {len(chunk):,} chars directly as message to qwen3.8-max...")
    print(f"  First 100 chars: {chunk[:100]!r}")
    
    prompt = f"[SYSTEM: Memory consolidation pass. Summarize the following conversation.]\n\n{chunk}"
    
    start = time.time()
    try:
        result = await svc.chat(
            message=prompt,
            chat_id=None,
            parent_id=None,
            model="qwen3.8-max",
        )
        elapsed = time.time() - start
        answer = result.get("answer", "")
        error = result.get("error", "")
        
        print(f"  Response time: {elapsed:.2f}s")
        print(f"  Answer length: {len(answer):,} chars")
        print(f"  Error: {error or 'None'}")
        
        # Check for captcha/WAF indicators
        captcha_indicators = ["captcha", "verify", "challenge", "blocked", "waf", "rate limit", "too many requests"]
        response_lower = (answer + error).lower()
        detected_captcha = any(ind in response_lower for ind in captcha_indicators)
        
        if detected_captcha:
            print(f"  ⚠️  CAPTCHA/WAF DETECTED in response!")
            print(f"  Response preview: {answer[:500]}")
        elif not answer:
            print(f"  ⚠️  EMPTY RESPONSE — likely blocked or errored silently")
            print(f"  Full result keys: {list(result.keys())}")
        else:
            print(f"  ✓ Got valid response")
            print(f"  Preview: {answer[:300]}...")
        
        return {"success": bool(answer), "captcha": detected_captcha, "elapsed": elapsed, "method": "direct"}
        
    except Exception as e:
        elapsed = time.time() - start
        print(f"  ✗ EXCEPTION after {elapsed:.2f}s: {type(e).__name__}: {e}")
        return {"success": False, "captcha": False, "elapsed": elapsed, "method": "direct", "error": str(e)}


# ─── EXPERIMENT 4: Send 200k as .txt File to Qwen ───────────────────────────
async def test_qwen_file_200k(chunks: list[str]):
    separator("EXPERIMENT 4: Send 200k Chars as .txt File Attachment to Qwen")
    
    from engine.service import ChatService
    svc = ChatService()
    
    chunk_path = OUTPUT_DIR / "test_conversation_chunk_0.txt"
    if not chunk_path.exists() and chunks:
        chunk_path.write_text(chunks[0], encoding="utf-8")
    
    print(f"  File: {chunk_path}")
    print(f"  File size: {chunk_path.stat().st_size / 1024:.1f} KB")
    
    prompt = "[SYSTEM: Memory consolidation pass. Read the attached conversation file and summarize key facts for memory extraction.]"
    
    start = time.time()
    try:
        # Try with file attachment parameter
        result = await svc.chat(
            message=prompt,
            chat_id=None,
            parent_id=None,
            model="qwen3.8-max",
            files=[str(chunk_path)],
        )
        elapsed = time.time() - start
        answer = result.get("answer", "")
        error = result.get("error", "")
        
        print(f"  Response time: {elapsed:.2f}s")
        print(f"  Answer length: {len(answer):,} chars")
        print(f"  Error: {error or 'None'}")
        
        captcha_indicators = ["captcha", "verify", "challenge", "blocked", "waf"]
        response_lower = (answer + error).lower()
        detected_captcha = any(ind in response_lower for ind in captcha_indicators)
        
        if detected_captcha:
            print(f"  ⚠️  CAPTCHA/WAF DETECTED!")
        elif not answer:
            print(f"  ⚠️  EMPTY RESPONSE")
        else:
            print(f"  ✓ Got valid response")
            print(f"  Preview: {answer[:300]}...")
        
        return {"success": bool(answer), "captcha": detected_captcha, "elapsed": elapsed, "method": "file_200k"}
        
    except TypeError as e:
        # files parameter might not be supported
        print(f"  Note: 'files' param not supported in svc.chat: {e}")
        print(f"  Trying alternative approach...")
        elapsed = time.time() - start
        return {"success": False, "error": str(e), "method": "file_200k", "elapsed": elapsed}
    except Exception as e:
        elapsed = time.time() - start
        print(f"  ✗ EXCEPTION after {elapsed:.2f}s: {type(e).__name__}: {e}")
        return {"success": False, "error": str(e), "method": "file_200k", "elapsed": elapsed}


# ─── EXPERIMENT 5: Send Full Conversation as .txt File ──────────────────────
async def test_qwen_file_full(full_text: str):
    separator("EXPERIMENT 5: Send FULL Conversation (~286k+) as .txt File to Qwen")
    
    from engine.service import ChatService
    svc = ChatService()
    
    full_path = OUTPUT_DIR / "test_conversation_full.txt"
    if not full_path.exists():
        full_path.write_text(full_text, encoding="utf-8")
    
    print(f"  File: {full_path}")
    print(f"  File size: {full_path.stat().st_size / 1024:.1f} KB ({full_path.stat().st_size / 1_000_000:.2f} MB)")
    
    prompt = "[SYSTEM: Memory consolidation pass. Read the attached full conversation file and extract memories.]"
    
    start = time.time()
    try:
        result = await svc.chat(
            message=prompt,
            chat_id=None,
            parent_id=None,
            model="qwen3.8-max",
            files=[str(full_path)],
        )
        elapsed = time.time() - start
        answer = result.get("answer", "")
        error = result.get("error", "")
        
        print(f"  Response time: {elapsed:.2f}s")
        print(f"  Answer length: {len(answer):,} chars")
        print(f"  Error: {error or 'None'}")
        
        if not answer:
            print(f"  ⚠️  EMPTY RESPONSE — Qwen likely can't handle this via file either")
        else:
            print(f"  ✓ Got valid response")
            print(f"  Preview: {answer[:300]}...")
        
        return {"success": bool(answer), "elapsed": elapsed, "method": "file_full"}
        
    except Exception as e:
        elapsed = time.time() - start
        print(f"  ✗ EXCEPTION after {elapsed:.2f}s: {type(e).__name__}: {e}")
        return {"success": False, "error": str(e), "method": "file_full", "elapsed": elapsed}


# ─── EXPERIMENT 6: Test Fallback Actually Works ─────────────────────────────
async def test_fallback_actually_works():
    separator("EXPERIMENT 6: Does the Fallback Chain Actually Work?")
    
    from server.api.routes.memory import _consolidation_llm_phase, _try_consolidation_call
    
    settings_path = Path("system/consolidation_settings.json")
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    primary = settings.get("model", "qwen3.8-max")
    fallbacks = settings.get("fallback_models", [])
    
    test_prompt = "Say 'hello' and nothing else."
    
    print(f"Testing each model individually with simple prompt:\n")
    
    all_models = [primary] + fallbacks
    for model in all_models:
        role = "PRIMARY" if model == primary else "FALLBACK"
        print(f"  [{role}] {model}...")
        start = time.time()
        result = await _try_consolidation_call(model, test_prompt)
        elapsed = time.time() - start
        
        if result:
            answer = result.get("answer", "")
            print(f"    ✓ Success in {elapsed:.2f}s — answer: {answer[:100]!r}")
        else:
            print(f"    ✗ Failed/Returned None in {elapsed:.2f}s")
    
    print(f"\n  Now testing full fallback chain via _consolidation_llm_phase:")
    start = time.time()
    result, attempts = await _consolidation_llm_phase(primary, test_prompt, fallbacks)
    elapsed = time.time() - start
    
    print(f"  Total time: {elapsed:.2f}s")
    print(f"  Attempts: {' → '.join(attempts)}")
    if result:
        print(f"  ✓ Chain succeeded with: {result.get('answer', '')[:100]!r}")
    else:
        print(f"  ✗ ENTIRE CHAIN FAILED — no model produced a result")
    
    return result, attempts


# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    separator("CONSOLIDATION EXPERIMENT SUITE")
    print(f"Test chat ID: {TEST_CHAT_ID}")
    print(f"Output dir: {OUTPUT_DIR}")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Exp 1: Sync - check fallback resolution
    fallback_results = test_fallback_chain()
    
    # Exp 2: Sync - extract conversation
    full_text, chunks = test_extract_conversation()
    
    # Exp 3-6: Async - actual LLM calls
    exp3_result = await test_qwen_direct_200k(full_text, chunks)
    exp4_result = await test_qwen_file_200k(chunks)
    exp5_result = await test_qwen_file_full(full_text)
    exp6_result, attempts = await test_fallback_actually_works()
    
    # Summary
    separator("FINAL SUMMARY")
    
    report = {
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "chat_id": TEST_CHAT_ID,
        "conversation_chars": len(full_text),
        "chunks_200k": len(chunks),
        "experiments": {
            "1_fallback_resolution": fallback_results,
            "3_qwen_direct_200k": exp3_result,
            "4_qwen_file_200k": exp4_result,
            "5_qwen_file_full": exp5_result,
            "6_fallback_chain": {"attempts": attempts, "success": exp6_result is not None},
        },
        "conclusions": [],
    }
    
    # Draw conclusions
    conclusions = []
    
    if not fallback_results[0]["api_backend"]:
        conclusions.append("CONFIRMED: Primary Qwen model does NOT resolve to an API backend. It is silently skipped.")
    
    working_fallbacks = [r["model"] for r in fallback_results[1:] if r["api_backend"]]
    if working_fallbacks:
        conclusions.append(f"Fallback models that resolve: {working_fallbacks}")
    else:
        conclusions.append("CRITICAL: NO fallback models resolve to API backends either!")
    
    if not exp3_result.get("success"):
        conclusions.append("Direct 200k text to Qwen: FAILED (captcha or empty response)")
    
    if not exp6_result:
        conclusions.append("CONFIRMED BUG: Fallback chain produces no result even with configured fallbacks.")
    
    report["conclusions"] = conclusions
    
    for c in conclusions:
        print(f"  • {c}")
    
    # Save report
    report_path = OUTPUT_DIR / "consolidation_experiment_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n  Report saved to: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())

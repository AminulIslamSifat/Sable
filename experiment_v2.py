#!/usr/bin/env python3
"""
Consolidation Experiment V2 — Corrected Qwen File Upload + Message Leak Investigation
======================================================================================
Fixes:
1. Uses session.upload_file() to properly upload via STS+OSS before sending
2. Passes correctly structured file objects in the chat payload
3. Investigates the 203k char message source
"""
from __future__ import annotations
import asyncio
import json
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

OUTPUT_DIR = Path("/home/sifat/sable_output/research")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TEST_CHAT_ID = "29d1e0ef249140afaa3a8635d40c4ca4"


def separator(title: str):
    print(f"\n{'='*70}\n  {title}\n{'='*70}\n", flush=True)


# ─── PART A: INVESTIGATE THE 203K MESSAGE LEAK ──────────────────────────────
def investigate_massive_message():
    separator("PART A: Where Does the 203k Message Come From?")
    
    from server.database import get_messages
    msgs = get_messages(TEST_CHAT_ID, include_skill_events=True)
    
    # Find all messages > 10k chars
    big_msgs = []
    for i, m in enumerate(msgs):
        content = m.get("content", "")
        if len(content) > 10_000:
            big_msgs.append({
                "index": i,
                "id": m.get("id"),
                "role": m.get("role"),
                "chars": len(content),
                "preview": content[:300],
                "created_at": m.get("created_at"),
            })
    
    print(f"Total messages: {len(msgs)}")
    print(f"Messages > 10k chars: {len(big_msgs)}\n")
    
    for bm in big_msgs:
        print(f"  msg[{bm['index']}] id={bm['id']} role={bm['role']} chars={bm['chars']:,} at={bm['created_at']}")
        print(f"    Preview: {bm['preview'][:200]!r}")
        print()
    
    # Now check: are these skill event outputs that got concatenated into the message?
    # Or are they raw tool call results stored directly in content?
    print("ANALYSIS: Checking if large messages contain tool output patterns...")
    
    for bm in big_msgs[:3]:
        content = msgs[bm["index"]].get("content", "")
        
        # Check for common tool output markers
        has_execute_result = "execute_command" in content or "exit code" in content
        has_view_file = "view_file" in content
        has_grep = "grep" in content or "[grep]" in content
        has_json_dump = content.count("{") > 50  # Lots of JSON
        has_markdown_code = "```" in content
        has_html = "<html" in content.lower() or "<!doctype" in content.lower()
        
        print(f"\n  msg[{bm['index']}] ({bm['chars']:,} chars):")
        print(f"    Has execute_command output: {has_execute_result}")
        print(f"    Has view_file output: {has_view_file}")
        print(f"    Has grep output: {has_grep}")
        print(f"    Looks like JSON dump: {has_json_dump}")
        print(f"    Has markdown fences: {has_markdown_code}")
        print(f"    Has HTML: {has_html}")
        
        # Check what percentage is actual assistant text vs tool output
        lines = content.split("\n")
        print(f"    Total lines: {len(lines)}")
        
        # Look for [Tool result] or similar markers
        tool_lines = sum(1 for l in lines if l.strip().startswith("[") and "]" in l[:30])
        print(f"    Lines starting with [...]: {tool_lines}")
        
        # Show structure: first 5 and last 5 lines
        print(f"    First 3 lines:")
        for l in lines[:3]:
            print(f"      {l[:120]!r}")
        if len(lines) > 6:
            print(f"    ...({len(lines)-6} more lines)...")
            print(f"    Last 3 lines:")
            for l in lines[-3:]:
                print(f"      {l[:120]!r}")
    
    # Check ALL chats for this pattern
    separator("Checking ALL Chats for Massive Messages (>50k chars)")
    
    from server.database.chats import get_db
    with get_db() as conn:
        rows = conn.execute(
            "SELECT chat_id, id, role, LENGTH(content) as clen, substr(content, 1, 200) as preview, created_at "
            "FROM messages WHERE LENGTH(content) > 50000 ORDER BY clen DESC LIMIT 20"
        ).fetchall()
    
    print(f"Found {len(rows)} messages > 50k chars across all chats:\n")
    for r in rows:
        print(f"  chat={r['chat_id'][:12]}... msg_id={r['id']} role={r['role']} chars={r['clen']:,} at={r['created_at']}")
        print(f"    Preview: {r['preview'][:150]!r}")
        print()
    
    return big_msgs


# ─── PART B: CORRECTED QWEN FILE UPLOAD + CHAT ──────────────────────────────
async def test_qwen_with_proper_upload():
    separator("PART B: Qwen Chat with Proper OSS File Upload")
    
    from engine.service import ChatService
    from engine.session import BrowserSessionManager
    
    svc = ChatService()
    
    # We need a session manager to access upload_file
    # BrowserSessionManager has the upload_file method
    session_mgr = BrowserSessionManager()
    
    # Ensure headers are loaded (needed for STS token request)
    headers = await svc._ensure_headers()
    cookies = headers.get("Cookie", "")
    bx_ua = headers.get("bx-ua", "")
    bx_umidtoken = headers.get("bx-umidtoken", "")
    
    # Create test files of different sizes
    test_files = {
        "small_10k": OUTPUT_DIR / "test_10k.txt",
        "medium_100k": OUTPUT_DIR / "test_100k.txt",
        "large_200k": OUTPUT_DIR / "test_200k.txt",
    }
    
    # Generate test content
    base_text = "This is line {i} of the test conversation for memory consolidation. " * 5
    for name, path in test_files.items():
        target_size = {"small_10k": 10_000, "medium_100k": 100_000, "large_200k": 200_000}[name]
        content = ""
        i = 0
        while len(content) < target_size:
            content += base_text.format(i=i) + "\n"
            i += 1
        content = content[:target_size]
        path.write_text(content, encoding="utf-8")
        print(f"  Created {name}: {path.stat().st_size:,} bytes")
    
    # Also prepare the real conversation chunk
    real_chunk_path = OUTPUT_DIR / "test_conversation_chunk_0.txt"
    if real_chunk_path.exists():
        test_files["real_200k"] = real_chunk_path
        print(f"  Real chunk: {real_chunk_path.stat().st_size:,} bytes")
    
    results = {}
    
    for label, fpath in test_files.items():
        separator(f"Upload & Send: {label} ({fpath.stat().st_size:,} bytes)")
        
        try:
            # Step 1: Upload file via STS + OSS
            print(f"  Uploading {fpath.name} to Qwen OSS...")
            start = time.time()
            file_obj = await session_mgr.upload_file(
                str(fpath),
                cookies=cookies,
                bx_ua=bx_ua,
                bx_umidtoken=bx_umidtoken,
            )
            upload_time = time.time() - start
            
            if not file_obj:
                print(f"  ✗ Upload failed!")
                results[label] = {"upload": False, "error": "upload returned None"}
                continue
            
            print(f"  ✓ Upload succeeded in {upload_time:.2f}s")
            print(f"    file_id: {file_obj.get('id')}")
            print(f"    file_class: {file_obj.get('file_class')}")
            print(f"    status: {file_obj.get('status')}")
            
            # Step 2: Send chat with the uploaded file object
            prompt = "Read the attached file. Summarize the first 3 paragraphs concisely."
            
            print(f"\n  Sending chat with file attachment...")
            start = time.time()
            result = await svc.chat(
                message=prompt,
                chat_id=None,
                parent_id=None,
                model="qwen3.8-max",
                files=[file_obj],  # Pass the structured file object, NOT a string path
            )
            chat_time = time.time() - start
            
            answer = result.get("answer", "")
            error = result.get("error", "")
            
            print(f"  Response time: {chat_time:.2f}s")
            print(f"  Answer length: {len(answer):,} chars")
            
            if error:
                print(f"  Error: {error[:300]}")
            
            if answer:
                print(f"  ✓ SUCCESS!")
                print(f"  Preview: {answer[:300]}...")
                results[label] = {"upload": True, "chat": True, "time": chat_time, "answer_len": len(answer)}
            else:
                print(f"  ✗ Empty response")
                results[label] = {"upload": True, "chat": False, "error": error}
                
        except Exception as e:
            print(f"  ✗ Exception: {type(e).__name__}: {e}")
            results[label] = {"upload": False, "error": str(e)}
    
    return results


# ─── PART C: TEST FULL CONVERSATION WITH PROPER UPLOAD ──────────────────────
async def test_full_conversation_upload():
    separator("PART C: Full 456k Conversation via Qwen File Upload")
    
    from engine.service import ChatService
    from engine.session import BrowserSessionManager
    
    svc = ChatService()
    session_mgr = BrowserSessionManager()
    
    headers = await svc._ensure_headers()
    
    full_path = OUTPUT_DIR / "test_conversation_full.txt"
    if not full_path.exists():
        print("  Full conversation file not found, generating...")
        from server.database import get_messages
        from server.api.routes.memory import _format_conversation
        msgs = get_messages(TEST_CHAT_ID, include_skill_events=True)
        formatted = _format_conversation(msgs, max_chars=10_000_000)
        full_path.write_text(formatted, encoding="utf-8")
    
    print(f"  File size: {full_path.stat().st_size / 1024:.1f} KB")
    
    try:
        file_obj = await session_mgr.upload_file(
            str(full_path),
            cookies=headers.get("Cookie", ""),
            bx_ua=headers.get("bx-ua", ""),
            bx_umidtoken=headers.get("bx-umidtoken", ""),
        )
        
        if not file_obj:
            print("  ✗ Upload failed for full conversation")
            return False
        
        print(f"  ✓ Uploaded full conversation. file_id={file_obj.get('id')}")
        
        prompt = "[Memory Consolidation Task]\nRead the attached conversation log. Extract key facts, preferences, and technical details about the user. Return them as a JSON object with categories: semantic, episodic, procedural."
        
        start = time.time()
        result = await svc.chat(
            message=prompt,
            chat_id=None,
            parent_id=None,
            model="qwen3.8-max",
            files=[file_obj],
        )
        elapsed = time.time() - start
        
        answer = result.get("answer", "")
        error = result.get("error", "")
        
        print(f"  Response time: {elapsed:.2f}s")
        print(f"  Answer length: {len(answer):,} chars")
        
        if answer:
            print(f"  ✓ FULL CONVERSATION CONSOLIDATION WORKED!")
            print(f"  Preview: {answer[:500]}...")
            # Save the result
            result_path = OUTPUT_DIR / "consolidation_result_qwen.json"
            result_path.write_text(json.dumps({"answer": answer, "elapsed": elapsed}, indent=2), encoding="utf-8")
            print(f"  Saved to: {result_path}")
            return True
        else:
            print(f"  ✗ Empty response. Error: {error[:300] if error else 'None'}")
            return False
            
    except Exception as e:
        print(f"  ✗ Exception: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    separator("CONSOLIDATION EXPERIMENT V2")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Part A: Sync investigation
    big_msgs = investigate_massive_message()
    
    # Part B: Corrected upload test
    upload_results = await test_qwen_with_proper_upload()
    
    # Part C: Full conversation
    full_success = await test_full_conversation_upload()
    
    # Summary
    separator("V2 SUMMARY")
    print(f"Upload test results: {json.dumps(upload_results, indent=2, default=str)}")
    print(f"Full conversation success: {full_success}")


if __name__ == "__main__":
    asyncio.run(main())

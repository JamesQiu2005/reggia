"""Test that chat CC subprocess works in headless mode without permission denials.

Usage:
    uv run python test_headless_chat.py          # Static checks only
    uv run python test_headless_chat.py --live    # Static + one real chat call
"""

import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://localhost:8000"
WORKSPACE = Path(__file__).resolve().parent / "chat_workspace"


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

def check_settings_json():
    path = WORKSPACE / ".claude" / "settings.json"
    data = json.loads(path.read_text())
    allowed = data.get("permissions", {}).get("allow", [])
    print(f"[check] settings.json allowlist: {allowed}")
    assert "Read" in allowed, "Missing Read permission"
    assert any("Bash(curl *localhost*)" in a for a in allowed), "Missing Bash(curl *localhost*) restriction"
    assert not any("Bash(curl *)" in a and "localhost" not in a for a in allowed), "Bash(curl *) must be scoped to localhost"
    assert "Write" not in allowed, "Write should NOT be in allowlist"
    assert "Edit" not in allowed, "Edit should NOT be in allowlist"
    assert "Agent" not in allowed, "Agent should NOT be in allowlist"
    print("  ✓ settings.json is correctly restricted")


def check_claude_md():
    path = WORKSPACE / "CLAUDE.md"
    text = path.read_text()
    assert "Tool constraints" in text, "Missing tool constraints section"
    assert "Read" in text, "Missing Read in tool constraints"
    assert "Bash(curl" in text, "Missing Bash(curl) in tool constraints"
    assert "localhost" in text, "Missing localhost restriction in tool constraints"
    print("  ✓ CLAUDE.md has tool constraints")


def check_backend_alive():
    try:
        resp = urllib.request.urlopen(f"{BASE}/docs", timeout=5)
        assert resp.status == 200
        print("  ✓ Backend is running")
    except Exception as e:
        print(f"  ✗ Backend not reachable: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Live chat test (costs one API call)
# ---------------------------------------------------------------------------

def test_chat_roundtrip():
    """Send one minimal message and verify the SSE stream completes cleanly."""
    print("\n[chat] Creating session...")
    req = urllib.request.Request(f"{BASE}/sessions", method="POST")
    with urllib.request.urlopen(req) as resp:
        session = json.loads(resp.read())
    sid = session["id"]
    print(f"  → session_id: {sid}")

    print("[chat] Sending message (expecting Reggia read → short reply)...")
    body = json.dumps({
        "prompt": "What should I focus on today? Keep it under 30 words.",
        "model": "deepseek-v4-flash",
    }).encode()
    req = urllib.request.Request(f"{BASE}/sessions/{sid}/chat", data=body,
                                 headers={"Content-Type": "application/json"})

    t0 = time.time()
    result_text = ""
    error_text = ""
    timeout = 120  # 2 min max

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for line_bytes in resp:
                line = line_bytes.decode().strip()
                if not line or not line.startswith("data: "):
                    continue
                try:
                    msg = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                if msg.get("type") == "error":
                    error_text = msg.get("message", "")
                    print(f"  ✗ Error from subprocess: {error_text[:200]}")
                    return False
                elif msg.get("type") == "assistant":
                    for block in msg.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            result_text += block["text"]
                elif msg.get("type") == "result":
                    elapsed = time.time() - t0
                    print(f"  ✓ Chat completed in {elapsed:.0f}s")
                    print(f"  → Response: {result_text[:100]}")
                    return True
    except Exception as e:
        print(f"  ✗ Connection error: {e}")
        return False

    if error_text:
        print(f"  ✗ Chat failed with error")
        return False

    print("  ✗ No result received (timeout or empty stream)")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Static checks ===\n")
    check_settings_json()
    check_claude_md()
    check_backend_alive()
    print("\n  All static checks passed ✓")

    if "--live" in sys.argv:
        print("\n=== Live chat test (costs API credits) ===\n")
        ok = test_chat_roundtrip()
        if ok:
            print("\n=== All tests passed ✓ ===")
        else:
            print("\n=== Chat test failed ✗ ===")
            sys.exit(1)
    else:
        print("\nPass --live to run a real chat roundtrip (costs API credits).")

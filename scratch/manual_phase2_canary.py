"""W34c Phase 2 manual canary (Section 5 of the Option A run).

Streams a VALUE_TRACE query and reports the phase2 response shape so we
can verify phase2.explainer.invoke is exercised on gpt-4o-mini.
Standalone — uses the same curl-based SSE reader the canary harness uses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

QUERY = (
    "Why is N_EOP_BAL negative for account "
    "PK00108091TR00PKRGBP-T24-LIVEPOSG on 2025-12-31?"
)
URL = "http://localhost:8000/v1/stream"


def main() -> int:
    body = {
        "query": QUERY,
        "session_id": f"manual-phase2-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
        "engineer_id": "manual-phase2-canary",
    }
    cmd = [
        "curl", "-s", "-N", "--no-buffer",
        "-H", "Content-Type: application/json",
        "-X", "POST",
        "--max-time", "300",
        "-d", json.dumps(body),
        URL,
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    events: list[tuple[str, dict]] = []
    buf = ""
    fd = proc.stdout.fileno()
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buf += chunk.decode("utf-8", errors="replace")
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            kind = data = None
            for line in block.split("\n"):
                if line.startswith("event:"):
                    kind = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
            if kind and data:
                try:
                    events.append((kind, json.loads(data)))
                except json.JSONDecodeError:
                    events.append((kind, {"_raw": data}))
            if kind == "done":
                proc.terminate()
                break
        if any(k == "done" for k, _ in events):
            break
    proc.wait(timeout=5)

    print(f"=== phase2 manual canary ===")
    print(f"query: {QUERY}\n")

    meta = next((d for k, d in events if k == "meta"), None)
    done = next((d for k, d in events if k == "done"), None)
    stages = [d for k, d in events if k == "stage"]

    print(f"meta.query_type = {meta and meta.get('query_type')!r}")
    print(f"meta.schema     = {meta and meta.get('schema')!r}")
    print(f"done.type       = {done and done.get('type')!r}")
    print(f"done.badge      = {done and done.get('badge')!r}")
    print(f"done.status     = {done and done.get('status')!r}")
    print(f"stages          = {[s.get('stage') for s in stages]}")

    if not done:
        print("\n!! NO done EVENT — stream ended early")
        return 1

    summary = done.get("summary") or done.get("explanation", {}).get("markdown", "")
    print(f"\n--- summary (first 800 chars) ---")
    print(summary[:800])

    leaks = ["Traceback", "ValidationError", "phase2_invoke_llm",
             "<class '", "Phase2Explainer"]
    leaked = [w for w in leaks if w in (summary or "")]
    print(f"\nleak check: {leaked or 'clean'}")

    qt = (meta or {}).get("query_type")
    if qt != "VALUE_TRACE":
        print(f"\n!! Did not route to VALUE_TRACE (got {qt!r}) — phase2 not exercised")
        return 2
    print("\nOK — VALUE_TRACE routed; phase2.explainer was on the call path")
    return 0


if __name__ == "__main__":
    sys.exit(main())

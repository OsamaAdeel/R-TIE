"""W34 post-W35 latency measurement — runs the 9 canaries 3x each.

Captures wall-clock total, time-to-first-token (TTFT), and the
correlation_id for each run so the JSONL stage timings can be joined
back. Output goes to scratch/w34_canary_results.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path


CANARIES = [
    {
        "id": "C1",
        "label": "FUNCTION_LOGIC OFSMDM (FN_LOAD_OPS_RISK_DATA)",
        "query": "How does FN_LOAD_OPS_RISK_DATA work?",
        "filters": {},
        "expected_type": "FUNCTION_LOGIC",
    },
    {
        "id": "C2",
        "label": "DATA_QUERY OFSMDM (N_EOP_BAL ABL on 2025-12-31)",
        "query": "What is the total N_EOP_BAL for V_LV_CODE='ABL' on 2025-12-31?",
        "filters": {"mis_date": "2025-12-31"},
        "expected_type": "DATA_QUERY",
    },
    {
        "id": "C3",
        "label": "FUNCTION_LOGIC OFSERM via BI routing (CAP973)",
        "query": "How is CAP973 calculated?",
        "filters": {},
        "expected_type": "FUNCTION_LOGIC",
    },
    {
        "id": "C4",
        "label": "FUNCTION_LOGIC OFSERM explicit (Deferred_Tax_Asset)",
        "query": "How does CS_Deferred_Tax_Asset_Net_of_DTL_Calculation work?",
        "filters": {},
        "expected_type": "FUNCTION_LOGIC",
    },
    {
        "id": "C5",
        "label": "DATA_QUERY OFSERM (N_STD_ACCT_HEAD_AMT 2025-12-31)",
        "query": (
            "What is the total N_STD_ACCT_HEAD_AMT in FCT_STANDARD_ACCT_HEAD "
            "on 2025-12-31?"
        ),
        "filters": {"mis_date": "2025-12-31"},
        "expected_type": "DATA_QUERY",
    },
    {
        "id": "C6",
        "label": "VARIABLE_TRACE OFSMDM (writes N_STD_ACCT_HEAD_AMT)",
        "query": "What writes N_STD_ACCT_HEAD_AMT?",
        "filters": {},
        "expected_type": "VARIABLE_TRACE",
    },
    {
        "id": "C7",
        "label": "DECLINED reconciliation",
        "query": (
            "Why does FCT_PRODUCT_EXPOSURES differ from STG_PRODUCT_PROCESSOR?"
        ),
        "filters": {},
        "expected_type": "ANY",
    },
    {
        "id": "C8",
        "label": "FUNCTION_LOGIC OFSERM via BI routing + derivation (CAP943)",
        "query": "How is CAP943 calculated?",
        "filters": {},
        "expected_type": "FUNCTION_LOGIC",
    },
    {
        "id": "C9",
        "label": "W45 ungrounded path (CAP999)",
        "query": "How is CAP999 calculated?",
        "filters": {},
        "expected_type": "FUNCTION_LOGIC",
    },
]


def stream_one(query: str, filters: dict, base_url: str, timeout: float) -> dict:
    """Run a single SSE request, return wall-clock + TTFT + correlation_id."""
    url = f"{base_url}/v1/stream"
    # API uses strict pydantic model: query / session_id / engineer_id /
    # provider? / model?. Filters are extracted by the orchestrator from
    # the raw query text (e.g. "on 2025-12-31"), not passed separately.
    _ = filters
    body = {
        "query": query,
        "session_id": f"w34-canary-{int(time.time() * 1000)}",
        "engineer_id": "w34-diagnostic",
        "provider": "openai",
        "model": "gpt-4o",
    }

    t0 = time.perf_counter()
    # W34a: TTFT is now "time to first non-meta SSE event" — i.e. the
    # first event whose payload the frontend will render as user-visible
    # progress. ``meta`` is excluded because it carries plumbing (schema,
    # query_type, correlation_id) the user doesn't see. Counted event
    # types: stage, token, status, done, error. The legacy metric
    # (time-to-first event:token) is kept as ``ttft_token_ms`` for the
    # before/after comparison the W34a fix branch requires.
    first_feedback_at: float | None = None
    first_feedback_kind: str | None = None
    first_token_at: float | None = None
    correlation_id: str | None = None
    done_seen = False
    error: str | None = None
    event_kind: str | None = None
    char_count = 0
    stage_events: list[dict] = []
    NON_META = {"stage", "token", "status", "done", "error"}

    # The runner uses curl --no-buffer because httpx (and other Python
    # HTTP clients) buffer the first ~64 KB of body at httpcore level,
    # holding small SSE events for ~2 s on localhost — a measurement
    # artifact, NOT a server-side delay. Browser Streams API readers
    # don't have this buffer; curl mirrors that behaviour. Verified by
    # comparing httpx (~2.4 s TTFT for every canary, including ones
    # whose first yield runs at server t<10 ms) against curl --no-buffer
    # (~240 ms TTFT for the same canary).
    body_json = json.dumps(body)
    cmd = [
        "curl", "-s", "-N", "--no-buffer",
        "-D", "-",  # write headers to stdout before body
        "-H", "Content-Type: application/json",
        "-X", "POST",
        "--max-time", str(int(timeout)),
        "-d", body_json,
        url,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        in_headers = True
        buf = ""
        try:
            import os as _os
            stdout_fd = proc.stdout.fileno()
            while True:
                # os.read blocks until at least 1 byte is available, then
                # returns up to ``size`` bytes — won't wait for the buffer
                # to fill. Critical for accurate TTFT measurement.
                chunk = _os.read(stdout_fd, 4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                if in_headers:
                    # Headers are CRLF-delimited and end with a blank line.
                    while True:
                        nl = text.find("\n")
                        if nl == -1:
                            buf += text
                            text = ""
                            break
                        line, text = text[:nl], text[nl + 1:]
                        line = (buf + line).rstrip("\r")
                        buf = ""
                        if line == "":
                            in_headers = False
                            break
                        if line.lower().startswith("x-correlation-id:"):
                            correlation_id = line.split(":", 1)[1].strip()
                    if in_headers:
                        continue
                # Body: aggregate into the SSE event parser.
                buf += text
                while "\n\n" in buf:
                    event_block, buf = buf.split("\n\n", 1)
                    kind = None
                    for ev_line in event_block.split("\n"):
                        if ev_line.startswith("event:"):
                            kind = ev_line[len("event:"):].strip()
                        elif ev_line.startswith("data:"):
                            data_str = ev_line[len("data:"):].strip()
                            if (
                                kind in NON_META
                                and first_feedback_at is None
                            ):
                                first_feedback_at = time.perf_counter()
                                first_feedback_kind = kind
                            if kind == "token" and first_token_at is None:
                                first_token_at = time.perf_counter()
                            if kind == "token":
                                try:
                                    char_count += len(json.loads(data_str))
                                except Exception:
                                    char_count += len(data_str)
                            if kind == "stage":
                                try:
                                    stage_events.append(json.loads(data_str))
                                except Exception:
                                    stage_events.append({"raw": data_str})
                            if kind == "done":
                                done_seen = True
                                event_kind = "done"
                            if kind == "error":
                                event_kind = "error"
                                try:
                                    error = json.loads(data_str).get("error")
                                except Exception:
                                    error = data_str
        finally:
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
        if not done_seen and event_kind != "error":
            error = "stream ended without done event"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    _ = uuid  # silence unused-import warning

    t1 = time.perf_counter()
    total_ms = (t1 - t0) * 1000.0
    ttft_ms = (
        (first_feedback_at - t0) * 1000.0 if first_feedback_at else None
    )
    ttft_token_ms = (
        (first_token_at - t0) * 1000.0 if first_token_at else None
    )
    return {
        "correlation_id": correlation_id,
        "total_ms": round(total_ms, 1),
        "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
        "ttft_first_event_kind": first_feedback_kind,
        "ttft_token_ms": (
            round(ttft_token_ms, 1) if ttft_token_ms is not None else None
        ),
        "stage_events": stage_events,
        "char_count": char_count,
        "done": done_seen,
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--gap-seconds", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--out", default="scratch/w34_canary_results.json"
    )
    parser.add_argument(
        "--only", default="", help="comma-separated canary IDs to run"
    )
    args = parser.parse_args()

    selected = CANARIES
    if args.only:
        wanted = set(s.strip() for s in args.only.split(",") if s.strip())
        selected = [c for c in CANARIES if c["id"] in wanted]

    results = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": args.base_url,
        "runs_per_canary": args.runs,
        "gap_seconds": args.gap_seconds,
        "canaries": [],
    }

    for canary in selected:
        print(f"\n=== {canary['id']} :: {canary['label']} ===", flush=True)
        print(f"    query: {canary['query']!r}", flush=True)
        runs = []
        for n in range(1, args.runs + 1):
            print(f"  run {n}/{args.runs} ... ", end="", flush=True)
            r = stream_one(
                canary["query"],
                canary.get("filters") or {},
                args.base_url,
                args.timeout,
            )
            runs.append(r)
            label = "OK" if r["done"] and not r["error"] else f"FAIL ({r['error']})"
            print(
                f"{label} | total={r['total_ms']}ms "
                f"ttft={r['ttft_ms']}ms ({r.get('ttft_first_event_kind')}) "
                f"ttft_token={r.get('ttft_token_ms')}ms "
                f"cid={r['correlation_id']}",
                flush=True,
            )
            time.sleep(args.gap_seconds)
        results["canaries"].append({
            "id": canary["id"],
            "label": canary["label"],
            "query": canary["query"],
            "runs": runs,
        })
        # Persist incrementally so partial results survive a crash.
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"\n-> wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

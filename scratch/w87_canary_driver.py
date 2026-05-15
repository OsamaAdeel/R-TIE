"""W87 manual canary driver.

Runs three canaries against /v1/stream and captures verbatim SSE events:
  A — Q11 reproduction ("what is the threshold value for G Test")
      Expected: W87 fires, badge=UNVERIFIED, type=unrecognized_term.
  B — Known function regression ("How does FN_LOAD_OPS_RISK_DATA work?")
      Expected: W87 does NOT fire; normal FUNCTION_LOGIC pipeline runs.
  C — CAP-code regression ("How is CAP973 calculated?")
      Expected: W87 does NOT fire; BI routing claims the query.

Each canary writes a self-contained <id>.txt with the meta + done event
payloads. Toheed reviews these against the cowork reference.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx


URL = "http://localhost:8000/v1/stream"
OUT_DIR = Path(__file__).resolve().parent

CANARIES: List[Tuple[str, str]] = [
    ("w87_canary_a", "what is the threshold value for G Test"),
    ("w87_canary_b", "How does FN_LOAD_OPS_RISK_DATA work?"),
    ("w87_canary_c", "How is CAP973 calculated?"),
]


def run_one(query: str) -> Dict[str, Any]:
    body = {
        "query": query,
        "session_id": str(uuid.uuid4()),
        "engineer_id": "w87-canary",
    }
    events: List[Tuple[str, Any]] = []
    done_payload: Optional[Dict[str, Any]] = None
    meta_payload: Optional[Dict[str, Any]] = None
    markdown_tokens: List[str] = []
    with httpx.Client(timeout=240.0) as client:
        with client.stream("POST", URL, json=body) as resp:
            resp.raise_for_status()
            current_event: Optional[str] = None
            for line in resp.iter_lines():
                if not line:
                    current_event = None
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    try:
                        parsed = json.loads(data)
                    except Exception:
                        parsed = data
                    events.append((current_event, parsed))
                    if current_event == "done":
                        done_payload = parsed
                    elif current_event == "meta" and meta_payload is None:
                        meta_payload = parsed
                    elif current_event == "token" and isinstance(parsed, str):
                        markdown_tokens.append(parsed)
    return {
        "meta": meta_payload,
        "done": done_payload,
        "markdown": "".join(markdown_tokens),
        "event_names": [e[0] for e in events],
    }


def write_report(canary_id: str, query: str, result: Dict[str, Any]) -> Path:
    path = OUT_DIR / f"{canary_id}.txt"
    parts: List[str] = []
    parts.append(f"=== {canary_id.upper()} ===")
    parts.append(f"QUERY: {query}")
    parts.append("")
    parts.append("--- event sequence ---")
    parts.append(", ".join(result.get("event_names") or []))
    parts.append("")
    parts.append("--- meta event (verbatim) ---")
    parts.append(json.dumps(result.get("meta"), indent=2))
    parts.append("")
    parts.append("--- done event (verbatim) ---")
    parts.append(json.dumps(result.get("done"), indent=2))
    parts.append("")
    parts.append("--- rendered markdown body ---")
    parts.append(result.get("markdown") or "(empty)")
    parts.append("")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def main() -> int:
    overall = 0
    for canary_id, query in CANARIES:
        print(f"[{canary_id}] {query!r}", flush=True)
        try:
            result = run_one(query)
        except Exception as exc:
            print(f"  FAILED: {exc}")
            overall = 1
            continue
        path = write_report(canary_id, query, result)
        done = result.get("done") or {}
        meta = result.get("meta") or {}
        print(
            f"  type={done.get('type')} badge={done.get('badge')} "
            f"validated={done.get('validated')} -> {path.name}",
            flush=True,
        )
        print(
            f"  meta.type={meta.get('type')} meta.warnings={meta.get('warnings')}",
            flush=True,
        )
    return overall


if __name__ == "__main__":
    sys.exit(main())

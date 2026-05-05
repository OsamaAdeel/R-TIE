"""RTIE canary runner.

Runs the 18-query regression set defined in canaries.yaml against a live
backend at /v1/stream and asserts on the captured SSE payload. Supports
tier filtering (--tier 1, --tier 2, --tier 3, or --all) so the same fixture
file backs three different gating use-cases:

  * Tier 1 — fast happy-path + UNSUPPORTED checks. Run before any LLM model
    swap.
  * Tier 2 — DATA_QUERY + VARIABLE_TRACE with pinned SQL/citation semantics.
    Run before promoting data_query._generate_sql to a smaller model.
  * Tier 3 — manual; needs local Oracle data state. Skipped unless --tier 3
    is passed explicitly.

The HTTP loop is lifted from scratch/w34_canary_runner.py — `curl --no-buffer`
is load-bearing because httpx (and other Python HTTP clients) buffer the
first ~64 KB at httpcore level, holding small SSE events for ~2 s on
localhost. That's a measurement artifact, not a server-side delay.

Run from RTIE/:
    python tests/canary/run_canaries.py --tier 1
    python tests/canary/run_canaries.py --tier 2
    python tests/canary/run_canaries.py --tier 1 --tier 2
    python tests/canary/run_canaries.py --all --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_BASE_URL = "http://localhost:8000"
NON_META_EVENTS = {"stage", "token", "status", "done", "error"}


# ---------------------------------------------------------------------------
# SSE capture (curl --no-buffer loop, lifted from scratch/w34_canary_runner.py)
# ---------------------------------------------------------------------------

@dataclass
class StreamCapture:
    """Everything the assertion engine might need from one SSE response."""
    correlation_id: str | None = None
    done: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None
    stage_events: list[dict[str, Any]] = field(default_factory=list)
    markdown: str = ""
    error: str | None = None
    total_ms: float = 0.0


def stream_one(query: str, base_url: str, timeout: float) -> StreamCapture:
    """POST to /v1/stream, parse SSE events, return a StreamCapture.

    Uses `curl --no-buffer -N` instead of httpx to avoid the ~2 s first-event
    artifact caused by Python HTTP clients buffering at httpcore level.
    """
    url = f"{base_url}/v1/stream"
    body = {
        "query": query,
        "session_id": f"canary-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
        "engineer_id": "canary-runner",
        # No provider/model override — runs against whatever the backend
        # has configured. That's the point: gate model changes by running
        # this BEFORE flipping a model in llm_factory or env.
    }
    body_json = json.dumps(body)
    cmd = [
        "curl", "-s", "-N", "--no-buffer",
        "-D", "-",
        "-H", "Content-Type: application/json",
        "-X", "POST",
        "--max-time", str(int(timeout)),
        "-d", body_json,
        url,
    ]

    cap = StreamCapture()
    t0 = time.perf_counter()

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as exc:
        cap.error = f"failed to spawn curl: {type(exc).__name__}: {exc}"
        return cap

    in_headers = True
    buf = ""
    done_seen = False
    error_text: str | None = None

    try:
        stdout_fd = proc.stdout.fileno()
        while True:
            chunk = os.read(stdout_fd, 4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")

            if in_headers:
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
                        cap.correlation_id = line.split(":", 1)[1].strip()
                if in_headers:
                    continue

            buf += text
            while "\n\n" in buf:
                event_block, buf = buf.split("\n\n", 1)
                kind: str | None = None
                data_str: str | None = None
                for ev_line in event_block.split("\n"):
                    if ev_line.startswith("event:"):
                        kind = ev_line[len("event:"):].strip()
                    elif ev_line.startswith("data:"):
                        data_str = ev_line[len("data:"):].strip()
                if kind is None or data_str is None:
                    continue
                try:
                    parsed: Any = json.loads(data_str)
                except Exception:
                    parsed = data_str

                if kind == "token":
                    if isinstance(parsed, str):
                        cap.markdown += parsed
                    else:
                        cap.markdown += str(parsed)
                elif kind == "stage":
                    if isinstance(parsed, dict):
                        cap.stage_events.append(parsed)
                elif kind == "meta":
                    if isinstance(parsed, dict):
                        cap.meta = parsed
                elif kind == "done":
                    if isinstance(parsed, dict):
                        cap.done = parsed
                    done_seen = True
                elif kind == "error":
                    if isinstance(parsed, dict):
                        error_text = parsed.get("error") or json.dumps(parsed)
                    else:
                        error_text = str(parsed)
    finally:
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    cap.total_ms = (time.perf_counter() - t0) * 1000.0

    if not done_seen and error_text is None:
        cap.error = "stream ended without done event"
    elif error_text is not None:
        cap.error = error_text

    return cap


# ---------------------------------------------------------------------------
# Assertion engine
# ---------------------------------------------------------------------------

@dataclass
class AssertionResult:
    passed: bool
    description: str
    detail: str = ""


def _resolve_path(obj: Any, path: str) -> Any:
    """Walk a dotted path through dict/list. Returns _MISSING on miss."""
    cur = obj
    for part in path.split("."):
        if cur is None:
            return _MISSING
        if isinstance(cur, dict):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                idx = int(part)
            except ValueError:
                return _MISSING
            if idx < 0 or idx >= len(cur):
                return _MISSING
            cur = cur[idx]
        else:
            return _MISSING
    return cur


_MISSING = object()


def _markdown_text(cap: StreamCapture) -> str:
    """Concatenate token-stream markdown with the done.explanation.markdown.

    Some response shapes (W45 ungrounded, W49 partial-source, UNSUPPORTED)
    deliver the structured prose only inside `done.explanation.markdown`.
    Other shapes (FUNCTION_LOGIC streaming) emit it as a token stream and
    don't repeat it in the done payload. Concatenate both so substring
    assertions are robust to either shape.
    """
    parts: list[str] = []
    if cap.markdown:
        parts.append(cap.markdown)
    if cap.done:
        explanation = cap.done.get("explanation")
        if isinstance(explanation, dict):
            md = explanation.get("markdown")
            if isinstance(md, str):
                parts.append(md)
        # Some declined paths put the human prose under `message`.
        message = cap.done.get("message")
        if isinstance(message, str):
            parts.append(message)
    return "\n".join(parts)


def _sql_text(cap: StreamCapture) -> str:
    if cap.done is None:
        return ""
    sql = cap.done.get("sql") or ""
    return sql if isinstance(sql, str) else str(sql)


def _eval_assertion(spec: dict[str, Any], cap: StreamCapture) -> AssertionResult:
    kind = spec.get("kind")
    if kind == "done_field_eq":
        path = spec["path"]
        expected = spec["value"]
        actual = _resolve_path(cap.done, path)
        passed = actual == expected
        return AssertionResult(
            passed,
            f"done.{path} == {expected!r}",
            "" if passed else f"got {actual!r}",
        )
    if kind == "done_field_neq":
        path = spec["path"]
        unexpected = spec["value"]
        actual = _resolve_path(cap.done, path)
        passed = actual != unexpected
        return AssertionResult(
            passed,
            f"done.{path} != {unexpected!r}",
            "" if passed else f"got {actual!r} (forbidden)",
        )
    if kind == "done_field_in":
        path = spec["path"]
        allowed = list(spec["values"])
        actual = _resolve_path(cap.done, path)
        passed = actual in allowed
        return AssertionResult(
            passed,
            f"done.{path} in {allowed!r}",
            "" if passed else f"got {actual!r}",
        )
    if kind == "done_field_present":
        path = spec["path"]
        actual = _resolve_path(cap.done, path)
        passed = actual is not _MISSING and actual not in (None, "", [])
        return AssertionResult(
            passed,
            f"done.{path} present",
            "" if passed else f"missing or empty",
        )
    if kind == "markdown_contains":
        value = spec["value"]
        text = _markdown_text(cap)
        passed = value in text
        return AssertionResult(
            passed,
            f"markdown contains {value!r}",
            "" if passed else f"absent (markdown len={len(text)})",
        )
    if kind == "markdown_not_contains":
        value = spec["value"]
        text = _markdown_text(cap)
        passed = value not in text
        return AssertionResult(
            passed,
            f"markdown does NOT contain {value!r}",
            "" if passed else "found (forbidden)",
        )
    if kind == "markdown_contains_any":
        values = list(spec["values"])
        text = _markdown_text(cap)
        passed = any(v in text for v in values)
        return AssertionResult(
            passed,
            f"markdown contains any of {values!r}",
            "" if passed else "none of the substrings present",
        )
    if kind == "summary_contains_any":
        values = list(spec["values"])
        summary = ""
        if cap.done is not None:
            s = cap.done.get("summary")
            if isinstance(s, str):
                summary = s
        passed = any(v in summary for v in values)
        return AssertionResult(
            passed,
            f"done.summary contains any of {values!r}",
            "" if passed else f"got summary={summary[:120]!r}",
        )
    if kind == "warnings_contain":
        value = spec["value"]
        warnings = (cap.done or {}).get("warnings") or []
        passed = any(value in w for w in warnings if isinstance(w, str))
        return AssertionResult(
            passed,
            f"any warning contains {value!r}",
            "" if passed else f"warnings={warnings!r}",
        )
    if kind == "warnings_not_contain":
        value = spec["value"]
        warnings = (cap.done or {}).get("warnings") or []
        offenders = [w for w in warnings if isinstance(w, str) and value in w]
        passed = not offenders
        return AssertionResult(
            passed,
            f"no warning contains {value!r}",
            "" if passed else f"forbidden in warnings: {offenders!r}",
        )
    if kind == "min_citations":
        threshold = int(spec["value"])
        cites = (cap.done or {}).get("source_citations") or []
        passed = len(cites) >= threshold
        return AssertionResult(
            passed,
            f"len(source_citations) >= {threshold}",
            "" if passed else f"got {len(cites)}",
        )
    if kind == "sql_contains":
        value = spec["value"]
        sql = _sql_text(cap)
        passed = value.lower() in sql.lower()
        return AssertionResult(
            passed,
            f"done.sql contains {value!r} (case-insensitive)",
            "" if passed else f"sql={sql[:200]!r}",
        )
    if kind == "sql_not_contains":
        value = spec["value"]
        sql = _sql_text(cap)
        passed = value.lower() not in sql.lower()
        return AssertionResult(
            passed,
            f"done.sql does NOT contain {value!r}",
            "" if passed else f"forbidden in sql={sql[:200]!r}",
        )
    if kind == "stage_emitted":
        target = spec["value"]
        passed = any(s.get("stage") == target for s in cap.stage_events)
        names = [s.get("stage") for s in cap.stage_events]
        return AssertionResult(
            passed,
            f"stage event emitted: {target!r}",
            "" if passed else f"stage events={names!r}",
        )
    if kind == "stage_message_not_emitted":
        target = spec["value"]
        offenders = [
            s for s in cap.stage_events
            if isinstance(s.get("message"), str) and target in s["message"]
        ]
        passed = not offenders
        return AssertionResult(
            passed,
            f"no stage event message contains {target!r}",
            "" if passed else f"forbidden stages={offenders!r}",
        )
    if kind == "meta_field_eq":
        path = spec["path"]
        expected = spec["value"]
        actual = _resolve_path(cap.meta, path)
        passed = actual == expected
        return AssertionResult(
            passed,
            f"meta.{path} == {expected!r}",
            "" if passed else f"got {actual!r}",
        )
    if kind == "meta_field_in":
        path = spec["path"]
        allowed = list(spec["values"])
        actual = _resolve_path(cap.meta, path)
        passed = actual in allowed
        return AssertionResult(
            passed,
            f"meta.{path} in {allowed!r}",
            "" if passed else f"got {actual!r}",
        )

    return AssertionResult(False, f"unknown assertion kind: {kind!r}", "fixture bug")


def _expand_expected(expected: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Expand the `expected` shorthand into normalised assertion dicts."""
    if not expected:
        return []
    out: list[dict[str, Any]] = []
    qt = expected.get("query_type")
    if qt is not None:
        if isinstance(qt, dict) and "one_of" in qt:
            out.append({"kind": "meta_field_in", "path": "query_type", "values": list(qt["one_of"])})
        elif isinstance(qt, list):
            out.append({"kind": "meta_field_in", "path": "query_type", "values": list(qt)})
        else:
            out.append({"kind": "meta_field_eq", "path": "query_type", "value": qt})
    sch = expected.get("schema")
    if sch is not None:
        out.append({"kind": "meta_field_eq", "path": "schema", "value": sch})
    badge = expected.get("badge")
    if badge is not None:
        out.append({"kind": "done_field_eq", "path": "badge", "value": badge})
    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _load_canaries(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    canaries = data.get("canaries") or []
    if not isinstance(canaries, list):
        raise SystemExit(f"{path}: 'canaries' must be a list")
    return canaries


def _select(canaries: list[dict[str, Any]], tiers: set[int], only_ids: set[str]) -> list[dict[str, Any]]:
    out = []
    for c in canaries:
        if only_ids and c.get("id") not in only_ids:
            continue
        if tiers and c.get("tier") not in tiers:
            continue
        out.append(c)
    return out


def _run_canary(
    canary: dict[str, Any],
    base_url: str,
    timeout: float,
    verbose: bool,
) -> tuple[bool, list[AssertionResult], StreamCapture]:
    """Run one canary, return (passed, assertion_results, capture)."""
    cap = stream_one(canary["query"], base_url, timeout)

    if cap.error and cap.done is None:
        # Treat transport-level failures as a single failed assertion so the
        # output format stays consistent.
        return False, [AssertionResult(False, "stream completed with done event", cap.error)], cap

    specs = list(_expand_expected(canary.get("expected")))
    specs.extend(canary.get("assertions") or [])

    results = [_eval_assertion(s, cap) for s in specs]
    passed = all(r.passed for r in results)
    return passed, results, cap


def _print_canary_header(canary: dict[str, Any]) -> None:
    print(f"\n=== {canary['id']} (Tier {canary.get('tier','?')}): {canary['query']!r}", flush=True)


def _print_canary_result(
    canary: dict[str, Any],
    passed: bool,
    results: list[AssertionResult],
    cap: StreamCapture,
    verbose: bool,
) -> None:
    label = "PASS" if passed else "FAIL"
    summary = (
        f"  [{label}] cid={cap.correlation_id} "
        f"total_ms={cap.total_ms:.0f} "
        f"meta_query_type={(cap.meta or {}).get('query_type')!r} "
        f"meta_schema={(cap.meta or {}).get('schema')!r} "
        f"done_type={(cap.done or {}).get('type')!r} "
        f"done_badge={(cap.done or {}).get('badge')!r} "
        f"done_status={(cap.done or {}).get('status')!r}"
    )
    print(summary, flush=True)
    for r in results:
        marker = "PASS" if r.passed else "FAIL"
        line = f"      [{marker}] {r.description}"
        if not r.passed and r.detail:
            line += f" — {r.detail}"
        print(line, flush=True)
    if verbose and not passed:
        print("    --- meta ---", flush=True)
        print(f"    {json.dumps(cap.meta, default=str)}", flush=True)
        print("    --- done ---", flush=True)
        print(f"    {json.dumps(cap.done, default=str)[:2000]}", flush=True)
        print("    --- markdown (first 1500 chars) ---", flush=True)
        print(f"    {_markdown_text(cap)[:1500]}", flush=True)
        print("    --- stages ---", flush=True)
        for s in cap.stage_events:
            print(f"    {s}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run RTIE canary regression set against /v1/stream.",
    )
    parser.add_argument(
        "--tier", action="append", type=int, choices=[1, 2, 3], default=[],
        help="Tier to run. Repeatable: --tier 1 --tier 2.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Run every tier, including Tier 3 (manual).",
    )
    parser.add_argument(
        "--only", default="",
        help="Comma-separated canary IDs to run (overrides --tier).",
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL,
        help=f"RTIE backend base URL. Default {DEFAULT_BASE_URL}.",
    )
    parser.add_argument(
        "--timeout", type=float, default=180.0,
        help="Per-canary timeout in seconds. Default 180.",
    )
    parser.add_argument(
        "--gap-seconds", type=float, default=2.0,
        help="Sleep between canary runs. Default 2.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Dump meta/done/markdown/stages for each FAIL.",
    )
    parser.add_argument(
        "--canaries", default=str(Path(__file__).with_name("canaries.yaml")),
        help="Path to canaries.yaml.",
    )
    args = parser.parse_args()

    fixture_path = Path(args.canaries)
    if not fixture_path.exists():
        print(f"ERROR: canaries fixture not found at {fixture_path}", flush=True)
        return 2
    canaries = _load_canaries(fixture_path)

    if args.only:
        only_ids = {s.strip() for s in args.only.split(",") if s.strip()}
        tiers: set[int] = set()
        selected = _select(canaries, tiers, only_ids)
    elif args.all:
        selected = _select(canaries, {1, 2, 3}, set())
    elif args.tier:
        selected = _select(canaries, set(args.tier), set())
    else:
        # Default: Tier 1 only — the gating set.
        selected = _select(canaries, {1}, set())

    if not selected:
        print("No canaries selected. Use --tier {1,2,3}, --all, or --only IDs.", flush=True)
        return 2

    # Tier 3 needs explicit opt-in even when listed via --tier 3 — the user
    # is asserting they have the local data. Skipped canaries still report
    # a clear reason so the operator knows what's happening.
    skipped: list[tuple[dict[str, Any], str]] = []
    runnable: list[dict[str, Any]] = []
    explicit_tier3 = (3 in set(args.tier)) or args.all or bool(args.only)
    for c in selected:
        if c.get("tier") == 3 and not explicit_tier3:
            reasons = c.get("needs_local_data") or ["Tier 3 (manual)"]
            skipped.append((c, "; ".join(reasons)))
        else:
            runnable.append(c)

    print(
        f"Selected {len(selected)} canaries from {fixture_path.name} "
        f"(runnable={len(runnable)}, skipped={len(skipped)}). "
        f"Backend: {args.base_url}",
        flush=True,
    )

    results: list[tuple[dict[str, Any], bool, list[AssertionResult], StreamCapture]] = []
    for i, canary in enumerate(runnable):
        _print_canary_header(canary)
        passed, asserts, cap = _run_canary(canary, args.base_url, args.timeout, args.verbose)
        _print_canary_result(canary, passed, asserts, cap, args.verbose)
        results.append((canary, passed, asserts, cap))
        if i < len(runnable) - 1 and args.gap_seconds > 0:
            time.sleep(args.gap_seconds)

    print("\n===== SUMMARY =====", flush=True)
    for canary, passed, _asserts, _cap in results:
        label = "PASS" if passed else "FAIL"
        print(f"  [{label}] {canary['id']} (Tier {canary.get('tier','?')}): {canary['query'][:80]}", flush=True)
    for canary, reason in skipped:
        print(f"  [SKIP] {canary['id']} (Tier {canary.get('tier','?')}): {reason}", flush=True)

    failed = [r for r in results if not r[1]]
    print(
        f"\nTotal: {len(results)} run + {len(skipped)} skipped. "
        f"Passed: {len(results) - len(failed)}, Failed: {len(failed)}",
        flush=True,
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

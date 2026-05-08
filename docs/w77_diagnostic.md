# W77 Diagnostic: Response Truncation Root Cause

**Branch:** diagnostic/w77-truncation-investigation
**Date:** 2026-05-08
**Investigator:** Wire-level SSE capture + non-streaming `ainvoke` replay (no production-code changes)

---

## 1. Summary verdict

**Cause #1 confirmed — `max_tokens` cap on the explainer LLM call.**

The hardcoded `max_tokens=4096` at the `LogicExplainer.stream_semantic` (and `explain_semantic`) call sites is too small once you account for **gpt-5-mini's reasoning-token consumption**. gpt-5-mini is a reasoning model: its `completion_tokens` budget is split between `reasoning_tokens` (internal, never shown to the user) and the visible markdown output. With a 4096 cap, the model regularly spends 2,000–4,096 tokens on reasoning, leaving zero-to-partial budget for the visible answer. The OpenAI API returns `finish_reason="length"`. Frontend and SSE plumbing carry the truncated bytes faithfully — the cut happens at the LLM layer.

The mismatch with the W34c Phase 2 prompt context: that promotion (commit `61034a3`) targeted `phase2.explainer.invoke` (the value-tracer), **not** `logic_explainer.*`. The logic-explainer call sites (Phase 4 in the W34c plan, "intentionally absent" per `llm_factory.py:50-52`) are still on the global default `OPENAI_MODEL=gpt-5-mini`. gpt-5-mini reasoning cost is the source of the truncation, not gpt-4o-mini behavior.

---

## 2. Wire payload analysis — stream IS truncated at the wire level

A3 prompt was reproduced live against `localhost:8000/v1/stream` (correlation_id `0993fb7d-1605-42ae-919b-06cb47c0774f`). Capture: `scratch/w77_a3_wire.txt` (60,297 bytes, 1761 SSE events).

Stream timeline:
- `event: stage` ×4 (classify → search → fetch → explain)
- `event: meta` ×1
- `event: token` ×1755
- `event: done` ×1 (terminal)

The `done` event payload's `explanation.markdown` ends mid-clause — same pattern as v2 benchmark Run 7:

```text
- Why is it being changed?
  - The input strings are converted into types/values that match dimension
    keys (date and run key) used later to filter and join to DIM_DATES and
    DIM_RUN (
```

Closing parenthesis is open. Last two `event: token` chunks before `done` were `"_RUN"` and `" ("` — the LLM stopped mid-token-sequence, not on a sentence boundary.

Token count of the streamed body: **1,765 tokens (cl100k) / 1,788 tokens (o200k_base)** for 5,912 chars of markdown. The `done` payload still reports `validated: true, badge: "VERIFIED"` (post-W57 grounding evaluator runs on whatever it sees and doesn't notice the truncation — pre-existing observation, out of scope for W77).

Wire is the source of truth: the truncated bytes leave the server. **Cause #3 (frontend) ruled out.**

---

## 3. Configuration findings — where `max_tokens` is set

The A3 query routes through:

| Path | File | Line |
|---|---|---|
| `/v1/stream` endpoint dispatch | `src/main.py` | 1187 / 1248 |
| `LogicExplainer.stream_semantic` | `src/agents/logic_explainer.py` | 1657 |
| LLM construction inside `stream_semantic` | `src/agents/logic_explainer.py` | **1712-1718** |

The active call:

```python
# src/agents/logic_explainer.py:1712-1718
llm = create_llm(
    provider=provider,
    model=model,
    temperature=self._temperature,
    max_tokens=4096,        # ← hardcoded literal
    json_mode=False,
)
```

The hardcoded `4096` **bypasses** `LogicExplainer._max_tokens` (set from `settings.yaml: llm.max_tokens = 4000` in `main.py:222-226`). Same pattern at `logic_explainer.py:1613-1619` for the non-streaming `explain_semantic`. No env var, no config — change must be at the call site.

`create_llm` is invoked **without** `site=`, so W34c per-site dispatch does not apply; the model resolves to `OPENAI_MODEL` env var = `gpt-5-mini` (`.env.dev:25`). Confirmed by API response `model_name: "gpt-5-mini-2025-08-07"`.

### finish_reason capture (non-streaming replay)

To surface metadata that the streaming path discards, `scratch/w77_finish_reason.py` reconstructs the exact `stream_semantic` call (same SystemMessage `SEMANTIC_EXPLANATION_PROMPT`, same model/provider/temperature/max_tokens, same A3 user prompt with the same source SQL) and uses `llm.ainvoke()` non-streamed. The `AIMessage` then carries the metadata:

```json
{
  "token_usage": {
    "completion_tokens": 4096,
    "prompt_tokens": 1885,
    "total_tokens": 5981,
    "completion_tokens_details": {
      "reasoning_tokens": 4096,
      "accepted_prediction_tokens": 0,
      "audio_tokens": 0,
      "rejected_prediction_tokens": 0
    }
  },
  "model_name": "gpt-5-mini-2025-08-07",
  "finish_reason": "length"
}
```

Visible `response.content` length on this run: **0 chars**. All 4,096 completion tokens went to internal reasoning. `finish_reason="length"` confirms the cap is the proximate cause.

The streamed Run 7 capture (~1,788 visible tokens) and this non-streamed replay (0 visible tokens) are two points on the same distribution: gpt-5-mini's reasoning consumption is variable, and `max_tokens=4096` is a hard ceiling on `reasoning + output`. Anywhere reasoning ≥ ~2,300 tokens, visible output is truncated mid-clause.

### Why this surfaced now and not pre-W34c

Pre-W34c the explainer was on the same gpt-5-mini default but the v2 benchmark was thinner. v2 Run 7 (2026-05-08) added the questions whose graph payloads happen to push reasoning consumption past the threshold. The bug isn't new — the test that reveals it is.

---

## 4. Recommended fix scope

**Cause #1, two viable fixes, listed in the order they should be considered:**

### Option 4a — Promote logic_explainer call sites to gpt-4o-mini (W34c Phase 4-equivalent)

This was already on the W34c roadmap (`llm_factory.py:50-52`: *"Remaining Phase 4 sites … logic_explainer.* are intentionally absent — they will be added individually in later PRs"*). gpt-4o-mini is **not** a reasoning model, so the full `max_tokens=4096` budget is available for visible output, and 4096 visible tokens is comfortably more than the explainer needs (longest observed Run 7 output ≈ 1,800 visible tokens).

Change scope (1-line each in dispatch + 4 call-site additions):

1. `src/llm_factory.py:54-65` — add to `SITE_MODEL_DEFAULTS`:
   ```python
   "logic_explainer.stream_semantic":  "gpt-4o-mini",
   "logic_explainer.explain_semantic": "gpt-4o-mini",
   ```
2. `src/agents/logic_explainer.py:1613` — pass `site="logic_explainer.explain_semantic"` to `create_llm`.
3. `src/agents/logic_explainer.py:1712` — pass `site="logic_explainer.stream_semantic"` to `create_llm`.
4. (Optional same-shape) `_get_llm` at line 1451 for `explain_logic` JSON path.

Re-test outcome: replay A3 + C2; confirm `finish_reason="stop"` and full markdown body. Then re-run v2 benchmark Run 8 to confirm no other regressions.

Risk: gpt-4o-mini's per-token output behavior differs from gpt-5-mini's. W34c Phase 2 already validated this for the value-tracer explainer (no regression on canaries). Phase 4 promotion needs its own canary pass — at minimum the W57 grounding canaries plus a hand-verified A3/C2 pair.

### Option 4b — Bump `max_tokens` on the explainer call sites

Mechanical fix that keeps gpt-5-mini. Change `max_tokens=4096` → `max_tokens=16384` (or higher) at:

- `src/agents/logic_explainer.py:1617` (`explain_semantic`)
- `src/agents/logic_explainer.py:1716` (`stream_semantic`)

Pros: 1-line-each, no model switch, no canary obligation.

Cons: keeps the explainer on a reasoning model whose latency per call is materially higher than gpt-4o-mini's; reasoning consumption will continue to scale with input size and may eventually need another bump; cost per completion is higher because reasoning tokens are billed.

**Recommendation: 4a.** It's already the planned direction; it solves the problem at the source rather than papering over reasoning-budget growth; it aligns the logic_explainer with the value-tracer (both then on gpt-4o-mini) which simplifies future model audits. 4b is the right call only if Phase 4 promotion is gated on additional canary work that won't fit this PR.

---

## 5. Confidence

**High.** Two independent measurements converge:

1. Wire capture: 1,788 visible tokens, body cut mid-clause, `done` event reaches the client cleanly → not a stream-transport issue.
2. Non-streaming replay against the same model with the same `max_tokens`: `finish_reason="length"`, `reasoning_tokens=4096`, `completion_tokens=4096`, visible content empty → reasoning is consuming the cap.

The two runs differ on visible-output count (1,788 vs 0) but agree on the mechanism: gpt-5-mini reasoning consumption against a 4,096 budget can leave anywhere from 0 to ~2,000 tokens for visible output, depending on the model's run-to-run reasoning-effort variance at temperature=1 (forced for gpt-5 family at `llm_factory.py:223-225`).

The `finish_reason="length"` evidence is API-level ground truth — no further capture needed before scoping the fix.

---

## Appendix — Repro commands

All scripts live under `scratch/` (not staged). To re-run on the same backend:

```powershell
# (1) Wire capture: SSE stream from /v1/stream → scratch/w77_a3_wire.txt
powershell -ExecutionPolicy Bypass -File scratch\w77_capture_a3.ps1

# (2) Token / done-payload analysis on the captured wire
python scratch\w77_count_tokens.py

# (3) finish_reason capture via direct ainvoke (bypasses stream layer)
python scratch\w77_finish_reason.py
```

No `src/` files were modified during this diagnostic.

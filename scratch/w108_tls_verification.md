# W108 / W122b — TLS-rationale verification

**Date:** 2026-05-25
**Branch:** `fix/w108-explainer-context-budget-coverage`
**Bundled with:** W108 explainer context-budget coverage fix
**Informs:** W122b cap decision (do not change indexer.py:210 in this PR)

---

## Purpose

`src/agents/indexer.py:209-210` truncates per-function source to 3000 chars
with the comment "Truncate source to keep payload under 2KB (corporate
network TLS limit)" and "~750 tokens, keeps total request under 2KB". W122
diagnostic observed this cap is now the binding constraint on indexer
description quality, since the embedding/description LLM call only ever
sees the first 3000 chars of any function — meaning every function longer
than ~80 lines gets indexed against a truncated prefix.

Before W122b raises the cap, we need to know whether the "TLS payload limit"
rationale is real, partially real, or stale.

This doc records the empirical answer.

---

## 2.1 — Git history finding

**Commit that introduced `max_chars = 3000` in `src/agents/indexer.py`:**

| Field | Value |
|---|---|
| SHA | `39c7162369a783b734f615e4052db5ca877aaa5f` |
| Date | 2026-04-13 14:19:24 +0500 |
| Author | Toheed-Techlogix |
| Files changed | `src/agents/indexer.py` (+24/-6) |

**Commit message (verbatim):**

> Fix indexer: use OpenAI for indexing with truncated source, add JSON fallback
>
> - Switch indexer from Ollama to OpenAI (one-time fast indexing)
> - Truncate source to 3KB to stay under corporate TLS payload limit
> - Add JSON fallback for non-JSON LLM responses
> - Add progress print for each function during indexing

**Verdict on commit message:**

The phrase "corporate TLS payload limit" appears, but with no specific incident
reference, no ticket ID, no error message, no measurement of *where* the
purported limit actually was, and no link to a network-team confirmation. The
constant `3000` (and the "~750 tokens" comment) are quoted with no calibration
data behind them.

This is a **generic "limit request size" rationale**, not a documented incident
response. By the ticket's own classifier: "rationale almost certainly stale".

A second commit (`acaeb059`, 2026-04-28, "refactor: schema-aware source
retrieval and vector store") later duplicated the same `max_chars = 3000` block
into a parallel code path (indexer.py:430) without re-validating the rationale.

---

## 2.2 — Empirical API probe

**Setup:**

- Probe script: `RTIE/scratch/w108_tls_probe.py`
- Model: `gpt-4o-mini` (same as indexer default per `SITE_MODEL_DEFAULTS`)
- Provider/client: `langchain_openai.ChatOpenAI` via `src/llm_factory.create_llm` — same call path as indexer's `_generate_description` (the function that uses the truncated `max_chars` body)
- Machine: this dev box (`Toheed.Asghar` Windows 11), corporate environment
- Network egress: same as indexer runs from
- `.env.dev` loaded via `dotenv.load_dotenv(".env.dev")` — same OPENAI_API_KEY the indexer uses
- Padding: realistic-looking PL/SQL fragment (`CREATE OR REPLACE PROCEDURE ... INSERT ... COMMIT`) repeated to hit target char count, so tokenization density matches real indexer payloads (not lorem ipsum)
- Each call: `SystemMessage` ("Summarize the following PL/SQL source in one short sentence.") + `HumanMessage` containing `Function Name: ... Source Code: <padded body>`. `max_tokens=50`, `temperature=0`, `json_mode=False`.
- Single run per size, sequential (no concurrent load).

**Results:**

| Target chars | Outcome | Round-trip latency | Error |
|---|---|---|---|
| 3,000 | ✅ HTTP 200 | 2,972 ms | None |
| 8,000 | ✅ HTTP 200 | 1,730 ms | None |
| 12,000 | ✅ HTTP 200 | 2,294 ms | None |

**Observations:**

- No `openai.APIConnectionError`, no `openai.APIError`, no `ssl.SSLError`, no
  `httpx.ConnectError`, no `httpx.ReadTimeout`, no TLS handshake failure at
  any size.
- No 4xx/5xx HTTP error at any size.
- Latency does NOT increase monotonically with size; 8K was actually faster
  than 3K. Latency variance is dominated by model inference time + general
  API jitter, not payload size.
- Response `content` was empty on all three runs. This is a model-behavior
  artifact of the synthetic prompt (gpt-4o-mini at `temperature=0` deciding
  the repeated PL/SQL boilerplate doesn't merit a substantive sentence), NOT
  a network failure. The TLS-rationale contract is "did the request go
  through at this payload size" — at all three sizes, the answer is yes.

---

## 2.3 — Verdict

**TLS rationale is STALE.**

Both findings concur:

- **Git history:** rationale was generic ("stay under corporate TLS payload
  limit"), no specific incident cited, no measurement of *where* the
  purported limit actually was.
- **API probe:** all three sizes (3K, 8K, 12K) succeed cleanly. No 4xx/5xx
  errors. No TLS-layer failures. No connection resets. No latency
  degradation with size.

If a corporate TLS payload limit ever existed at ~2-3 KB (no evidence it
did), it does not constrain the indexer's egress path on this machine + this
network as of **2026-05-25**.

**Implication for W122b (separate ticket, do NOT act on this in W108):**

W122b is free to raise `max_chars` in `src/agents/indexer.py:210` (and the
parallel constant at indexer.py:430) to at least 12,000 chars without
risking a TLS-layer regression on the empirically-tested path. The
remaining constraints on the cap value are:

1. **OpenAI request payload limits** (separate from TLS): documented at
   ~1 MB total request size for the Chat Completions API. 12K chars
   per function × number of functions per request ≪ 1 MB, so no
   constraint here at single-function granularity.
2. **Embedding-context limits for `text-embedding-3-small`** (8191 tokens):
   if the indexer uses the same body for both description generation
   AND embedding, the embedding step may truncate. Verify before
   raising past ~30K chars (~7,500 tokens).
3. **gpt-4o-mini context window** (128K tokens, but typically much less
   in practice for description quality): a description-generation
   prompt with 12K chars of source is well within the model's window;
   no constraint here.
4. **Description quality vs source length**: at some point, longer source
   yields diminishing description quality returns. Empirical question,
   not a hard cap. Worth A/B testing during W122b implementation.

**Cap-raise scoping suggestion for W122b:**

- Raise to 12,000 chars as the documented safe ceiling per this probe.
- If embedding-context (item 2) becomes a constraint, drop to ~8,000 for the
  embedding step while keeping 12,000 for the description-generation step.
- The "banner-prepend instead of raising the cap" alternative the ticket
  contemplated is NOT needed — the TLS layer is not the limiting factor.

**Caveats:**

- Probe ran once per size, sequentially, from this single machine. Corporate
  VPN settings, firewall rules, or proxy configurations elsewhere could
  differ. If W122b's indexer runs from a different host (CI runner, prod
  server) — re-run this probe from that host before relying on this verdict.
- The empty-content quirk in the probe responses doesn't affect the verdict
  but is worth noting if anyone re-runs this script and wonders why
  `first_response_snippet` is blank.
- A "no constraint observed" verdict is weaker evidence than a "constraint
  measured at X" verdict — it only proves the limit (if any) is above 12K,
  not that there's no limit. If a limit exists above 12K we will discover it
  via 5xx/connection-reset when W122b tests its chosen value.

---

## Files

- This document: `RTIE/scratch/w108_tls_verification.md`
- Probe script: `RTIE/scratch/w108_tls_probe.py`
- Raw probe stdout: not retained (single ~10-second run; re-runnable via
  `cd RTIE && python scratch/w108_tls_probe.py`)

# W168 — Attribution-consistency gate (routing anchor vs prose subject)

**Status:** design, route-validated on live `91bec4e` (`:8000`). **HOLD — not implemented.**
**Phase:** 1a (the badge-as-guard safety net for wrong-function answers).
**Blast radius:** one badge cap inside `evaluate_grounding`; rides existing warning machinery. No payload-shape change, no `w57_enforce_grounding` signature change.

---

## 1. Problem (recap)

The adversarial run found the badge anti-correlated with correctness: wrong/fabricated answers shipped at VERIFIED·95% when the **routing anchor** (the function the pipeline resolved) was absent from the **prose** the answer actually wrote. Two signals already exist at badge time but are never compared:

- **Routing anchor** — `state["w70_anchor"]` (cascade; `w76_anchor` fallback), forwarded into `evaluate_grounding` ([main.py:1990-1996](../src/main.py#L1990-L1996)).
- **Prose subject** — the function name(s) the answer cites, already extracted by `_w57_check_per_claim_binding` / `_w57_resolve_primary_function`.

W168 computes that disagreement and caps the badge at UNVERIFIED + warns.

## 2. Why a *source*-based gate, not a query_type-only gate

Live route empirics (`91bec4e`, `event: meta.query_type`):

| query | query_type | anchor source class | shape |
|---|---|---|---|
| How is / What writes N_STD_ACCT_HEAD_AMT | VARIABLE_TRACE | — | true fan-in (121) |
| How does CS_GOODWILL_CALCULATION work | VARIABLE_TRACE | — | fan-in (40) |
| Explain logic of F_RET_GRANULARITY_CHECK_FLAG | VARIABLE_TRACE | — | fan-in (40) |
| How is CAP973 / CAP838 calculated? | FUNCTION_LOGIC | `bi_routing`/`classifier_object` (explicit, object_name set) | single anchor (10) |
| **How does the goodwill deduction logic work** | **COLUMN_LOGIC** | `semantic_top1` (weak) | **broad multi-fn (35-40)** |

A *legitimately broad* query lands on COLUMN_LOGIC, so "exclude only VARIABLE_TRACE" is insufficient as the sole protector. The robust discriminator is the **anchor's `source`** (`state["w70_anchor"]["source"]`, taxonomy from [anchor_resolution.py](../src/agents/anchor_resolution.py)):

- **Explicit** (deliberate resolution → fire): `w76_*` (L115-118), `classifier_object` (L132-134), `bi_routing` (L139-141), `raw_query_scan` (L173-175).
- **Weak** (wide-retrieval top-1 cosine, never authoritative → suppress): `semantic_top1` (L208-210) — the **same** signal W150 keys on (L299). On this path anchor-vs-prose disagreement is not a reliable trust signal (W160).

VARIABLE_TRACE exclusion stays as belt-and-suspenders for the true fan-in where even an explicit anchor may be a column legitimately absent from multi-writer prose.

## 3. Predicate

Fire **iff all** hold:

1. **Resolve anchor.** `anchor = w70_anchor` if `w70_anchor.get("function")` non-empty, else `w76_anchor`. `anchor_fn = anchor["function"].strip()`; `anchor_source = anchor.get("source", "")` (when falling back to `w76_anchor` with no source, treat as `w76_prefix`). If no anchor_fn → **no-op**.
2. **Explicit-source gate.** `anchor_source.startswith("w76_")` OR `anchor_source in {"classifier_object", "bi_routing", "raw_query_scan"}`. Otherwise (incl. `semantic_top1`, unknown, or empty) → **no-op**. (Fail-safe = don't fire; deliberate, per §2.)
3. **query_type guard.** `query_type != "VARIABLE_TRACE"` → else **no-op**.
4. **Prose subject extracted.** `prose_fns` = normalized set of function names cited in `markdown` (see §4). Non-empty → else **no-op** (no attributed subject to compare; W135 / per-claim-binding own that case).
5. **Mismatch.** `normalize(anchor_fn) ∉ prose_fns` **AND** `normalize(anchor_fn)` is not a substring of the normalized body (the substring belt — see §4 rationale).

Normalization: `SchemaAwareKeyspace.normalize_function_name` ([keyspace.py:180](../src/parsing/keyspace.py#L180)) — strip/collapse-ws/UPPER; identity discipline (W163/W164), **not** prefix/shape. **Guard:** it *raises* `ValueError` on empty/whitespace/non-string — wrap each call and skip empties.

The set-membership form (not dominance) is what keeps **Q39** ("compare A and B" — anchor is among the prose fns) and **Q9** (anchor *is* the prose subject) safe.

## 4. Prose-subject extraction (new helper)

Add `_w168_extract_prose_function_names(markdown) -> set[str]` reusing the three existing extractors so W168 and per-claim-binding agree on "what the prose cites":

- `_W57_PROSE_FUNCTION_REF_RE` ([L788](../src/agents/logic_explainer.py#L788)) — "function `NAME`", backticked names.
- `_W57_HEADING_AND_RESPONSIBILITY_REF_RE` ([L828](../src/agents/logic_explainer.py#L828)) — `## NAME`, "NAME is responsible for".
- `_W57_FUNC_CITATION_RE` ([L764](../src/agents/logic_explainer.py#L764)) — `(NAME, Lines X-Y)`.

For each candidate: apply `_w57_passes_function_name_filters` (W58 table/column/alias strip), then `normalize_function_name` (guarded), collect into a set. The **dominant** member (highest body count via the `_w57_resolve_primary_function` priority-2 rule) is reported as `prose_lead` in the warning text.

**Substring belt (predicate step 5b) — rationale for review.** The structured extractors only catch the anchor when it's named in a heading / prose-framing / parenthetical citation. If the prose mentions the anchor only informally ("…computed in CS_GOODWILL…" with no backticks/framing) it would be absent from `prose_fns`, producing a false fire. The belt — *also* require the normalized anchor to be absent as a raw substring of the normalized body (mirroring `_w57_check_anchoring`'s `body_upper.count(...)` approach, [L2163-2164](../src/agents/logic_explainer.py#L2163-L2164)) — means W168 fires only when the routed function is **mentioned nowhere in the answer**. `prose_fns` still supplies the "describes X instead" target for the message. **Recommendation: ship with the belt** (false-positives on a trust gate that the user already over-trusts are worse than a missed drift, which Phase 2 backstops).

## 5. Action — rides existing machinery

W168 is a **standalone module-level helper** `_w168_check_attribution_mismatch(markdown, query_type, w70_anchor, w76_anchor) -> List[str]`, called from `evaluate_grounding` **after** the W57 try/except block ([L324](../src/agents/logic_explainer.py#L324)) and **before** `blocking_warnings` is computed ([L331](../src/agents/logic_explainer.py#L331)):

```
        ...                                  # end W57 try/except (L324)
    # W168: attribution-consistency gate. Standalone (not inside
    # w57_enforce_grounding) so query_type is in scope without churning
    # that signature. Pure string comparison — no redis needed.
    if query_type in _REQUIRES_CITATIONS:
        warnings.extend(_w168_check_attribution_mismatch(
            markdown=markdown,
            query_type=query_type,
            w70_anchor=w70_anchor,
            w76_anchor=w76_anchor,
        ))

    blocking_warnings = [                     # L331 — now sees W168
        w for w in warnings if not w.startswith("GROUNDING-LOW:")
    ]
```

Because the warning is appended **before** L331, it enters `blocking_warnings` and flips the badge to UNVERIFIED at [L342-344](../src/agents/logic_explainer.py#L342-L344) (confidence 0.4 with citations, 0.2 without) — **identical** to how W85's `GROUNDING-ANCHOR-MISMATCH-HIGH` blocks. No badge-formula edit.

**Warning string** (prefix is NOT `GROUNDING-LOW:`, so it blocks; `-HIGH` mirrors W85):

```
GROUNDING-ATTRIBUTION-MISMATCH-HIGH: answer describes '{prose_lead}' but query resolved to '{anchor_fn}'
```

Action is **UNVERIFIED-cap, not DECLINED** — the answer may still be partially useful; UNVERIFIED + warning is the honest signal. W46's `ValidationHeader` already renders `warnings` — no frontend change.

**New imports in `logic_explainer.py`:** `from src.parsing.keyspace import SchemaAwareKeyspace` (currently absent). `_REQUIRES_CITATIONS` is already module-level ([L84](../src/agents/logic_explainer.py#L84)).

## 6. Discriminator matrix (corrected after LIVE validation — anchor-ABSENT only)

The catch-set is **anchor-absent / "silent-swap"** drift ONLY: the routed
function name appears NOWHERE in the prose. An earlier draft of this matrix
(and the Phase-1a design report) wrongly listed Q12/Q16/Q48 as caught — that
was falsified by live validation (see the framing-drift rows). Corrected:

| case | anchor_fn (source) | query_type | prose subject | fires? | badge | why |
|---|---|---|---|---|---|---|
| **CAP973 silent-swap** (live-proven ✅) | CS_REGULATORY_…_DEDUCTION_AMOUNT (`bi_routing`) | FUNCTION_LOGIC | REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP (anchor name absent) | ✅ | UNVERIFIED | explicit anchor ∉ prose & not a body substring |
| W76 "In <Fn>" silent-swap | named (`w76_prefix`) | COLUMN_LOGIC | different fn, anchor absent | ✅ | UNVERIFIED | explicit anchor ∉ prose |
| raw_query named-fn silent-swap | named (`raw_query_scan`) | FUNCTION_LOGIC | different fn, anchor absent | ✅ | UNVERIFIED | explicit anchor ∉ prose |
| **Q12/Q16/Q48 framing-drift** (live, NOT caught) | FN_G_TEST_CSTM (explicit) | VARIABLE_TRACE | names FN_G_TEST_CSTM as framing, describes ABL_INV_ASSET_CLASS_RECLASS | ❌ | (unchanged, stays VERIFIED) | **two reasons**: VARIABLE_TRACE gate; AND `anchor ∈ prose` (framing mention) so set-membership won't fire even on COLUMN_LOGIC. → **W169** |
| passing COLUMN_LOGIC (Q1-4/37/38/42/49) | named, == subject | COLUMN_LOGIC | same fn | ❌ | **VERIFIED** | anchor ∈ prose |
| **Q39** two-fn compare | A (explicit) | COLUMN_LOGIC | {A, B} | ❌ | **VERIFIED** | anchor ∈ prose (member) |
| **Q9** broad goodwill logic (live ✅) | CS_GOODWILL_NET_OF_DTL (`semantic_top1`) | COLUMN_LOGIC/VARIABLE_TRACE | CS_GOODWILL_NET_OF_DTL | ❌ | unchanged, no W168 | weak source → suppressed (and anchor ∈ prose anyway) |
| **Q1/Q2 W159 fan-in** (live ✅) | — | VARIABLE_TRACE | 121 writers | ❌ | VERIFIED unchanged | query_type guard |
| no anchor / unanchored semantic | — | any | any | ❌ | unchanged | gate 1/2 no-op |
| C01 / C19 (live ✅) | == prose | COLUMN_LOGIC | same | ❌ | VERIFIED preserved | anchor ∈ prose |

## 7. Honest residual — three classes W168 does NOT own

- **W168 catches** ONLY `anchor ∉ prose` (and not a body substring) on an **explicit** anchor: the LLM silently swapped the routed function for a *different named* one and never mentions the routed name. Live-proven on CAP973. This is much narrower than the original "~14" estimate — and the narrowing is correct.
- **W168 does NOT catch — and structurally cannot:**
  - **Framing-drift (Q12/Q16/Q48)** — the prose names the anchor as framing context ("In the function FN_G_TEST_CSTM, … updated in the ABL_INV_ASSET_CLASS_RECLASS function") while the substantive subject is a *different* function. Because the explainer structurally frames "in `<Fn>`" answers with the anchor name, `anchor ∈ prose` holds and the set-membership predicate (lenient by design for Q39) stays silent — independent of the VARIABLE_TRACE gate. Catching this needs a *dominant-subject* test plus extending the citation extractor to catch `**NAME (Lines X-Y)**` (name outside parens) forms, both of which reopen the Q39/W159 false-positive surface. **Tracked as W169 (diagnose-first: badge-gate vs upstream retrieval/Phase-2).**
  - **W166** (weak-anchor wrong near-twin): the anchor is itself the wrong sibling but the prose *faithfully* describes it → `anchor ∈ prose` → silent. **Phase 2 near-twin abstain / W150** (already hedging — see "What writes N_EOP_BAL" → W150). Suppressing W168 on `semantic_top1` sacrifices **zero** W166 coverage.
  - **Bluffing within the right function** (prose subject == anchor, claims fabricated) → **Phase 2 decline gate**.

## 8. Tests (offline, no backend)

New file `tests/unit/agents/test_w168_attribution_mismatch.py`, mirroring `test_w85_anchor_vs_asked_mismatch.py` + the `evaluate_grounding`-badge integration block in `test_w57_grounding.py:382-446`. **Use the real `source` strings** from `anchor_resolution.py` (`"bi_routing"`, `"classifier_object"`, `"raw_query_scan"`, `"w76_prefix"`, `"semantic_top1"`) — not the illustrative `"object_name"` the W85 test uses (W85 ignores source; W168 reads it).

Helper-level (`_w168_check_attribution_mismatch`):
- **fires** — explicit anchor (`bi_routing`), COLUMN_LOGIC, prose names a different fn, anchor mentioned nowhere → 1 warning, text `GROUNDING-ATTRIBUTION-MISMATCH-HIGH`.
- **fires** — `w76_prefix` and `raw_query_scan` variants (parametrize the explicit set).
- **no-fire (suppressed)** — `semantic_top1` anchor, anchor ∉ prose. ← the Q9/W160 guard.
- **no-fire (excluded)** — `query_type="VARIABLE_TRACE"`, anchor ∉ prose. ← the **W159 fan-in** guard.
- **no-fire (member)** — anchor ∈ prose set (Q39: prose = {A, B}, anchor = A).
- **no-fire (substring belt)** — anchor absent from structured `prose_fns` but present as informal body substring.
- **no-fire** — no anchor; empty `prose_fns`; `w70_anchor` empty-dict falls back to `w76_anchor`; non-dict anchor safe no-op; `normalize_function_name` ValueError-input guarded.
- case-insensitive / whitespace-vs-underscore match (anchor `CS_Net_AT1` vs prose `CS_NET_AT1`).

**Grounding-machinery test (required) — mirrors `test_evaluate_grounding_d1_caveat_flips_badge`:**
```python
def test_w168_warning_flips_badge_via_evaluate_grounding():
    """The W168 warning must route through blocking_warnings → UNVERIFIED,
    exactly like W85's GROUNDING-ANCHOR-MISMATCH-HIGH. Proves the prefix is
    not 'GROUNDING-LOW:' and that insertion precedes blocking_warnings."""
    md = "## Logic in `OTHER_FUNCTION`\nStep 1 (Lines 5-10)."   # anchor named nowhere
    result = evaluate_grounding(
        raw_query="How is CAP973 calculated?",
        markdown=md,
        multi_source={"OTHER_FUNCTION": {"source_code": _src(100)}},
        functions_analyzed=["OTHER_FUNCTION"],
        query_type="FUNCTION_LOGIC",
        redis_client=None,
        w70_anchor={"function": "CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT",
                    "source": "bi_routing", "confidence": "high"},
    )
    assert result["badge"] == "UNVERIFIED"
    assert result["confidence"] == 0.4          # citations present → 0.4 bucket
    assert any("GROUNDING-ATTRIBUTION-MISMATCH-HIGH" in w for w in result["warnings"])
```
Plus a companion `..._verified_when_anchor_in_prose` asserting VERIFIED is preserved when the anchor *is* the heading subject (the Q9/passing-COLUMN_LOGIC non-regression).

## 9. Regression gate before merge

- Full unit suite (`pytest tests/unit/`) — expect the 5 pre-existing fails only.
- Canary tier-1 (`run_canaries.py --tier 1`) — confirm C01/C19 stay VERIFIED.
- Live re-run of Q1/Q2/Q9 (`:8000`) — confirm badge unchanged (W159 fan-in + Q9 broad COLUMN_LOGIC not downgraded), and a constructed explicit-anchor-drift query downgrades to UNVERIFIED.

## 10. Open questions for review

1. **Substring belt (§4):** ship it (recommended) or set-membership only? Trade FP-resistance vs catch-breadth.
2. **Fail-safe direction (§3 step 2):** fire only on the explicit allow-list (recommended) vs fire on "anything not `semantic_top1`". The allow-list is safer against future anchor `source` values.
3. **Confidence on the no-citation branch:** current formula yields 0.2 when blocking + no citations. W168 only runs for `_REQUIRES_CITATIONS` types which usually carry citations → 0.4; accept the existing bucket (no special-casing), consistent with W85.

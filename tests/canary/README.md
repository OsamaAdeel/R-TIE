# RTIE canary regression set

18 end-to-end queries that exercise the full request pipeline (`/v1/stream`)
and assert on the captured SSE payload. Used as a gating set before any LLM
model swap or routing change.

The set carves out of three pre-existing query corpuses — `tests/integration/test_live_stream.py`,
`scratch/w34_canary_runner.py`, and the W35 Phase 4 canary list — plus a
small number of new entries.

## Files

- `canaries.yaml` — the 18-query fixture. Each entry: id, tier, query text,
  expected/assertions, what-it-protects one-liner, optional needs-local-data
  notes.
- `run_canaries.py` — the runner. Reuses the curl-based SSE loop from
  `scratch/w34_canary_runner.py` (curl `--no-buffer` is load-bearing — see
  the docstring there for why).
- This README.

## Quick start

From the project root (`RTIE/`):

```
# Tier 1 — happy paths + UNSUPPORTED routes (default)
python tests/canary/run_canaries.py

# Same as above, explicit
python tests/canary/run_canaries.py --tier 1

# Tier 2 — DATA_QUERY surface, pinned SQL semantics
python tests/canary/run_canaries.py --tier 2

# Both gating tiers
python tests/canary/run_canaries.py --tier 1 --tier 2

# Everything, including Tier 3 (manual; needs local Oracle data state)
python tests/canary/run_canaries.py --all

# A single canary by id
python tests/canary/run_canaries.py --only C05

# Verbose output for failing canaries (dumps meta/done/markdown/stages)
python tests/canary/run_canaries.py --tier 1 --verbose
```

The runner exits 0 if all selected canaries pass, 1 otherwise.

## Prerequisites

- Backend running at `http://localhost:8000` (`python run.py` from `RTIE/`).
- Redis populated (`docker-compose up -d` then `python cli.py index`).
- Oracle reachable for DATA_QUERY/VALUE_TRACE canaries.
- `curl` on `PATH`. Ships with Windows 10+ and most Linux/macOS systems.

`yaml` and `httpx` come with the project's poetry deps; `curl` is the only
external binary the runner shells out to.

## Tier definitions

| Tier | When to run | Count | Runtime |
|---|---|---|---|
| 1 | Before any LLM model swap (W34c phase 2+, future routing changes) | 10 | ~2-3 min |
| 2 | Before promoting `data_query._generate_sql` to a smaller model | 5 | ~2 min |
| 3 | Manual; needs local Oracle data state or specific Redis state | 3 | varies |

Tier 1 includes:

- Function-name happy path (FN_LOAD_OPS_RISK_DATA, CS_Deferred_Tax_…).
- W45 ungrounded paths (FAKEXYZ — no-underscore column shape; CAP999 — BI shape).
- UNSUPPORTED classifier rules (reconciliation, forecasting).
- Function-not-found DECLINED.
- Phase 7 BI routing (CAP943).
- Grounded VARIABLE_TRACE in OFSMDM.
- One DATA_QUERY anchor (C05, the SUM = -24,179,237,139.63).

The W49 partial-source path (C10 — ABL_Def_Pension) is currently **Tier 3**;
the function isn't in `graph:OFSERM` on the local Redis state, so the path
is unreachable. See "Tier 3 manual procedure" below.

Tier 2 concentrates on DATA_QUERY/VARIABLE_TRACE shape — schema-pivot to
OFSERM, SQL semantics (`RTRIM`, schema-qualified table refs, expected
row counts), W22 identifier-ambiguity short-circuit. These take longer
to verify than Tier 1 because they pin semantic SQL output, not just
markdown structure.

Tier 3 needs an operator to confirm the local Oracle state (specific
account/date combinations exist); skipped by default.

## C05 SUM anchor — single-LV note

`C05` pins the substring `-24,179,237,139.63` (or numeric `-24179237139.63`)
as the SUM of `N_EOP_BAL` for `V_LV_CODE='ABL'` on `2025-12-31`. Local
OFSMDM data is single-LV, so a no-filter SUM equals the filtered SUM under
current state — which is why a separate "no-filter SUM" canary (C19 in the
proposal) was dropped. If LV diversity is added later, add a second
DATA_QUERY canary that exercises the no-filter shape against the new
expected SUM.

## Authoring a new canary

1. Add a YAML entry to `canaries.yaml` under `canaries:`. Required fields:
   `id`, `tier`, `query`, `protects`. Optional but encouraged: `expected`
   (shorthand), `assertions` (list), `notes`, `needs_local_data` (Tier 3).

2. Pick assertion kinds from the menu at the top of `canaries.yaml`. Most
   common patterns:

   - **FUNCTION_LOGIC happy path** — `expected.query_type=COLUMN_LOGIC`,
     `expected.badge=VERIFIED`, `markdown_contains: "This function runs in"`,
     `min_citations: 1`, `warnings_not_contain: UNGROUNDED_IDENTIFIERS`.

   - **DATA_QUERY anchor** — `done_field_eq` on `type=data_query` +
     `status=answered`, `summary_contains_any` on the known SUM, `sql_contains`
     for column/filter substrings, optional `done_field_eq` on `row_count`.

   - **DECLINED** — `done_field_eq: badge=DECLINED`, `done_field_eq: type=…`
     (`function_not_found`, `unsupported`, `identifier_ambiguous`, …).

   - **W45 ungrounded** — `warnings_contain: UNGROUNDED_IDENTIFIERS`,
     `markdown_contains` for the "Not Found in Indexed Functions" title,
     `markdown_not_contains: "This function runs in"` (no hierarchy header).

3. For canaries with legitimate variability (e.g. BI promotion that may or
   may not change `query_type`), use `expected.query_type: {one_of: [A, B]}`.

4. If the canary needs local Oracle data state, mark `tier: 3` and list
   the required state under `needs_local_data:`.

5. Run `python tests/canary/run_canaries.py --only <new_id>` to validate.
   If the canary FAILs and you can confirm the underlying behaviour is
   correct, the assertion is mis-pinned — adjust the substring/expected
   value, don't loosen the assertion silently.

## Make targets

From `RTIE/`:

```
make canary-tier1     # Tier 1 only (default gating)
make canary-tier2     # Tier 2 only (DATA_QUERY surface)
make canary-all       # Tier 1 + Tier 2 (Tier 3 still skipped)
```

The `make` targets just shell out to `python tests/canary/run_canaries.py`
with the appropriate flags. They do NOT start the backend — that's a
separate `python run.py` step the operator owns.

## Tier 3 manual procedure

Tier 3 canaries (`C08`, `C10`, `C17`) need specific local Oracle data state
or specific Redis state to be meaningful. They're skipped by default.

### C08 / C17 — Oracle account/date data state

Need account/date combinations to exist in the local Oracle. To run:

1. Confirm the account/date pair listed under `needs_local_data:` exists
   in the staging schema. A quick check:
   ```sql
   SELECT COUNT(*) FROM OFSMDM.STG_PRODUCT_PROCESSOR
   WHERE V_ACCOUNT_NUMBER = 'PK00108091TR00PKRGBP-T24-LIVEPOSG'
     AND FIC_MIS_DATE = DATE '2025-12-31';
   ```
2. If present:
   ```
   python tests/canary/run_canaries.py --only C08
   python tests/canary/run_canaries.py --only C17
   ```
3. If the account isn't local, leave Tier 3 skipped — the assertions
   would degrade to "phase2 declined gracefully," which is a degenerate
   regression check.

### C10 — W49 partial-source path

Needs `ABL_DEF_PENSION_FUND_ASSET_NET_DTL` (or another function) present in
`graph:OFSERM` *as metadata only* — i.e. registered in the graph but with
the source body absent from the OFSMDM-only vector store. That's the
specific state W49 fires on: the function is "known" but its body is "not
currently indexed."

On the canary expansion's first pinning run, this function was absent from
Redis entirely (no `graph:OFSERM:ABL_DEF_PENSION*` key), so the
function-precheck DECLINEd with `function_not_found` rather than the W49
"Source Not Currently Indexed" structured response.

Resolution options (decision deferred):

- **(a)** Re-index the OFSERM pension file. Confirm the file still exists
  under `db/modules/`, then `python cli.py index --force`. Verify with
  ```bash
  python -c "import redis; r=redis.Redis(); print(r.exists(b'graph:OFSERM:ABL_DEF_PENSION_FUND_ASSET_NET_DTL'))"
  ```
  Expected: `1`. Once present, run `python tests/canary/run_canaries.py --only C10`.

- **(b)** Re-pin C10 to a different function that's metadata-only-no-source
  on current Redis state. To find candidates:
  ```python
  # Functions with graph:OFSERM:<fn> present but no graph:source:OFSERM:<fn>
  import redis
  r = redis.Redis(host='localhost', port=6379)
  graph_keys = {k.decode().split(':')[-1] for k in r.keys(b'graph:OFSERM:*')
                if len(k.decode().split(':')) == 3}
  source_keys = {k.decode().split(':')[-1] for k in r.keys(b'graph:source:OFSERM:*')}
  print(graph_keys - source_keys)
  ```
  Pick a function from the diff, update the canary's `query` and the
  `markdown_contains` substrings, leave the assertion structure intact.

Until one of these lands, C10 stays in Tier 3 and skips by default.

## W42 sanitization (manual canary, not in YAML)

The W42 LLM-error sanitization path can't run as a normal canary because
it requires forcing an `AuthenticationError` (e.g., bad API key). Manual
procedure when validating that path:

```
$env:OPENAI_API_KEY = "sk-INVALID-FOR-TEST"
python run.py        # restart with bad key
python tests/canary/run_canaries.py --only C01

# Expected:
#   done.type        == "llm_api_error"
#   done.category    == "AuthenticationError"
#   done.warnings[0] starts with "LLM_API_ERROR: AuthenticationError"
#   user_message     contains no Python internals or stack info
```

Restore the real `OPENAI_API_KEY` after the check.

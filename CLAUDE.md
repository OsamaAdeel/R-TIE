# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo layout

The actual project lives in the `RTIE/` subdirectory, not at this directory's root. All commands below assume `cwd = RTIE/`. Before running anything, `cd RTIE` (or use `git -C RTIE` for git commands). Files outside `RTIE/` are scratch experiments and `tmp_*` artifacts; ignore them unless asked.

For project background, capabilities, query routing, Phase 1/Phase 2/Option A architecture, frontend SSE flow, CLI/slash commands, and env vars, read `RTIE/README.md` first — it is the canonical reference.

## Common commands

Backend:
```bash
python run.py                 # NOT `uvicorn src.main:app` — run.py sets WindowsSelectorEventLoopPolicy first (psycopg requires it)
python cli.py index --force   # one-time / after adding modules
python cli.py ask "…"
```

Tests (asyncio_mode = auto is already configured in pyproject.toml):
```bash
python -m pytest tests/unit/ -v                              # full unit suite
python -m pytest tests/unit/parsing/test_loader.py -v        # one file
python -m pytest tests/unit/parsing/test_loader.py::test_x   # one test
python tests/integration/test_live_stream.py                 # integration (needs backend + Redis + Oracle running)
```

Canary regression set (the gate before model swaps / routing changes; needs backend running on :8000):
```bash
python tests/canary/run_canaries.py --tier 1       # happy paths + UNSUPPORTED routes
python tests/canary/run_canaries.py --tier 2       # DATA_QUERY semantics
python tests/canary/run_canaries.py --all          # includes Tier 3 (manual, needs Oracle state)
python tests/canary/run_canaries.py --only C05     # single canary by id
make canary-tier1                                  # Makefile wrapper
```
Fixture: `tests/canary/canaries.yaml`. SSE driver uses curl `--no-buffer` (load-bearing). The runner does NOT start the backend.

Frontend (`cd RTIE/frontend`): `npm run dev` / `npm run build` / `npm run lint`.

Infra: `docker-compose up -d` brings up Redis Stack (6379, includes RediSearch) and PostgreSQL (5432).

Env: loader is `load_dotenv(f".env.{ENVIRONMENT}")` in `src/main.py` (default `dev` → `.env.dev`); `cli.py` hardcodes `.env.dev`. `requirements.txt` now exists (committed in 8fb3e76 on audit/w134); use either `poetry install` (canonical) or `pip install -r requirements.txt` (pip-only environments). The two must be hand-synced on dep changes — `requirements.txt` is not auto-regenerated from `pyproject.toml`.

## Project conventions

**W## work tickets.** Every meaningful change is tagged with a W-number (W35 active multi-phase; W57, W70, W78a calibration/anchor; W79, W81 feature work — illustrative, not exhaustive) tracked in `docs/wXX_*.md` and `scratch/wXX_*.md`. Branches follow `<type>/<wxx-slug>` (e.g. `refactor/w35-phase1-schema-aware-foundation`, `diagnostic/w43-graph-fallback`). When the user references "W47" or "Phase 0.5", read the matching doc in `RTIE/docs/` before acting. Some test failures are pre-existing and tagged with their W-number (e.g. W48) — don't fix unrelated W-flagged failures unless asked.

**Phases vs. Options.** "Phase 1" = logic explainer (graph pipeline). "Phase 2" = value tracer (row-first). "Option A" = data query (LLM-generated SQL with three safeguards). These names appear in code paths, branch names, and PR titles.

## Schema-aware refactor in flight (W35)

There is an active multi-phase refactor making `schema` a first-class parameter throughout the loader, indexer, store, agents, and streaming layer. Phases 0-4 have landed (`SchemaAwareKeyspace` at `src/parsing/keyspace.py`, `schema_discovery` at `src/parsing/schema_discovery.py`, multi-schema catalogs/origins, OFSERM source retrieval, schema-aware DATA_QUERY/VARIABLE_TRACE routing); Phases 5-8 (business-identifier indexing + routing) remain. Read `docs/w35_diagnostic.md` (hardcoded-OFSMDM inventory, Section 1 = work list), `docs/w35_architecture.md`, and the latest `docs/w35_phaseN_summary.md` before touching parsing, store, agents, or `main.py`. Grep `OFSMDM` across `src/**/*.py` to see the active inventory.

**Do NOT add new hardcoded `"OFSMDM"` defaults.** Thread schema through as a parameter, even if you have to add it to a signature. The diagnostic doc explicitly tracks each site that needs converting.

## Redis key conventions

All graph/source/cache keys are namespaced by schema. The layout (which is what `SchemaAwareKeyspace` centralizes):

| Pattern | Owner |
|---|---|
| `graph:{schema}:{fn}` | per-function MessagePack-compressed graph |
| `graph:full:{schema}` | full module graph |
| `graph:index:{schema}` | global column → node-id index |
| `graph:source:{schema}:{fn}` | raw PL/SQL source cache (loader-managed) |
| `rtie:logic:{schema}:{fn}` | logic-explainer cache (CacheClient-managed) |

The two source/logic caches (`graph:source:*` vs. `rtie:logic:*`) are tracked for Phase 8 rationalization — leave them alone for now.

After indexing, `graph:index:OFSERM` should be ~385 KB and `graph:OFSERM:*` should hold ~166 functions; `graph:index:OFSMDM` ~40 KB with ~12 functions. Diverging materially from those baselines means a parser/loader regression (counts grow as new modules are added — re-baseline after a deliberate add).

## Don't

- Don't touch the frontend in backend-only refactors. The response payload schema is depended on by `frontend/src/api/client.js` and `MessageBubble.jsx` — changing it silently breaks the UI.
- Don't write SQL outside `SqlGuardian`. Every Oracle query goes through `src/tools/sql_guardian.py` (SELECT-only, AST-validated, bind-params only) and `src/tools/schema_tools.py`. There are no exceptions, even for "obviously safe" reads.
- Don't bypass `metadata_interpreter` for source code. It's the single resolver for "give me the source of function X" across Redis cache, Oracle DDL fetch, and on-disk `db/modules/`.
- Don't add proactive batch-detection, forecasting, write operations, or speculation features. The README's "What's Excluded" list is a deliberate scope boundary, not a backlog.
- Don't probe `/v1/query` for badge / warnings / grounding signals. That endpoint returns the raw LangGraph state (`final_state["output"]`) and skips the W57 grounding overlay. Only `/v1/stream` invokes `evaluate_grounding` — the `event: done` payload of the SSE stream is the source of truth for `badge` / `validated` / `warnings` (and is what the frontend reads).
- Don't blanket-taskkill the backend. `taskkill /F /IM python.exe` kills every Python process on the machine, including agent workers in other terminals. Find the specific PID and kill only it:
```
netstat -ano | findstr :8000   # find PID owning port 8000
taskkill /PID <pid> /F          # kill only that PID
python run.py                   # restart from RTIE/
```
- Don't improvise the post-approval close-out. Once Toheed approves a fix, run this sequence as-is — no manual changes, no skipped steps:
```
git push -u origin <branch>
git checkout main
git pull origin main
git merge --no-ff <branch>
git push origin main
git branch -d <branch>
git push origin --delete <branch>
```
Then report: commit SHA, merge SHA, and that the branch is deleted local + remote. No `Co-Authored-By: Claude` trailer.
- Don't trust a parallel-worktree launch (`git worktree add ../RTIE-<branch>`) without verifying. Two gotchas: (1) `.env.dev` is gitignored and only lives in the original checkout — copy it across before `python run.py`. (2) `sys.path` may pin to the original checkout's `src/` regardless of cwd; in PowerShell set `$env:PYTHONPATH = (Get-Location).Path + '\src'` explicitly. Verify the worktree's code is actually running by hitting a query and grepping the worktree's `logs/app.log` (not the original's) for a known signature line. Backlog item ("Worktree environment hygiene") tracks the permanent fix.

## Where to look first

- New module added → `src/parsing/loader.py` (startup pipeline) and `src/parsing/manifest.py`.
- Query routed wrong → `src/agents/orchestrator.py` (7-type classifier + ambiguity rule).
- Graph misses a variable → `src/parsing/query_engine.py` (alias resolution, column index, upstream discovery).
- Value trace gives wrong origin → `src/phase2/origin_classifier.py` and `src/phase2/origins_catalog.py` (bootstrap-validated catalog).
- LLM hallucinated → `src/phase2/explainer.py` (sanity check on forbidden phrases) or `src/agents/logic_explainer.py` (`detect_ungrounded_identifiers`).
- SSE event missing → `src/main.py` `/v1/stream` endpoint.
- LangGraph state shape → `src/pipeline/state.py` (`LogicState` TypedDict) and `src/pipeline/logic_graph.py` (StateGraph + conditional edges).
- Badge wrong / unexpected GROUNDING-HIGH warning → `src/agents/logic_explainer.py` `w57_enforce_grounding` (seven `_w57_check_*` sub-checks + W83a's December paraphrase = 8 total, all invoked in order). Only runs from `/v1/stream` after the LangGraph pipeline. Content checks anchor on the asked-about function via `_w57_resolve_primary_function` (W76) and validate against ONE source, not concatenated `multi_source`.

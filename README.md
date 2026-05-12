# RTIE — Regulatory Trace & Intelligence Engine

RTIE is a read-only multi-agent system that answers questions about an Oracle OFSAA FSAPPS deployment — explaining PL/SQL logic, tracing column lineage, classifying row origins (PL/SQL vs ETL), and executing validated SELECT-only SQL for aggregation questions. Built for Techlogix engineers working on Basel III/IV regulatory capital computations, it grounds every answer in parsed code or fetched data and declines explicitly when it can't.

This README is the onboarding entry point for a Techlogix engineer landing on RTIE for the first time. It assumes Python and Docker baseline familiarity and that Oracle access has been arranged. It does not cover provisioning OFSAA-shaped Oracle schemas or loading regulatory data — that is multi-day infrastructure work owned by the DBA team.

---

## What RTIE isn't

RTIE deliberately refuses several adjacent capabilities. These are scope choices, not backlog items:

| Out of scope | Why |
|---|---|
| Write operations of any kind | The Oracle service account is SELECT-only. `SqlGuardian` rejects DML/DDL at the application layer as a second defense. |
| Forecasting or prediction | RTIE is read-only introspection. "Which accounts will fail next quarter?" routes to UNSUPPORTED and declines. |
| Cross-table reconciliation against FCT / result tables | FCT lives in the OFSAA Results schema, which is outside RTIE's parsed graph scope. |
| References to tables not in `db/modules/*/functions/` | If a table isn't parsed into the graph, RTIE returns a capability-limitation response rather than guessing. |
| Speculation about upstream ETL systems | For rows with `V_DATA_ORIGIN = T24` / `OF` / `IBG` / `CBS`, RTIE states the origin and suggests where to investigate. It does not speculate about why the upstream value is what it is. |
| Proactive batch detection | RTIE is reactive — engineers ask, the system answers. No watchers, no alerts. |

A confidently wrong answer is worse than no answer. The architecture prioritizes refusal over guessing.

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11+ (`pyproject.toml` declares `^3.11`) | 3.12 / 3.13 also fine |
| Poetry | 1.x or 2.x | Install via `pip install poetry`. There is no `requirements.txt`. |
| Docker Desktop (Windows) or Docker Engine (Linux) | Recent | Used for Redis Stack + PostgreSQL |
| Node.js + npm | LTS (20.x / 22.x) | For the React/Vite frontend |
| Oracle access | OFSAA FSAPPS with OFSMDM and OFSERM schemas | Read-only credentials. Provisioning is out of scope — assumed arranged by the DBA team. |
| OpenAI API key | — | Required: classification, embeddings, indexing, generation |
| Anthropic API key | — | Optional: frontend model selector can pick Claude |
| LangSmith API key | — | Optional: tracing/observability |

A standalone Windows-from-scratch walkthrough (Git install, WSL 2, Docker Desktop, every download link) lives at [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md). The Windows quick start below assumes those prerequisites are already in place.

---

## Quick start — Linux / macOS

All commands run from inside the `RTIE/` directory (the repo root containing `pyproject.toml`, `run.py`, `docker-compose.yml`).

```bash
# 1. Install Python dependencies
poetry install
poetry shell                       # or prefix every command with `poetry run`

# 2. Create .env.dev from the template, fill in secrets
cp .env.example .env.dev
$EDITOR .env.dev                   # set ORACLE_*, OPENAI_API_KEY, etc.

# 3. Start Redis Stack + PostgreSQL
docker-compose up -d
docker ps                          # expect rtie-redis and rtie-postgres, both Up

# 4. Verify Redis (RediSearch is required for vector search)
docker exec -it rtie-redis redis-cli PING        # → PONG

# 5. Verify Postgres (used as the LangGraph checkpointer)
docker exec -it rtie-postgres pg_isready -U postgres   # → accepting connections

# 6. Place PL/SQL sources under db/modules/<MODULE>/functions/*.sql
#    (skip if a teammate has already populated db/modules/)

# 7. One-time index — parses sources, generates descriptions, builds vectors
python cli.py index --force

# 8. Start the backend (do NOT run `uvicorn src.main:app` directly — see below)
python run.py                      # listens on http://localhost:8000

# 9. Start the frontend in a second terminal
cd frontend
npm install
npm run dev                        # serves http://localhost:5173
```

Open [http://localhost:5173](http://localhost:5173) in the browser. The chat UI streams responses from `/v1/stream`.

**Why `python run.py` and not `uvicorn`.** [run.py](run.py) sets `WindowsSelectorEventLoopPolicy` on Windows before importing uvicorn. The psycopg async driver requires `SelectorEventLoop`; Windows' default `ProactorEventLoop` is incompatible. The launcher is a no-op on Linux but harmless to use everywhere.

---

## Quick start — Windows (PowerShell)

Same flow, with PowerShell-specific paths and environment-variable syntax. If you're starting from a clean Windows 11 machine, follow [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md) first; the steps below assume Python, Docker Desktop, Node.js, and Git are already installed.

```powershell
# All commands run inside the RTIE folder.

# 1. Install dependencies
pip install poetry
poetry install
poetry shell

# 2. Create and edit .env.dev
Copy-Item .env.example .env.dev
notepad .env.dev

# 3. Start infrastructure (Docker Desktop must be running first)
docker compose up -d
docker ps                          # expect rtie-redis and rtie-postgres, both Up

# 4. Verify Redis
docker exec -it rtie-redis redis-cli PING        # → PONG

# 5. Verify Postgres
docker exec -it rtie-postgres pg_isready -U postgres

# 6. Index sources
python cli.py index --force

# 7. Start backend
python run.py

# 8. In a second PowerShell window — start frontend
cd frontend
npm install
npm run dev
```

**Restarting the backend on Windows — never blanket-kill Python.** `taskkill /F /IM python.exe` kills every Python process on the machine, including agent workers in other terminals. Find the specific PID and kill only it:

```powershell
netstat -ano | findstr :8000      # find the PID owning port 8000
taskkill /PID <pid> /F             # kill only that PID
python run.py                      # restart
```

---

## Verifying it works — the canary triple

Three queries exercise the three core capabilities. Run them from the UI (or via `python cli.py ask "…"`) after the backend is up. The expected outcomes below are the trust contract — if any one diverges, something in the setup or the index is wrong.

| # | Query | Expected outcome |
|---|---|---|
| 1 | `How does FN_LOAD_OPS_RISK_DATA work?` | **UNVERIFIED** badge. Body explains the function. Warnings array contains a `GROUNDING-HIGH:` entry catching either the "pass-through" template phrase or the line-198-369 padding fabrication. Route: `COLUMN_LOGIC`, schema `OFSMDM`. |
| 2 | `What is the total N_EOP_BAL for V_LV_CODE='ABL' on 2025-12-31?` | **VERIFIED** badge. `SUM(N_EOP_BAL) = -24,179,237,139.63` (exact). Route: `DATA_QUERY`, schema `OFSMDM`. SQL contains `V_LV_CODE` and `FIC_MIS_DATE`. |
| 3 | `How is CAP973 calculated?` | **UNVERIFIED** badge. Body anchors on `CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT`. Warnings array contains a `GROUNDING-HIGH:` entry from the W57 enforcer. |

The first and third are deliberate trust-contract tests: the body looks plausible but contains a fabrication that W57's grounding overlay catches and downgrades. The middle one is the deterministic data path — the SUM is exact, not approximate, and stamps the SQL into the response.

The formal canary suite lives at [tests/canary/canaries.yaml](tests/canary/canaries.yaml) (18 queries across 3 tiers); run it with:

```bash
python tests/canary/run_canaries.py --tier 1     # backend must be running on :8000
make canary-tier1                                # Makefile wrapper, same thing
```

---

## Architecture at a glance

```
                ┌─────────────────────────────────────────────┐
                │   Orchestrator — 7-type query classifier    │
                └────────────────────┬────────────────────────┘
                                     │
       ┌───────────────────┬─────────┼─────────┬──────────────────┐
       ▼                   ▼         ▼         ▼                  ▼
  FUNCTION_LOGIC      VARIABLE_     DATA_     VALUE_         UNSUPPORTED
  COLUMN_LOGIC          TRACE       QUERY     TRACE
       │                   │         │         │                  │
       └─── Logic Explainer ┘     Data Query   Value Tracer      Decline
       (Phase 1: graph trace)    (Option A:   (Phase 2:         (explicit
                                  SQL gen +    row-first)        capability
                                  guardian)                      limitation)
                                     │
                                     ▼
                       ┌────────── LangGraph pipeline ──────────┐
                       │  classify → retrieve → generate →      │
                       │  validate (W57 grounding overlay) →    │
                       │  render (SSE stream)                   │
                       └────────────────────────────────────────┘
```

Six things to know:

1. **Orchestrator** ([src/agents/orchestrator.py](src/agents/orchestrator.py)) classifies every query into one of seven types: `FUNCTION_LOGIC`, `COLUMN_LOGIC`, `VARIABLE_TRACE`, `VALUE_TRACE`, `DIFFERENCE_EXPLANATION`, `DATA_QUERY`, `UNSUPPORTED`. The ambiguity rule defaults to `VALUE_TRACE` unless explicit aggregation keywords ("total", "sum", "count") AND absence of a specific account number trigger `DATA_QUERY`.

2. **Phase 1 — graph pipeline.** At startup, `db/modules/*/functions/*.sql` is parsed into a compressed graph (~86% smaller than raw source) stored in Redis under `graph:{schema}:{fn}`. At query time, the query engine resolves a target column to a subgraph of ~2-4KB and sends only that to the LLM. Source for the W35 schema-aware refactor: `src/parsing/` plus `docs/w35_*.md`.

3. **Phase 2 — value tracer.** "Why is N_EOP_BAL X for account Y?" starts from the row, not the graph. The row's `V_DATA_ORIGIN` column determines the trace strategy: PL/SQL-computed values walk the graph, ETL-loaded values point to the source system, unknown origins surface row facts and suggest investigation paths. Catalog is auto-derived from the parsed graph at startup. Source: `src/phase2/`.

4. **Option A — data query.** Aggregation/filter/count/time-series questions go through an LLM-generated SQL path with three safeguards: row-count pre-check (rejects >10K rows), aggregation-preference prompt, and mandatory `FETCH FIRST 100 ROWS ONLY` injection for row-list queries. Every query passes through `SqlGuardian` (SELECT-only, AST-validated, bind params only) before execution. Source: `src/agents/data_query.py`, `src/tools/sql_guardian.py`.

5. **W57 grounding overlay.** After the LangGraph pipeline generates a response, `evaluate_grounding` in [src/agents/logic_explainer.py](src/agents/logic_explainer.py) runs eight sub-checks against the retrieved source — chain coherence, per-claim binding, citation hygiene, template-phrase detection, paraphrase grounding, December-only execution claims, etc. Failures emit warnings tagged `GROUNDING-HIGH:` (badge-blocking) or `GROUNDING-LOW:` (advisory). **This overlay only runs on `/v1/stream`. `/v1/query` skips it.**

6. **Schema awareness (W35 in flight).** `schema` is a first-class parameter throughout the loader, indexer, store, agents, and streaming layer. Redis keys are namespaced (`graph:OFSMDM:*`, `graph:OFSERM:*`). The frontend schema dropdown picks `ALL` / `OFSMDM` / `OFSERM`, threaded through as `schema_scope`. Phases 0-4 of the refactor have landed; Phases 5-8 (business-identifier indexing + routing) remain. See `docs/w35_architecture.md` and `docs/w35_phaseN_summary.md` before touching parsing, store, agents, or `main.py`.

For deeper architecture, query routing details, and the streaming SSE payload shape, the prior README content is preserved in git history (`git log -- README.md`) and the diagrams in `docs/ARCHITECTURE_OVERVIEW.md` are still current.

---

## Trust contract — what badges mean

Every response carries a `badge` field in the `event: done` SSE payload. Three values:

**VERIFIED.** The response is grounded in the cited source. Citations resolve to lines in retrieved functions, the chain of cited functions is coherent with `functions_analyzed`, no template-phrase fabrications, no caveat triggers ("possible reasons", "might be because"). Trust the answer.

**UNVERIFIED.** The validator detected something off — a function name cited but not in retrieved sources, a line range not present in the cited function, an unsupported paraphrase template, a December-only execution claim that doesn't match the source, or a caveat trigger in the rendered text. The body of the response may still be largely correct, but the warnings array names what failed. Read the cited source before trusting.

**DECLINED.** RTIE refused to answer. Reasons include: function name not found in the graph (`function_not_found`), ungrounded identifier in an otherwise unanswerable query (W45), classifier routed to `UNSUPPORTED` (forecasting, cross-table reconciliation, unknown tables), or LLM error surfaced sanitized. The body explains why and (where useful) suggests rephrasing.

**Warning categories** (any one of these in `done.warnings` is meaningful):

| Prefix / tag | Severity | Meaning |
|---|---|---|
| `GROUNDING-HIGH:` | Blocks the badge → forces UNVERIFIED | W57 content-trust failure: fabricated function name, unsupported template phrase, December-claim mismatch, etc. |
| `GROUNDING-LOW:` | Advisory; badge stays VERIFIED | W57 citation hygiene: repeated citations, excessive line citations, padding patterns. |
| `UNGROUNDED_IDENTIFIERS` | Blocks the badge | User named an identifier (column / CAP-code / function) that doesn't appear in any indexed source. |
| `NAMED_FUNCTION_NOT_RETRIEVED` | Blocks the badge | User named a function whose source wasn't retrieved for this query. |
| `PARTIAL_SOURCE` | Blocks the badge | Function metadata is indexed but the source body isn't loaded (W49 path). |
| `CONTRADICTION` | Blocks the badge | Generated content contradicts a known fact (e.g. claimed function purpose vs. graph evidence). |

---

## API endpoints — `/v1/stream` vs `/v1/query`

**`/v1/stream` is the canonical endpoint.** It returns Server-Sent Events with `event: stage`, `event: meta`, `event: token`, and `event: done`. The `done` payload carries `badge`, `validated`, `warnings`, `explanation`, `meta`, `functions_analyzed`, `source_citations`, and (post-W84) a `diagnostic` block with W81/W70/W76 anchor state. This is what the frontend reads. This is what canary harnesses and benchmark drivers must read.

**`/v1/query` returns raw LangGraph state.** It produces `final_state["output"]` directly and **skips the W57 grounding overlay entirely** — no `badge`, no `warnings`, no validator output. It exists for debugging only. Do not probe `/v1/query` for trust signals; the body prose can look like a logic explanation even when the route is correct. Verify route from the `meta` / `done` fields on `/v1/stream` instead.

If you're writing a script that asserts on a response, use `/v1/stream` and parse the `done` SSE event.

---

## Schema scope (W79)

The frontend schema dropdown sends `schema_scope` to the backend with three values:

- **`ALL`** (default) — semantic search fans out across both OFSMDM and OFSERM; the result with the highest relevance wins. Use this when you don't know which schema a function lives in.
- **`OFSMDM`** — retrieval is constrained to `graph:OFSMDM:*`. Use this when you specifically want the staging/MDM layer.
- **`OFSERM`** — retrieval is constrained to `graph:OFSERM:*`. Use this for the regulatory/risk computation layer (CAP codes, capital structure functions).

Schema-aware behavior also threads through `DATA_QUERY` (table-name-to-schema pivot) and the column index (multi-schema column ownership). Single-owner columns pivot; multi-schema columns keep the orchestrator's classification.

---

## Common operations

```bash
# Backend
python run.py                                  # start (Windows-safe event loop)
netstat -ano | findstr :8000                   # find PID for restart (PowerShell)
taskkill /PID <pid> /F                         # kill only that PID — NEVER use /IM python.exe

# Indexing
python cli.py index --force                    # re-index everything after adding modules
python cli.py status                           # show indexed function counts per schema
python cli.py ask "How is N_EOP_BAL calculated?"

# Redis hygiene
docker exec -it rtie-redis redis-cli FLUSHDB   # wipe all keys (force full re-index after)
docker exec -it rtie-redis redis-cli DBSIZE    # current key count

# Tests
python -m pytest tests/unit/ -v                                # full unit suite
python -m pytest tests/unit/parsing/test_loader.py -v          # one file
python tests/canary/run_canaries.py --tier 1                   # Tier 1 canaries (backend must be up)
make canary-tier1                                              # Makefile wrapper

# Frontend
cd frontend && npm run dev                     # http://localhost:5173
cd frontend && npm run build
cd frontend && npm run lint
```

---

## Adding a new module

1. Drop `.sql` files under `db/modules/<NEW_MODULE>/functions/`. One function or procedure per file. Filename should match the function name (case-insensitive).
2. Re-index: `python cli.py index --force`.
3. Restart the backend: graph pipeline re-parses everything, origins catalog auto-rebuilds at startup, new `V_DATA_ORIGIN` literals and GL block-list codes get picked up.
4. Verify with `python cli.py status` — function count should reflect the new additions.

No code changes required to add a new module. The catalog system is hardened against partial initialization — if a rebuild fails, the previous catalog stays in memory; on first-time failure, requests get a clean `RuntimeError` rather than half-formed answers.

---

## Where to go next

- [CLAUDE.md](../CLAUDE.md) (one level above `RTIE/`) — agent guidance, project conventions, things-not-to-do, where-to-look-first map for common bug surfaces.
- [docs/](docs/) — W-ticket history. W35 is the active schema-aware refactor; W57 family is the trust contract; W76/W70/W78/W78a/W81/W83a are anchor/grounding work; W84 added diagnostic exposure in `/v1/stream`.
- [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md) — clean-machine Windows walkthrough (Git, WSL 2, Docker Desktop, all download links).
- [docs/ARCHITECTURE_OVERVIEW.md](docs/ARCHITECTURE_OVERVIEW.md) — the deeper diagrams and pipeline shapes.
- [docs/RTIE_Weakness_Log.md](docs/RTIE_Weakness_Log.md) — known regressions, brittle paths, fix-vs-paper trade-offs.
- [tests/canary/canaries.yaml](tests/canary/canaries.yaml) — the full 18-query canary set with assertions and tier annotations.
- `scratch/` — captured benchmark runs (`v2_benchmark_run*.md`), one-off canary drivers (`w70_canary_*.py`), W-ticket experiments. Useful for "what did W57 actually change?" archaeology.

---

## Troubleshooting

**Backend fails to start with `RuntimeError: Cannot run the event loop ...`** — you ran `uvicorn src.main:app` directly. Always use `python run.py` on Windows. `run.py` sets the Selector event loop policy that psycopg requires.

**`.env.dev` missing.** It's gitignored. Copy from `.env.example` and fill in real values. Per the W35 worktree gotchas in `CLAUDE.md`, if you're using a parallel git worktree (`git worktree add ../RTIE-<branch>`), the `.env.dev` does NOT come with it — copy it across explicitly before `python run.py`.

**Oracle connection refused (`oracledb.exceptions.DatabaseError: DPY-6005`).** Check `ORACLE_HOST` / `ORACLE_PORT` / `ORACLE_SID` in `.env.dev`. On Windows test the network path with `Test-NetConnection -ComputerName <ORACLE_HOST> -Port 1521`. If `TcpTestSucceeded: False`, the issue is VPN or firewall, not RTIE.

**`ORA-00942: table or view does not exist` on canary queries.** The current `schema_scope` doesn't have access to the table. Either switch the dropdown to the right schema or set it to `ALL`. If the table genuinely doesn't exist in the deployment, the canary expectation needs adjusting; see `tests/canary/canaries.yaml` `needs_local_data` notes.

**Canary results don't match expected outcomes.** Most often this is a stale Redis index after a parser change or a partial re-index. Wipe and rebuild:

```bash
docker exec -it rtie-redis redis-cli FLUSHDB
python cli.py index --force
python run.py
```

**Redis container not running (`redis.exceptions.ConnectionError`).** Run `docker ps` — if `rtie-redis` isn't listed, `docker compose up -d` to bring it back. The Redis Stack image (not vanilla Redis) is required because RediSearch powers the vector index.

**Parallel-worktree friction.** Two specific gotchas, per `CLAUDE.md`: (1) `.env.dev` is gitignored, so a new worktree starts with no env file — copy it explicitly. (2) `sys.path` may pin to the original checkout's `src/`. In PowerShell, set `$env:PYTHONPATH = (Get-Location).Path + '\src'` before starting the worktree's backend. Verify the worktree's code is actually running by hitting a query and grepping the *worktree's* `logs/app.log` (not the original's) for a known signature line.

**`event: done` payload has no `badge` field.** You're reading `/v1/query`, not `/v1/stream`. `/v1/query` returns raw LangGraph state and skips the W57 overlay. Switch to `/v1/stream`.

---

## Environment variables

The loader is `load_dotenv(f".env.{ENVIRONMENT}")` in [src/main.py](src/main.py); `ENVIRONMENT=dev` (default) reads `.env.dev`. `cli.py` hardcodes `.env.dev`. Production deployments would set `ENVIRONMENT=prod` and provide `.env.prod`.

| Variable | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI auth | (required) |
| `OPENAI_MODEL` | Default OpenAI model | `gpt-4o-mini` |
| `ANTHROPIC_API_KEY` | Anthropic auth | (optional) |
| `ANTHROPIC_MODEL` | Default Claude model | `claude-sonnet-4-20250514` |
| `DEFAULT_LLM_PROVIDER` | `openai` or `anthropic` | `openai` |
| `EMBEDDING_MODEL` | OpenAI embedding model | `text-embedding-3-small` |
| `ORACLE_HOST` / `ORACLE_PORT` / `ORACLE_SID` / `ORACLE_USER` / `ORACLE_PASSWORD` | Oracle connection (read-only) | — |
| `REDIS_HOST` / `REDIS_PORT` | Redis Stack | `localhost` / `6379` |
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | LangGraph checkpointer | `localhost` / `5432` / `rtie` / `postgres` / (required) |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` / `LANGSMITH_ENDPOINT` | Optional tracing | tracing off if `LANGSMITH_TRACING != true` |
| `ENVIRONMENT` | Selects `.env.{ENVIRONMENT}` | `dev` |

The `docker-compose.yml` hardcodes `POSTGRES_PASSWORD=postgres123`; either match that in `.env.dev` or edit the compose file.

# RTIE — Regulatory Trace & Intelligence Engine

RTIE is a read-only multi-agent AI system built on Oracle OFSAA FSAPPS that explains the complete logic behind regulatory capital computations — tracing PL/SQL functions, T2T transformations, and RRF rules to give engineers instant, fully cited answers without touching the underlying system.

**This branch (`main`)** explains individual PL/SQL functions by name.

---

## Prerequisites

- **Python 3.11+** — required runtime
- **Docker & Docker Compose** — for Redis and PostgreSQL
- **Oracle Database** — access to an OFSAA FSAPPS instance (read-only credentials)
- **OpenAI API Key** — for query classification and logic explanation
- **LangSmith Account** — for observability and tracing (optional)

---

## Quick Start

### 1. Clone the repository
```bash
git clone <repository-url>
cd RTIE
```

### 2. Start infrastructure
```bash
docker-compose up -d
```
This starts Redis (port 6379) and PostgreSQL (port 5432) with memory tables auto-initialized.

### 3. Configure environment
Edit `.env.dev` with your credentials:
```
OPENAI_API_KEY=your_openai_key
ORACLE_HOST=localhost
ORACLE_PORT=1521
ORACLE_SID=XE
ORACLE_USER=OFSMDM
ORACLE_PASSWORD=your_password
```

### 4. Install Python dependencies
```bash
pip install poetry
poetry install
```

### 5. Start the backend
```bash
python run.py
```
The server starts at http://localhost:8000

### 6. Start the frontend
```bash
cd frontend
npm install
npm run dev
```
Open http://localhost:5173

### 7. Verify health
```bash
curl http://localhost:8000/health
```
Should return: `{"oracle":"ok","redis":"ok","postgres":"ok","status":"healthy"}`

### 8. Ask a question
In the web UI or via curl:
```bash
curl -X POST http://localhost:8000/v1/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Explain the logic of FN_LOAD_OPS_RISK_DATA",
    "session_id": "test-001",
    "engineer_id": "engineer@company.com"
  }'
```

---

## How to Add a New Module

1. Create a directory under `db/modules/`:
   ```
   db/modules/YOUR_MODULE_NAME/
   └── functions/
       ├── FN_YOUR_FUNCTION.sql
       ├── SP_YOUR_PROCEDURE.sql
       └── ...
   ```

2. Place `.sql` files with PL/SQL source code in the `functions/` subdirectory.

3. RTIE reads from Oracle `ALL_SOURCE` first, then falls back to these disk files.

---

## Slash Commands

| Command | Args | Description |
|---------|------|-------------|
| `/refresh-cache <name>` | Object name | Refresh one object's source cache |
| `/refresh-cache-all` | None | Re-sync all functions for the schema |
| `/cache-status <name>` | Object name | Show cache timestamps and version hash |
| `/cache-list` | None | List all cached keys |
| `/cache-clear <name>` | Object name | Delete one cache entry |
| `/refresh-schema` | None | Detect Oracle DDL changes and sync |

---

## Architecture

### Pipeline (9 nodes)

```
parse_query → resolve_object → fetch_logic → cache_validator
→ fetch_dependencies → explain_logic → query_relevance_validator
→ output_validator → render_response → END
```

1. **Orchestrator** — Classifies query via OpenAI, extracts the PL/SQL function name
2. **Metadata Interpreter** — Resolves object in Oracle or disk files, fetches source, builds dependency tree
3. **Logic Explainer** — Sends source + call tree to OpenAI, gets structured explanation with line citations
4. **Validator** — Checks cache freshness, query relevance, and output accuracy
5. **Cache Manager** — Handles slash commands for cache operations
6. **Renderer** — Assembles final response with confidence badge and citations

Slash commands bypass the graph and route directly to the Cache Manager.

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key |
| `OPENAI_MODEL` | Model name (default: gpt-4o) |
| `ANTHROPIC_API_KEY` | Anthropic API key (optional) |
| `ANTHROPIC_MODEL` | Claude model name (optional) |
| `ORACLE_HOST` | Oracle database hostname |
| `ORACLE_PORT` | Oracle listener port (default: 1521) |
| `ORACLE_SID` | Oracle System Identifier |
| `ORACLE_USER` | Oracle username (read-only) |
| `ORACLE_PASSWORD` | Oracle password |
| `REDIS_HOST` | Redis server hostname |
| `REDIS_PORT` | Redis server port (default: 6379) |
| `POSTGRES_HOST` | PostgreSQL hostname |
| `POSTGRES_PORT` | PostgreSQL port (default: 5432) |
| `POSTGRES_DB` | PostgreSQL database name |
| `POSTGRES_USER` | PostgreSQL username |
| `POSTGRES_PASSWORD` | PostgreSQL password |
| `ENVIRONMENT` | Runtime environment (dev/staging/prod) |

---

## Branches

| Branch | Description |
|--------|-------------|
| `main` | Explains individual PL/SQL functions by name (stable) |
| `feature/semantic-search` | Semantic vector search — ask any question about columns, tables, or batch flows (testing) |

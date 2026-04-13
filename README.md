# RTIE — Regulatory Trace & Intelligence Engine

RTIE is a read-only multi-agent AI system built on Oracle OFSAA FSAPPS that explains the complete logic behind regulatory capital computations — tracing PL/SQL functions, T2T transformations, and RRF rules to give engineers instant, fully cited answers without touching the underlying system.

**This branch (`feature/semantic-search`)** adds semantic vector search — ask any question about columns, tables, batch flows, or individual functions.

---

## Prerequisites

- **Python 3.11+** — required runtime
- **Docker & Docker Compose** — for Redis Stack (with RediSearch) and PostgreSQL
- **Oracle Database** — access to an OFSAA FSAPPS instance (read-only credentials)
- **OpenAI API Key** — for query classification, embeddings, and indexing
- **Ollama** — local LLM for source code analysis (download from https://ollama.com)
- **LangSmith Account** — for observability and tracing (optional)

---

## Quick Start

### 1. Clone and switch to this branch
```bash
git clone <repository-url>
cd RTIE
git checkout feature/semantic-search
```

### 2. Install Ollama and pull a model
Download Ollama from https://ollama.com/download, install it, then:
```bash
ollama pull llama3.2:3b
```

### 3. Start infrastructure
```bash
docker-compose up -d
```
This starts **Redis Stack** (port 6379 — includes RediSearch for vector search) and **PostgreSQL** (port 5432).

### 4. Configure environment
Edit `.env.dev` with your credentials:
```
OPENAI_API_KEY=your_openai_key
ORACLE_HOST=localhost
ORACLE_PORT=1521
ORACLE_SID=XE
ORACLE_USER=OFSMDM
ORACLE_PASSWORD=your_password
OLLAMA_MODEL=llama3.2:3b
```

### 5. Install Python dependencies
```bash
pip install poetry
poetry install
```

### 6. Index your PL/SQL functions (one-time)
Place your `.sql` files in `db/modules/<MODULE_NAME>/functions/` then:
```bash
python cli.py index --force
```
This generates descriptions (via OpenAI) and embeddings for each function, stored in Redis.

### 7. Ask questions via CLI
```bash
python cli.py ask "How is N_ANNUAL_GROSS_INCOME calculated?"
python cli.py ask "What updates STG_PRODUCT_PROCESSOR?"
python cli.py ask "Explain the entire batch flow"
python cli.py ask "Explain FN_LOAD_OPS_RISK_DATA"
```

### 8. Run the web app (backend + frontend)
```bash
# Terminal 1: Backend
python run.py

# Terminal 2: Frontend
cd frontend
npm install
npm run dev
```
Open http://localhost:5173

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `python cli.py index` | Index all modules (skips unchanged functions) |
| `python cli.py index --force` | Re-index all functions |
| `python cli.py status` | Show index stats and indexed function names |
| `python cli.py ask "question"` | Ask any question about the PL/SQL codebase |

---

## Slash Commands (Web UI)

| Command | Args | Description |
|---------|------|-------------|
| `/refresh-cache <name>` | Object name | Refresh one object's source cache |
| `/refresh-cache-all` | None | Re-sync all functions for the schema |
| `/cache-status <name>` | Object name | Show cache timestamps and version hash |
| `/cache-list` | None | List all cached keys |
| `/cache-clear <name>` | Object name | Delete one cache entry |
| `/refresh-schema` | None | Detect Oracle DDL changes and sync |
| `/index-module <name> [--force]` | Module name | Index one module's functions |
| `/index-all [--force]` | None | Index all modules |
| `/index-status` | None | Show vector index statistics |

---

## Architecture

### Hybrid LLM Strategy

```
OpenAI (remote, small payloads):         Ollama (local, unlimited):
  - Query classification                   - Source code explanation
  - Embeddings (text-embedding-3-small)    - Cross-function analysis
  - Indexing descriptions                  - Batch flow explanation
```

### Query Pipeline (6 nodes)

```
parse_query → semantic_search → fetch_multi_logic → explain_semantic → output_validator → render_response
```

1. **Orchestrator** — Classifies query via OpenAI, extracts search terms
2. **Semantic Search** — Embeds query, searches Redis vector index (KNN), keyword boost re-ranking
3. **Metadata Interpreter** — Fetches source code for top-K functions (Redis cache → Oracle → disk)
4. **Logic Explainer** — Sends all relevant source to Ollama (local LLM) for cross-function explanation
5. **Validator** — Verifies referenced functions exist, computes confidence score
6. **Renderer** — Assembles final response with citations, confidence badge, warnings

### Indexing Pipeline

```
scan db/modules/*.sql → truncate to 3KB → OpenAI generates description → OpenAI embeds → store in Redis
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

2. Index it:
   ```bash
   python cli.py index --force
   ```

3. Ask questions about it immediately.

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key (classification + embeddings + indexing) |
| `OPENAI_MODEL` | OpenAI model for classification (default: gpt-4o-mini) |
| `OLLAMA_BASE_URL` | Ollama server URL (default: http://localhost:11434) |
| `OLLAMA_MODEL` | Ollama model for source analysis (default: llama3.2:3b) |
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
| `EMBEDDING_MODEL` | OpenAI embedding model (default: text-embedding-3-small) |
| `ENVIRONMENT` | Runtime environment (dev/staging/prod) |

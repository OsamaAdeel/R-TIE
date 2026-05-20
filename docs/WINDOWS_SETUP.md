# Setting Up RTIE on Windows 11 — A Complete Walkthrough

This guide will take you from a fresh Windows 11 PC to a fully running RTIE installation. **No prior experience is assumed.** Every command is shown in full, every tool you need has a download link, and every choice along the way is explained.

If you get stuck on any step, jump to the [Troubleshooting](#troubleshooting) section at the bottom.

---

## What you are about to install

RTIE is a Python web application that answers questions about Oracle banking code. Out of the box, it talks to four things:

| Piece | What it is | How we'll run it |
|---|---|---|
| **Python backend** | The brain of RTIE (a FastAPI server) | Runs locally with `python run.py` |
| **React frontend** | The web page you'll type questions into | Runs locally with `npm run dev` |
| **Redis Stack** | A fast in-memory database for caching | Runs inside Docker |
| **PostgreSQL** | A normal database for conversation history | Runs inside Docker |
| **Oracle Database** | The bank's actual data — owned by your team | You connect to a remote one |
| **OpenAI** | The AI model that writes the answers | Cloud service (paid) |

You will install Python, Node.js, Docker Desktop, and Git on your PC. Redis and PostgreSQL run inside Docker so you don't have to install them by hand. Oracle stays where it already is — you only need its address and a username/password.

Expect the full setup to take **60–90 minutes** the first time.

---

## Step 1 — Check your Windows version

RTIE has been built and tested on Windows 11. To confirm what you're on:

1. Press the **Windows key** on your keyboard.
2. Type `winver` and press **Enter**.
3. A small window will pop up. The line that says **Version** should read `21H2`, `22H2`, `23H2`, or `24H2`.

If you're on Windows 10, most steps still work, but Docker Desktop's WSL 2 backend setup may require an extra reboot. The official Microsoft docs for that case live at [https://learn.microsoft.com/en-us/windows/wsl/install](https://learn.microsoft.com/en-us/windows/wsl/install).

---

## Step 2 — Install Git

Git is the tool that downloads the RTIE source code from your team's repository.

1. Open this download page in any browser: [https://git-scm.com/download/win](https://git-scm.com/download/win)
2. The page will start downloading **Git for Windows** automatically. If it doesn't, click the **"Click here to download"** link near the top.
3. Run the downloaded `.exe` file (it will be in your **Downloads** folder).
4. **Accept all the defaults** by clicking **Next** through every screen. The defaults are sensible for this project.
5. On the last screen, click **Install**, then **Finish**.

**To verify Git is installed:**

1. Press the **Windows key**, type `powershell`, and click **Windows PowerShell**.
2. Type the following and press **Enter**:
   ```powershell
   git --version
   ```
3. You should see something like `git version 2.45.0.windows.1`. The exact version doesn't matter as long as it prints.

If you get `'git' is not recognized`, close PowerShell, reopen it, and try again. Windows needs a fresh terminal to see the new tool.

**Reference:** [Official Git for Windows page](https://gitforwindows.org/)

---

## Step 3 — Install Python 3.11 or newer

RTIE needs Python 3.11 or higher. Python 3.12 is fine. Python 3.13 is fine. Python 3.10 or older is **not** supported.

1. Open the official Python download page: [https://www.python.org/downloads/windows/](https://www.python.org/downloads/windows/)
2. Click **Latest Python 3 Release - Python 3.x.y** (whatever the current version is).
3. Scroll to the bottom and download **Windows installer (64-bit)**.
4. Run the downloaded installer.
5. **VERY IMPORTANT** — On the first screen, **tick the box that says "Add python.exe to PATH"** at the bottom. If you skip this, no command line will be able to find Python and you'll have to reinstall.
6. Click **Install Now** and let it finish.
7. On the final screen, if you see **"Disable path length limit"**, click it. This avoids future headaches with long file paths.

**To verify Python is installed:**

Open a **new** PowerShell window (Windows key → `powershell` → Enter), then run:

```powershell
python --version
```

You should see `Python 3.11.x` or higher. If it prints `Python 2.7` or you get an error, see [Troubleshooting → Python not found](#python-not-found).

**Reference:** [Official Python downloads](https://www.python.org/downloads/)

---

## Step 4 — Install Node.js (LTS) for the frontend

The web page part of RTIE is built with React, which needs Node.js to run.

1. Open: [https://nodejs.org/en/download](https://nodejs.org/en/download)
2. Choose the **LTS** version (the one labelled "Recommended for Most Users"). At the time of writing, that's **Node.js 20.x** or **22.x** — either is fine.
3. Click **Windows Installer (.msi) 64-bit** to download.
4. Run the installer.
5. Click **Next** through every screen. Accept the license. When asked about "Tools for Native Modules," it's safe to **leave it unchecked** — we don't need them.
6. Click **Install** and let it finish.

**To verify Node.js is installed:**

Open a new PowerShell window and run:

```powershell
node --version
npm --version
```

You should see something like `v20.18.0` and `10.8.2`. The versions just need to be present.

**Reference:** [Official Node.js site](https://nodejs.org/)

---

## Step 5 — Install Docker Desktop

Docker is the tool that will run Redis and PostgreSQL for you in lightweight isolated containers, so you don't have to install those two databases by hand.

### 5a. Enable WSL 2

Docker Desktop on Windows 11 uses **WSL 2** (Windows Subsystem for Linux) under the hood. Windows 11 has this built in but it isn't always turned on.

1. Open PowerShell **as Administrator**: press the **Windows key**, type `powershell`, **right-click** "Windows PowerShell," and choose **"Run as administrator"**.
2. Run this single command:
   ```powershell
   wsl --install
   ```
3. When it finishes, **restart your computer**. (Yes, fully restart — not just sign out.)

If `wsl --install` says "WSL is already installed," that's also fine — skip the restart and move on.

### 5b. Install Docker Desktop

1. Open: [https://www.docker.com/products/docker-desktop/](https://www.docker.com/products/docker-desktop/)
2. Click the big **Download for Windows (AMD64)** button. (If your PC has an Arm processor, choose Arm instead — most PCs are AMD64.)
3. Run the downloaded `Docker Desktop Installer.exe`.
4. On the configuration screen, **make sure "Use WSL 2 instead of Hyper-V" is ticked** (it's the default).
5. Click **OK** and wait. The install takes about 5 minutes.
6. When it asks you to sign out, do so, then sign back in.
7. Launch **Docker Desktop** from the Start menu. The first time, it'll show a "Welcome" screen — you can skip the sign-in if you want (it's optional for personal use).

**To verify Docker is working:**

Open a new PowerShell window and run:

```powershell
docker --version
docker compose version
```

Both commands should print a version number. If either fails, Docker Desktop probably isn't running yet — look for the **whale icon** in your system tray (bottom-right corner near the clock) and wait until it stops animating.

**Reference:** [Docker Desktop docs for Windows](https://docs.docker.com/desktop/install/windows-install/)

---

## Step 6 — Get the RTIE source code

If you have a Git repository URL from your team, use it. If you already have the project on your machine, you can skip this step.

1. Open PowerShell (a normal one — administrator is not needed).
2. Pick a folder where you want the project to live. A good choice is your Documents folder:
   ```powershell
   cd $env:USERPROFILE\Documents
   ```
3. Clone the repository (replace `<repository-url>` with the actual URL — ask your team lead if you don't have it):
   ```powershell
   git clone <repository-url>
   ```
   This creates a folder called `RTIE` (the repo's root) in your current directory.
4. Move into the project directory:
   ```powershell
   cd RTIE
   ```

From this point on, **every command in this guide assumes you are inside the `RTIE` folder** — the one that contains `run.py`, `cli.py`, `pyproject.toml`, `docker-compose.yml`, and the `src/`, `db/`, and `frontend/` subfolders.

---

## Step 7 — Get an OpenAI API key

RTIE uses OpenAI's `gpt-4o-mini` model by default for classification, source description, and final answer writing. You need an account and an API key.

1. Sign up or sign in at: [https://platform.openai.com/signup](https://platform.openai.com/signup)
2. Once signed in, go to: [https://platform.openai.com/api-keys](https://platform.openai.com/api-keys)
3. Click **Create new secret key**.
4. Give it a name like `RTIE local dev` and click **Create**.
5. **Copy the key immediately** and paste it into a private note (you will only ever see it once — OpenAI doesn't show it again). The key starts with `sk-...`.
6. You also need to add a payment method at [https://platform.openai.com/account/billing](https://platform.openai.com/account/billing) — even $5 in credit is plenty for development.

**Cost note:** RTIE is designed to be cheap. Each question typically costs under $0.01. Indexing a few hundred PL/SQL files end-to-end costs a few dollars total.

**Reference:** [OpenAI API key safety best practices](https://help.openai.com/en/articles/5112595-best-practices-for-api-key-safety)

---

## Step 8 — Get your Oracle credentials

RTIE reads from an Oracle OFSAA FSAPPS database. You need **read-only** credentials from whoever owns that database (usually your team's DBA).

You need five pieces of information:

| Field | Example | What to ask for |
|---|---|---|
| Hostname | `oracle-prod-rdr.bank.local` | "The hostname or IP of the Oracle server I should connect to" |
| Port | `1521` | "The listener port" (almost always 1521) |
| SID or service name | `XE` or `orclpdb` | "The SID or PDB name" |
| Username | `OFSMDM` | "A read-only schema account I can use for RTIE" |
| Password | (a secret) | The password for that account |

**Important:** ask for a **SELECT-only** account. RTIE never writes to Oracle, but using a read-only account is an extra safety net.

If your team is still provisioning Oracle access, you can complete steps 9–12 first and come back to fill in the Oracle details later.

---

## Step 9 — Configure environment variables

RTIE reads its configuration from a file called `.env.dev` in the project root (the `RTIE/` folder). There is already a template called `.env.example` — you'll copy it and fill in real values.

1. In PowerShell, still inside the `RTIE` folder, run:
   ```powershell
   Copy-Item .env.example .env.dev
   ```
2. Open the new file in Notepad (or any editor):
   ```powershell
   notepad .env.dev
   ```
3. Fill in each value:

   ```
   # --- Oracle ---
   ORACLE_HOST=oracle-prod-rdr.bank.local
   ORACLE_PORT=1521
   ORACLE_SID=orclpdb
   ORACLE_USER=OFSMDM
   ORACLE_PASSWORD=the-password-your-DBA-gave-you

   # --- Redis (leave as-is — Docker will provide it) ---
   REDIS_HOST=localhost
   REDIS_PORT=6379

   # --- PostgreSQL (must match docker-compose.yml) ---
   POSTGRES_HOST=localhost
   POSTGRES_PORT=5432
   POSTGRES_DB=rtie
   POSTGRES_USER=postgres
   POSTGRES_PASSWORD=postgres123

   # --- OpenAI ---
   OPENAI_API_KEY=sk-paste-the-key-you-copied-in-step-7
   OPENAI_MODEL=gpt-4o-mini
   DEFAULT_LLM_PROVIDER=openai

   # --- Anthropic (optional — leave blank if you don't have a Claude key) ---
   ANTHROPIC_API_KEY=
   ANTHROPIC_MODEL=claude-sonnet-4-20250514

   # --- Embeddings ---
   EMBEDDING_MODEL=text-embedding-3-small

   # --- LangSmith tracing (optional — leave blank to disable) ---
   LANGSMITH_TRACING=false
   LANGSMITH_API_KEY=
   LANGSMITH_PROJECT=RTIE

   # --- Runtime ---
   ENVIRONMENT=dev
   ```

4. **Save and close** the file (Ctrl+S, then close Notepad).

**Important notes:**
- The PostgreSQL password (`postgres123`) is hard-coded in [docker-compose.yml:20](../docker-compose.yml#L20). Don't change it unless you also change that file.
- `.env.dev` contains secrets — **never** commit it to Git. The project's `.gitignore` already excludes it.

---

## Step 10 — Start Redis and PostgreSQL (via Docker)

With Docker Desktop running (look for the whale icon in your system tray), spin up the two databases.

1. In PowerShell, still inside `RTIE`, run:
   ```powershell
   docker compose up -d
   ```
2. The first time, Docker has to download the Redis Stack and PostgreSQL images (~500 MB total). Expect 2–5 minutes.
3. When it finishes, verify both containers are running:
   ```powershell
   docker ps
   ```
   You should see two rows: `rtie-redis` and `rtie-postgres`, both with status `Up`.

**What you just started:**
- **Redis Stack** on `localhost:6379` — includes RediSearch for vector search. There's also a web UI at [http://localhost:8001](http://localhost:8001) if you want to browse keys visually.
- **PostgreSQL 15** on `localhost:5432` — used by LangGraph to remember conversation state.

To **stop** these later (you usually don't need to — they're tiny when idle):
```powershell
docker compose down
```

To **see the logs** if something looks broken:
```powershell
docker compose logs --tail 50
```

---

## Step 11 — Install Python dependencies

The project uses **Poetry** to manage Python packages. Despite the README mentioning `requirements.txt`, there is no such file — Poetry is the only supported path.

### 11a. Install Poetry

1. In PowerShell, run:
   ```powershell
   pip install poetry
   ```
2. Verify it installed:
   ```powershell
   poetry --version
   ```
   You should see `Poetry (version 1.x.x)` or `Poetry (version 2.x.x)`.

### 11b. Install RTIE's dependencies

1. Still in `RTIE`, run:
   ```powershell
   poetry install
   ```
2. Poetry reads [pyproject.toml](../pyproject.toml), downloads every package the project needs (FastAPI, LangGraph, oracledb, redis-py, etc.), and creates an isolated virtual environment. Expect 3–7 minutes the first time.
3. When it's done, **activate the virtual environment** so `python` and `pytest` run against the right packages:
   ```powershell
   poetry shell
   ```

   If `poetry shell` says it isn't a command (Poetry 2.x removed it from core), install the shell plugin once:
   ```powershell
   poetry self add poetry-plugin-shell
   poetry shell
   ```

   Alternatively, prefix every command with `poetry run`, like `poetry run python run.py`.

**Reference:** [Poetry installation guide](https://python-poetry.org/docs/#installation)

---

## Step 12 — Place your PL/SQL source files

RTIE works by parsing actual PL/SQL function files into a queryable graph. Without source files, there's nothing to ask questions about.

The folder layout is:

```
RTIE/
  db/
    modules/
      OFSDMINFO_ABL_DATA_PREPARATION/
        functions/
          FN_LOAD_OPS_RISK_DATA.sql
          POPULATE_PP_FROMGL.sql
          ...
      OFSERMINFO_RISK_COMPUTATION/
        functions/
          ...
```

Each `.sql` file should contain **one** function or procedure body. Filenames must match the function name (case-insensitive) and end in `.sql`.

If you don't have the production PL/SQL exports yet, ask your team for a zip of the `db/modules/` folder. Drop it in and unzip it so the structure above is preserved.

---

## Step 13 — Build the index (one-time)

Now that Redis is running and the source files are in place, RTIE needs to parse every function, generate a short natural-language description for each (using OpenAI), and build a vector index for semantic search.

1. Make sure your virtual environment is active (the prompt should show `(rtie-py3.11)` or similar). If not, run `poetry shell` first.
2. Run:
   ```powershell
   python cli.py index --force
   ```
3. The first run takes 5–15 minutes depending on how many functions you have. You'll see one line per function being indexed.
4. When it finishes, run:
   ```powershell
   python cli.py status
   ```
   You should see the number of indexed functions per schema.

**The `--force` flag** re-indexes everything from scratch. After the first run, you can skip it (`python cli.py index` will only process changed files).

---

## Step 14 — Start the backend

1. Make sure Docker Desktop is running, Redis and PostgreSQL containers are up, and your virtual environment is activated.
2. From the `RTIE` folder, run:
   ```powershell
   python run.py
   ```
3. **Do not run `uvicorn src.main:app` directly.** [run.py:13-14](../run.py#L13-L14) sets a Windows-specific event loop policy that the PostgreSQL driver requires. Skipping `run.py` will crash on the first database call.
4. When you see lines like:
   ```
   INFO:     Started server process [...]
   INFO:     Application startup complete.
   INFO:     Uvicorn running on http://0.0.0.0:8000
   ```
   the backend is live at [http://localhost:8000](http://localhost:8000).

5. **Quick sanity check:** open a second PowerShell window and run:
   ```powershell
   curl http://localhost:8000/health
   ```
   You should get a JSON response. If not, see [Troubleshooting](#troubleshooting).

Leave this PowerShell window running. Closing it stops the backend.

---

## Step 15 — Start the frontend

1. Open a **new** PowerShell window (don't close the backend's window).
2. Navigate to the frontend folder:
   ```powershell
   cd $env:USERPROFILE\Documents\RTIE\frontend
   ```
3. Install the JavaScript dependencies (only needed the first time):
   ```powershell
   npm install
   ```
   This takes 2–5 minutes the first time.
4. Start the dev server:
   ```powershell
   npm run dev
   ```
5. You should see:
   ```
   VITE v8.x.x  ready in 800 ms
   ➜  Local:   http://localhost:5173/
   ```
6. Open [http://localhost:5173](http://localhost:5173) in your browser.

You should see the RTIE chat interface. Type a question like:

> How is N_ANNUAL_GROSS_INCOME calculated?

You should see a streaming markdown answer with citations. Congratulations — RTIE is running.

---

## Daily startup (after the first install)

Once the one-time setup above is done, your daily startup is just:

1. Start Docker Desktop (if it's not already running on login).
2. In PowerShell, terminal 1:
   ```powershell
   cd $env:USERPROFILE\Documents\RTIE
   docker compose up -d
   poetry shell
   python run.py
   ```
3. In PowerShell, terminal 2:
   ```powershell
   cd $env:USERPROFILE\Documents\RTIE\frontend
   npm run dev
   ```
4. Browser → [http://localhost:5173](http://localhost:5173).

---

## Troubleshooting

### Python not found

After installing Python, PowerShell says `'python' is not recognized`.

- **Fix:** Close every PowerShell window and open a new one. Windows only refreshes the PATH when a new shell starts.
- If that doesn't help, your installer didn't tick "Add python.exe to PATH." Reinstall from [python.org](https://www.python.org/downloads/) and make sure the box is ticked on the first installer screen.

### `docker compose up` says "Cannot connect to the Docker daemon"

- **Fix:** Docker Desktop isn't running. Open it from the Start menu and wait until the whale icon in the system tray stops animating, then try again.

### Backend crashes with `RuntimeError: Cannot run the event loop ...`

- **Fix:** You ran `uvicorn src.main:app` directly instead of `python run.py`. Always use `python run.py` on Windows.

### Backend prints `oracledb.exceptions.DatabaseError: DPY-6005: cannot connect to database`

The Oracle host/port/SID/credentials in `.env.dev` are wrong, or your machine can't reach the Oracle host (VPN, firewall, etc.).

- **Fix:** Double-check the values with your DBA. Try a basic network test:
  ```powershell
  Test-NetConnection -ComputerName <ORACLE_HOST> -Port 1521
  ```
  If `TcpTestSucceeded` is `False`, the issue is network-level (VPN, firewall). Connect to your corporate VPN and try again.

### Backend prints `redis.exceptions.ConnectionError`

- **Fix:** The Redis container isn't running. Run `docker ps` — if you don't see `rtie-redis`, run `docker compose up -d` again.

### Frontend shows "Failed to fetch" when sending a question

- **Fix:** The backend isn't reachable on port 8000. Check the backend's PowerShell window for errors. Restart it with `python run.py`.

### `npm install` fails with `EPERM` or permission errors

- **Fix:** Antivirus or OneDrive sync may be locking files in `node_modules`. Pause OneDrive temporarily and try again, or move the project out of OneDrive entirely (e.g. to `C:\dev\RTIE`).

### `poetry install` is extremely slow or times out

- **Fix:** This usually means a corporate proxy is interfering. Set the proxy explicitly:
  ```powershell
  $env:HTTP_PROXY = "http://your-proxy:port"
  $env:HTTPS_PROXY = "http://your-proxy:port"
  poetry install
  ```

### `OPENAI_API_KEY` is set but I get `Incorrect API key provided`

- **Fix:** Either the key was truncated when you pasted it, or your OpenAI account has no payment method. Re-generate a key at [https://platform.openai.com/api-keys](https://platform.openai.com/api-keys) and add billing at [https://platform.openai.com/account/billing](https://platform.openai.com/account/billing).

### The browser at `localhost:5173` shows a blank page

- **Fix:** Open the browser's developer tools (F12) → Console tab. If you see CORS errors, the backend isn't running. If you see "Cannot find module," `npm install` was interrupted — delete `frontend\node_modules` and run `npm install` again.

---

## Useful links to bookmark

- [RTIE README](../README.md) — full project overview, architecture, query routing
- [Python downloads](https://www.python.org/downloads/)
- [Node.js downloads](https://nodejs.org/en/download)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [Git for Windows](https://git-scm.com/download/win)
- [Poetry docs](https://python-poetry.org/docs/)
- [OpenAI API console](https://platform.openai.com/)
- [LangSmith](https://smith.langchain.com/) (optional observability)
- [Redis Stack docs](https://redis.io/docs/about/about-stack/)
- [Oracle Python driver docs (`oracledb`)](https://python-oracledb.readthedocs.io/)
- [Microsoft WSL install guide](https://learn.microsoft.com/en-us/windows/wsl/install)

---

## What to do next

Once RTIE is up:

1. Read the [RTIE README](../README.md) sections **How It Works** and **Query Types and Routing** to understand what to ask.
2. Try the sample questions from the README's **Sample Outputs** section.
3. If you're contributing code, read [docs/w35_architecture.md](w35_architecture.md) before touching the parsing, loader, or agent layers.
4. To run the test suite:
   ```powershell
   python -m pytest tests/unit/ -v
   ```

Welcome to RTIE.

# Phase 1 and Phase 2: Personal Memory + Visual RAG Pipeline

This package builds on the project code you shared. Existing routes and the current Compose deployment are retained. The new database table is additive: `user_memories` stores one editable private memory note per user.

## What's added

- `GET /api/memory` and `PUT /api/memory`: authenticated, per-user persistent memory.
- Chat sends the saved memory to the LLM as a personalization hint, not as retrieved evidence.
- `GET /api/pipeline/metrics`: real counts from the logged-in user's document rows and recorded chunk counts.
- `POST /api/retrieval/debug`: user-scoped top-5 ChromaDB retrieval previews and similarity estimates.
- React screens: **Personal memory**, **RAG Pipeline**, pipeline stages, metric cards and retrieval debug results.
- No Kubernetes pods are created by these code changes.

## Step-by-step: run safely on Ubuntu

> Start with the current Docker Compose project. Do not delete containers or volumes. Back up PostgreSQL and ChromaDB before changing code. Do not copy secrets into source control.

### 1. Back up your current working directory

From the project root on your Ubuntu machine:

```bash
cd /home/sirag
cp -a Advanced_RAG_Agentic_AI "Advanced_RAG_Agentic_AI.backup.$(date +%Y%m%d-%H%M%S)"
```

### 2. Extract this ZIP into a separate test directory first

Copy the ZIP to Ubuntu, then:

```bash
mkdir -p ~/Advanced_RAG_Phase12_test
unzip Advanced_RAG_Agentic_AI_Phase12.zip -d ~/Advanced_RAG_Phase12_test
cd ~/Advanced_RAG_Phase12_test/Advanced_RAG_Agentic_AI
```

This keeps your existing live Compose project untouched while you inspect and test the code.

### 3. Review configuration

```bash
cat .env.example
```

Create a local `.env` only if required by your current Compose setup. Use your existing secrets locally; never paste them into chat or commit them. Confirm `DATABASE_URL`, `CHROMA_PATH`, `JWT_SECRET`, and `GROQ_API_KEY` match your environment.

### 4. Run backend tests in the project's existing Python environment/container

If Compose is already running, use its API container to test the current deployed code first. For this separate source checkout, install dependencies in a virtual environment only if your host has enough disk and memory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
PYTHONPATH=backend pytest -q
```

The embedding model may download on first run and can use substantial RAM/disk. If resources are low, skip local dependency installation and perform syntax checks instead:

```bash
python3 -m compileall -q backend
```

### 5. Build the frontend

```bash
cd frontend
npm install
npm run build
cd ..
```

A successful build creates `frontend/dist/`. If the host is resource-constrained, build on another development machine and copy the source/artifact after testing.

### 6. Database compatibility

The app uses `Base.metadata.create_all()` during startup, which adds the `user_memories` table if it does not exist. It does not drop existing tables. For production, replace this startup schema behavior with Alembic migrations before doing managed upgrades.

### 7. Test API behavior

After starting the API in the test environment, check:

```bash
curl -sS http://127.0.0.1:8000/health
```

Then sign in from the UI and open **Personal memory**. Save a short harmless preference, refresh the page, and confirm it persists. Log in as another user and confirm that user's memory is separate. Open **RAG Pipeline** and verify counts match that account's document list. Run the retrieval debugger with a query that should match one of your uploaded files.

### 8. Docker/Kubernetes later

Do not create pods until the host has adequate free RAM/disk and the existing Compose app is stable. Before a later deployment, check image tags, deployment manifests, secrets, PVC binding, probes and API health. Keep the existing `compose_postgres_data` and `compose_chroma_data` volumes intact.

## Important limitations / next hardening work

- Registration still does not send verification email; configure a real verification workflow before production.
- The saved memory is explicitly user-edited; automatic memory extraction/approval is not implemented here.
- Pipeline stages summarize the actual existing synchronous ingestion path and database records; they are not a distributed event stream.
- Similarity estimate is `1 - Chroma distance`, clamped to 0..1. It is a debugging indicator, not a calibrated probability.
- Hybrid search and reranking are not implemented by this patch; the retrieval debugger exposes the current vector retrieval behavior.
- Use Alembic migrations, rate limiting, CSRF/session review, secrets management, authorization tests and backups before production use.

## Readable assistant responses (Markdown rendering)

The chat UI renders assistant answers as safe Markdown using `react-markdown` and `remark-gfm`. Headings, paragraphs, numbered/bulleted lists, bold text, code blocks, blockquotes, links, and GitHub-style tables are rendered as formatted UI rather than raw Markdown syntax. Raw HTML is not enabled. User messages remain plain text.

After extracting the project, install the frontend dependencies and build it:

```bash
cd frontend
npm install
npm run build
```

If the build succeeds, restart/rebuild the frontend using the deployment method for this environment. With Docker Compose, from the project root:

```bash
docker compose -f deploy/compose/docker-compose.yml build frontend
docker compose -f deploy/compose/docker-compose.yml up -d frontend
```

The first build requires network access to download the new npm packages. Existing database volumes are not changed by these frontend commands.

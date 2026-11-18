# Advanced RAG Agentic AI

Starter implementation for evolving an existing Ubuntu-hosted FastAPI + ChromaDB RAG application into a multi-user platform.

## Included
- React/Vite dark chat UI with user/admin navigation
- FastAPI API, JWT login, registration, role checks
- PostgreSQL persistence for users, documents, conversations, messages, feedback
- ChromaDB vector retrieval and document chunking for TXT/PDF/DOCX
- Optional Groq-compatible chat completion API
- Docker Compose for local testing
- Kubernetes starter manifests
- GitHub Actions test workflow
- Admin metrics endpoint and feedback endpoint
- Separate Ubuntu/Kubernetes guide is supplied as a PDF

## Important
This is a starter scaffold, not a security-audited production service. Before internet exposure, configure HTTPS, email verification/password reset, rate limiting, secure secret storage, malware/file scanning, backup/restore tests, privacy/retention controls, and a proper migration workflow. The registration endpoint currently creates a user immediately; email verification is a Phase 2 hardening task.

## Quick start
1. Copy `.env.example` to `.env` and change `JWT_SECRET`.
2. Run `docker compose up --build`.
3. Open http://localhost:8080.
4. Register a user; the first registered user is not automatically an admin. Promote a trusted account manually using the admin SQL instructions in `docs/ADMIN_SETUP.md`.
5. API docs: http://localhost:8000/docs

## Folder map
- `backend/`: FastAPI, auth, database, retrieval, ingestion
- `frontend/`: React/Vite UI
- `deploy/compose/`: Docker Compose
- `deploy/k8s/`: Kubernetes starter YAML
- `tests/`: basic tests
- `docs/`: operational notes

## RAG design
Upload -> extract text -> chunk -> embed -> ChromaDB -> retrieve relevant chunks -> optional LLM answer.
This implementation does not train neural network weights when documents are uploaded. Feedback is stored for later evaluation. Fine-tuning should be a separate offline, reviewed workflow.

## Phase 1/2 additions: personal memory and visual RAG dashboard

See [`docs/PHASE_1_AND_2_IMPLEMENTATION.md`](docs/PHASE_1_AND_2_IMPLEMENTATION.md) for the safe, step-by-step Ubuntu workflow. This update adds per-user saved memory, pipeline metrics based on document records, a ChromaDB retrieval debugger, and React UI screens for those features. Test in a separate directory before replacing any live code; keep existing Compose volumes and secrets safe.

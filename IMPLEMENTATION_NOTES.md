# Advanced RAG Agentic AI — Admin/User Panel Update

## Included changes
- Preserved the existing chat, conversation history, citations, feedback, personal memory, ingestion and retrieval-debug code.
- Added an administrator workspace with Live View, Traces, Documents, Users, Analytics and Settings tabs.
- Added admin-only backend endpoints for user listing, role updates, application trace events, all-document ingestion metrics and a safe runtime settings summary.
- User role sees Chat and Chat History only in the navigation. Document listing, upload, memory, pipeline diagnostics, retrieval debugging and admin management endpoints are protected by `admin_user` on the backend.
- Admin document management supports upload and delete via existing protected endpoints.
- Role changes are audited; self-demotion and demoting the last administrator are blocked.
- Settings response intentionally excludes passwords, JWT secrets, database URLs and API keys.

## Important limitations
- The Traces screen currently reads `chat.completed` audit events. It does not claim to be connected to Langfuse and does not invent nested spans, token counts or costs. Real Langfuse integration requires instrumentation and configured server-side credentials.
- The Live View's ingestion stages are based on persisted document statuses/chunk counts. A genuinely per-request moving query pipeline requires emitting and streaming real execution events from the RAG/agent code.
- Analytics currently displays persisted database aggregates and latency values where recorded; no synthetic metrics are generated.
- Email verification/password reset and a complete settings editor are not implemented by this change; existing README warnings remain applicable.

## Validation performed
- Python syntax compilation passed for the modified FastAPI `main.py` and existing model/security modules.
- Frontend production build was not verified in this environment because dependency installation did not complete. Run `npm install` and `npm run build` in `frontend/` on a machine with npm registry access before deployment.

## Deployment safety
1. Back up the current project and database before deploying.
2. Keep the old server running until new-server verification is complete.
3. Never copy `.env` into a shared ZIP or commit it to source control. Use `.env.example` as a template and keep real secrets on the server.
4. Deploy through the existing Docker Compose process, then verify `/health`, login, user isolation, document ingestion/deletion, chat history and role permissions.

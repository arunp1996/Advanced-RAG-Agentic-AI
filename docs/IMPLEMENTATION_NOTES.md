# Implementation notes and known limitations

This is an executable starter foundation, not a fully production-ready ChatGPT clone.

- `Base.metadata.create_all()` is used for first boot only. Replace it with Alembic migrations before production.
- Email verification is not implemented; `is_verified` exists but is not yet enforced.
- Conversation history is persisted; personal memory summarization/retrieval is the next implementation phase.
- Hybrid BM25 search and reranking are planned extensions, not yet wired in.
- Web search and an agent planner are not enabled by default. Add explicit tool allowlists, timeouts, and human approval for consequential actions.
- API responses are not streamed yet; implement SSE or WebSockets in a later phase.
- Metrics are basic database counters. Add Prometheus instrumentation for latency histograms, errors, tokens and retrieval quality.
- Feedback is stored but does not train the model. Review and curate feedback into evaluation datasets before any fine-tuning.
- Chroma filters are applied by owner ID. Keep a strict rule that all private retrieval is user-scoped.
- The starter uses JWT in localStorage for simplicity. For production, prefer secure HttpOnly cookies or a carefully implemented short-lived access/refresh-token design.
- Fine-tuning must run offline in a separate worker/job with dataset approval, resource checks, evaluation and a manual promotion gate.

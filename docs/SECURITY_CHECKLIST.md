# Before production exposure

- Enable email verification and password reset.
- Store secrets in a secret manager; rotate any secrets that may have been exposed.
- Enforce HTTPS and secure cookies or short-lived access tokens with refresh-token rotation/revocation.
- Add rate limits to login, upload, chat and web-search endpoints.
- Add file size, MIME/magic-byte validation, malware scanning and safe parsing limits.
- Use Alembic migrations instead of `create_all`.
- Add ownership tests for every document, conversation, memory and feedback endpoint.
- Ensure vector retrieval always filters by owner/workspace and revalidates returned source permissions.
- Add CSRF protections if using cookies; configure strict CORS.
- Add backup, restore and disaster recovery tests.
- Redact credentials and private content from logs.
- Audit admin actions and restrict access to private conversations.
- Add prompt-injection defenses for web content and uploaded documents; never let retrieved content override system/tool policies.
- Scan container images and dependencies; run as non-root and set resource limits.
- Add TLS ingress, network policies, pod security controls and least-privilege service accounts.
- Run security and quality regression tests before releases.

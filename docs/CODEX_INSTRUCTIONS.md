# Permanent Codex workflow

- GitHub `main` is the source of truth; read `HANDOFF.md` and `SECURITY.md` first.
- Use one branch per task and never develop on `main`.
- Never merge your own PR. Verify local and remote tree SHAs match.
- Never expose secrets in source, APIs, logs, docs, or frontend storage.
- Never enable publishing without explicit authorization.
- Authentic Shopify product media remains authoritative.
- Human approval remains mandatory; no automatic approval or version selection.
- Update documentation on every task.
- Task #36 OAuth remains account-connection only; use Basic-auth token exchange, one-time hashed state, encrypted tokens, and mocked provider tests. Never add publishing scopes or provider writes.

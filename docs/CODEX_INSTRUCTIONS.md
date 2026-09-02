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
- Task #37 board sync is read-only: persist provider board/section snapshots only, require authentication, and never write Pinterest boards or Pins. Keep publishing, scheduling, and analytics disabled.
- Task #38 publication snapshots remain provider-independent audit records. Provider publishing requires both `PUBLISHING_ENABLED=true` and `pins:write`; never enable either in protected repository state, and never add write scopes to OAuth requests.
Task #38 allows human scheduling only; keep publishing false, OAuth write scopes forbidden, and unknown outcomes non-retryable.

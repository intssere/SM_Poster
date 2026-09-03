# Permanent Codex workflow

- GitHub `main` is the source of truth; read `docs/HANDOFF.md` and `docs/SECURITY.md` first.
- Use one branch per task and never develop on `main`.
- Verify local and remote commit/tree SHAs before starting, before pushing, and before reporting.
- Never amend, reset, rebase, squash, force-push, or rewrite history unless the user explicitly authorizes that exact operation.
- Never merge your own PR; merge requires separate explicit authorization.
- Never expose secrets in source, APIs, logs, docs, fixtures, telemetry, or frontend storage.
- Authentic Shopify product media remains authoritative. AI may not recreate branded bottles, packages, products, or logos.
- Human approval remains mandatory; no automatic approval, version selection, scheduling dispatch, or publication.
- Update all required documentation on every task when behavior or continuity changes.

## Task #38 protected state

- `PUBLISHING_ENABLED=false` remains the protected repository/default state.
- Live OAuth requested scopes stay exactly `user_accounts:read`, `boards:read`, and `pins:read`.
- Fixture-only `pins:write` is allowed in tests that prove future readiness boundaries.
- Never add `boards:write` in Task #38.
- Do not perform live Pinterest writes, OpenAI calls, browser automation, autonomous worker startup, or automatic `PUBLISH_UNKNOWN` retry.
- Do not add an enabled frontend Publish control.
- Task #38 PR creation must wait until the final release matrix and independent branch-vs-main audit are green and PR_READY is explicitly authorized.

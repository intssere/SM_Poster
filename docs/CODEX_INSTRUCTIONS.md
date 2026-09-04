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

- Task #38 is merged on `main`.
- The publisher foundation existing does not authorize publishing.
- `PUBLISHING_ENABLED=false` remains the protected repository/default state.
- Live OAuth remains read-only until a separately authorized task changes it; requested scopes stay exactly `user_accounts:read`, `boards:read`, and `pins:read`.
- Never add `pins:write` or `boards:write` casually.
- Any future write-scope request requires explicit authorization and review.
- Do not perform live Pinterest writes, OpenAI calls, browser automation, autonomous worker startup, or automatic `PUBLISH_UNKNOWN` retry.
- Do not add an enabled frontend Publish control without separate authorization.
- Future live-publishing work must pass the Pinterest SEO + Metadata + Creative Quality Release Gate.
- Authentic Shopify product media remains authoritative.
- No autonomous worker may be introduced unless a future task explicitly authorizes one.
- New work must start from current verified `main` and use a new branch.
- PR creation and merge still require independent review and explicit human authorization.
### Task #39 Phase 3B

Keep operator controls server-derived, preserve human approval, and never enable publishing or expose provider credentials in browser state.

Use the exact server confirmation text/version for authorization, require a human-readable revocation/reconciliation reason, and never expose generic retry/cancel controls for PUBLISH_UNKNOWN. Consult the operator runbook before incident actions.

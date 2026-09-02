# Project Handoff

## What this is

Diamond Shelf Social Studio (`intssere/SM_Poster`) is an internal system for turning trusted Shopify catalog facts into reviewable social creative while preserving authentic branded product imagery and human control.

## Current baseline

- Authoritative Task #38 main baseline is `4242b80e6626886b528641749beeb64cf7e4ea62` (tree `ac35cffe2f58c1d47deac578e43cabe12189e519`). Current Task #38 branch head is recorded separately in Git history.
- Alembic head: `0013`; migration 0013 adds nullable `PinterestConnection.boards_last_synced_at` for connection-level successful board-sync state.
- Backend: FastAPI, SQLAlchemy, Alembic, PostgreSQL-ready models.
- Frontend: React/Vite internal review dashboard.
- Publishing: disabled (`PUBLISHING_ENABLED=false`).
- AI/provider mode: disabled in protected state.

## Completed feature sequence

PRs #1–#8 established review-only AI foundations, production model configuration, fail-closed hosted errors, grounded fact safety, revision comparison previews, and persisted Creative Proof image serving. Task #34 adds exact approval identity, immutable publication snapshots, duplicate protection, and project documentation.

## Non-negotiable rules

- Use the authentic persisted Shopify product image for branded products.
- AI may generate approved supporting backgrounds, never branded products/packages/logos.
- Revisions are immutable; active-version selection is explicit.
- Human approval must bind the exact revision and creative.
- No auto-approval, auto-publishing, Pinterest API call, or scheduling worker.
- Hosted text uses Luna by default; Terra is explicit-only; hosted failures fail closed.
- Never place secrets in source, APIs, telemetry, or logs.

## Pinterest and analytics status

Task #36 account OAuth and Task #37 read-only board/section synchronization with local Board Manager selection are COMPLETE (PR #15 MERGED, Issue #14 CLOSED). Pinterest writes, scheduling, and analytics remain disabled. Publication Identity v2 remains audit-safe.

Pinterest connection refresh is provider-read-only: replacement values are
validated and encrypted before connection fields are changed. Failed refreshes
preserve encrypted credentials and all expiry/scope/timestamp metadata; only a
sanitized error code may be recorded. No background refresh job is enabled.

## Repository/test state versus runtime state

Task #34 verification uses isolated test databases. Those fixtures create and delete approvals and publication snapshots and are not evidence of live runtime counts. This repository session had no production database connection, so it did not re-query or mutate runtime business data.

The latest previously verified runtime snapshot, carried forward from controlled Adagio v3 verification, is 4 content revisions, 1 selection (Adagio v3), 5 telemetry records, 0 generated assets, 0 approvals, and 0 publications. Treat these values as a prior verified snapshot, not as counts measured by Task #34. Re-query the deployed PostgreSQL database before relying on them operationally.

PR #15 is MERGED and Issue #14 is CLOSED on `main` at `886ba9ec25e316f0d5b9a3a590ae8cbef103a059` (tree `f0b3ec2eaaa7f9ac050f43f3b0ad3bc015c409e0`). Task #35, Task #36, and Task #37 are COMPLETE; Alembic is `0013`. Pinterest board sync is read-only with connection-level successful-sync state, strict provider validation, five-minute refresh preflight, and local-only eligibility/routing; publishing, scheduling, and analytics remain disabled. The next stage is Publisher + Scheduler design.

## Known issues and next work

## Task #38 — Publisher + Scheduler Foundation v1

Task #38 is PRE-MERGE. Publication snapshots are provider-independent immutable audit records; provider execution remains separately gated by `PUBLISHING_ENABLED=true`, connected Pinterest `pins:write`, approved identities, eligible destination, and public HTTPS media. Scheduling, cancellation, bounded due discovery, transactional claims, and durable attempts are foundations only; no autonomous worker or automatic `PUBLISH_UNKNOWN` retry exists. Current OAuth scopes remain `user_accounts:read`, `boards:read`, and `pins:read`; publishing remains disabled.

Task #35, Task #36, and Task #37 are COMPLETE; PR #15 is MERGED and Issue #14 is CLOSED. Alembic is `0013`. Publishing remains disabled; runtime counts remain carried-forward historical values, not freshly queried. The next implementation stage is Publisher + Scheduler design, subject to explicit authorization.

## Verification commands

```bash
cd backend
pytest -q
alembic upgrade head

cd ../frontend
npm run build
```
# Task #37 refresh behavior

Task #38 is PRE-MERGE. Alembic is `0014`; publication snapshots are provider-independent and Pinterest dispatch has separate publishing/scope/media gates. Publishing remains disabled.

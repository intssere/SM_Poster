# Project Handoff

## What this is

Diamond Shelf Social Studio (`intssere/SM_Poster`) is an internal system for turning trusted Shopify catalog facts into reviewable social creative while preserving authentic branded product imagery and human control.

## Current baseline

- Task #37 started from authoritative GitHub `main` at `beca739cc7b0832f4c74898c928d2f028d453bc9` (tree `62bc9f03f939f322acad0cd81891cd92ac53fc03`).
- Alembic head: `0012` after read-only Pinterest board synchronization.
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

Task #36 implements account OAuth only. Task #37 adds read-only board/section synchronization and local Board Manager selection; Pinterest writes, scheduling, and analytics remain disabled. Publication Identity v2 remains audit-safe.

Pinterest connection refresh is provider-read-only: replacement values are
validated and encrypted before connection fields are changed. Failed refreshes
preserve encrypted credentials and all expiry/scope/timestamp metadata; only a
sanitized error code may be recorded. No background refresh job is enabled.

## Repository/test state versus runtime state

Task #34 verification uses isolated test databases. Those fixtures create and delete approvals and publication snapshots and are not evidence of live runtime counts. This repository session had no production database connection, so it did not re-query or mutate runtime business data.

The latest previously verified runtime snapshot, carried forward from controlled Adagio v3 verification, is 4 content revisions, 1 selection (Adagio v3), 5 telemetry records, 0 generated assets, 0 approvals, and 0 publications. Treat these values as a prior verified snapshot, not as counts measured by Task #34. Re-query the deployed PostgreSQL database before relying on them operationally.

PR #13 is merged into `main` at `beca739cc7b0832f4c74898c928d2f028d453bc9` (tree `62bc9f03f939f322acad0cd81891cd92ac53fc03`). Task #35 and Task #36 are COMPLETE. Task #37 is PR #15 — PRE-MERGE / under independent review on branch `task-37-pinterest-board-manager-v1`; Alembic is `0012`. Pinterest board sync is read-only; publishing, scheduling, and analytics remain disabled. The next stage after merge is Publisher + Scheduler design.

## Known issues and next work

Task #35 and Task #36 are COMPLETE. Task #37 is PR #15 — PRE-MERGE / under independent review; Alembic is `0012`. Publishing remains disabled; runtime counts remain carried-forward historical values, not freshly queried. The next implementation stage after merge is Publisher + Scheduler design, subject to explicit authorization.

## Verification commands

```bash
cd backend
pytest -q
alembic upgrade head

cd ../frontend
npm run build
```

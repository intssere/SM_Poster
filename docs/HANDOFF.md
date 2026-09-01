# Project Handoff

## What this is

Diamond Shelf Social Studio (`intssere/SM_Poster`) is an internal system for turning trusted Shopify catalog facts into reviewable social creative while preserving authentic branded product imagery and human control.

## Current baseline

- Task #34 started from GitHub `main` at PR #8 merge `d2674c98eec5da08f6e5c1843aa3526fce0f857c`.
- Alembic head: `0010` after Publication Identity v2.
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

Models and adapter boundaries exist, but OAuth, production Pin creation, board creation, scheduling, and analytics polling are not implemented. Publication Identity v2 prepares audit-safe snapshots only.

## Repository/test state versus runtime state

Task #34 verification uses isolated test databases. Those fixtures create and delete approvals and publication snapshots and are not evidence of live runtime counts. This repository session had no production database connection, so it did not re-query or mutate runtime business data.

The latest previously verified runtime snapshot, carried forward from controlled Adagio v3 verification, is 4 content revisions, 1 selection (Adagio v3), 5 telemetry records, 0 generated assets, 0 approvals, and 0 publications. Treat these values as a prior verified snapshot, not as counts measured by Task #34. Re-query the deployed PostgreSQL database before relying on them operationally.

PR #10 is merged into `main` at `6bff2e6cf36bbbac0c3f7831fe6680868d07a1be`, tree `22bae2de2346e408552eeeba57786959d38c2f2c`. Task #35 is the next branch and remains pre-merge. Authentication is the only new operational capability; Pinterest OAuth, publishing, scheduling, analytics, and provider calls remain out of scope.

## Known issues and next work

Internal proposal APIs still need a dedicated authentication/authorization hardening task. After that, design Pinterest OAuth and sandbox publishing as separate reviewed tasks. Do not enable publishing as part of either analysis or migration work.

## Verification commands

```bash
cd backend
pytest -q
alembic upgrade head

cd ../frontend
npm run build
```

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

## Protected state

Task #34 began with 2 content revisions, 0 selections, 5 telemetry records, 0 generated assets, 0 approvals, and 0 publications. Verification must leave these production counts unchanged; test fixtures use isolated databases.

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

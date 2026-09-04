# Project State

Diamond Shelf Social Studio is an internal Shopify-catalog-to-editorial-content review system. It preserves authentic branded product imagery, immutable review records, and explicit human control.

## Baselines

- Authoritative Task #38 `main` baseline: `4242b80e6626886b528641749beeb64cf7e4ea62`
- Baseline tree: `ac35cffe2f58c1d47deac578e43cabe12189e519`
- Authoritative main Alembic head: `0013`
- Task #38 PRE-MERGE branch: `task-38-publisher-scheduler-foundation-v1`
- Task #38 code/frontend checkpoint entering documentation closure: `8c14903deac4978e36d9a65d62122d84d374978e`
- Task #38 checkpoint tree: `8ca7b7aa438cd1c6fd038e734a4ad24cdd5e4d53`
- Task #38 PRE-MERGE branch Alembic head: `0014`

Migration `backend/alembic/versions/0014_publisher_scheduler_foundation.py` is additive and historical migrations `0001`-`0013` were not rewritten. Known limitation: historical migration `0002` contains PostgreSQL-specific syntax such as `'[]'::json`, so Task #38 requires narrow actual `0013 -> 0014` verification rather than claiming the complete fresh SQLite migration chain passes.

## Task status

- Task #34: COMPLETE / merged
- Task #35: COMPLETE / merged
- Task #36: COMPLETE / merged
- Task #37: COMPLETE / merged
- Task #38: PRE-MERGE; no PR has been claimed and release certification is not complete yet

## Current capabilities

Completed foundations include catalog normalization, controlled proposal generation, deterministic authentic-product creative rendering, immutable AI-assisted copy revisions, review-only generated backgrounds and video specifications, explicit version selection, approval auditing, read-only revision previews, single-admin authentication, read-only Pinterest OAuth, and read-only Pinterest board/section sync with local Board Manager configuration.

Task #38 adds provider-independent immutable publication snapshots, exact approved revision/original-content identity, exact approved creative identity, authentic source-image provenance, real PinterestConnection and PinterestBoard binding, immutable external board/title/description/alt/destination/UTM/media snapshots, duplicate fingerprint protection, explicit human schedule/reschedule/cancel, timezone-aware UTC scheduling, deterministic due discovery, bounded dispatcher batches of 25, transactional compare-and-set claims, durable STARTED PublicationAttempt rows, unique attempt numbering, mockable Pinterest gateway boundaries, conservative provider outcome classification, safe attempt metadata, authenticated publication APIs, server-derived readiness, and a hardened Publications frontend.

Explicit human scheduling exists. Autonomous/background scheduling does not.

## Protected release state

`PUBLISHING_ENABLED=false` remains authoritative. Live Pinterest OAuth requested scopes remain exactly:

- `user_accounts:read`
- `boards:read`
- `pins:read`

Task #38 does not request `pins:write` or `boards:write`. The publisher may inspect an already-granted `pins:write` in isolated tests or a future authorized environment, but live OAuth does not request it. Protected state therefore has two independent provider-write blockers: `PUBLISHING_ENABLED=false` and no live requested `pins:write`.

No real Pinterest Pin creation, OpenAI call, autonomous worker, browser automation, or automatic `PUBLISH_UNKNOWN` retry is part of Task #38.

## Repository and runtime state

Repository verification uses isolated test databases. Test-created approval/publication rows are ephemeral and do not represent deployed business data. No production database was connected or queried during this documentation pass.

Latest carried-forward runtime snapshot, historical and not freshly queried:

- Content revisions: 4
- Version selections: 1 (Adagio v3 selected)
- AI telemetry: 5
- Generated assets: 0
- Approvals: 0
- Publications: 0

Re-query the deployed PostgreSQL database before relying on runtime counts operationally.

## Latest focused verification history

- Publication API: 17 passed
- Pinterest gateway: 7 passed
- Pinterest publisher: 37 passed
- Publication scheduler: 11 passed
- Combined focused foundation: 72 passed
- Existing warning count: 1
- Frontend: `npm run build` passed (`tsc -b` and `vite build`)

This is not the final full release matrix.

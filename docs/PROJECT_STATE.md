# Project State

Diamond Shelf Social Studio is an internal Shopify-catalog-to-editorial-content review system. It preserves authentic branded product imagery, immutable review records, and explicit human control.

## Baselines

- Authoritative current `main`: `010e238c2750be8c85efa4d4c83b9aed48f3192e`
- Current main tree: `48335065aad89272b791362c4d8d56f238944b5e`
- Current main Alembic head: `0014`
- Task #38 starting main baseline: `4242b80e6626886b528641749beeb64cf7e4ea62`
- Task #38 starting tree: `ac35cffe2f58c1d47deac578e43cabe12189e519`
- Task #38 starting Alembic head: `0013`
- Task #38 branch: `task-38-publisher-scheduler-foundation-v1`
- Certified Task #38 branch SHA: `92614f876f10947c6c37c7f9bef056b07eefbb21`
- Certified and merged tree: `48335065aad89272b791362c4d8d56f238944b5e`
- Merged PR: #18 — Publisher + Scheduler Foundation v1
- Closed issue: #17 — Task #38 — Publisher + Scheduler Foundation v1 (Publishing Disabled)

Migration `backend/alembic/versions/0014_publisher_scheduler_foundation.py` is additive and historical migrations `0001`-`0013` were not rewritten. Known limitation: historical migration `0002` contains PostgreSQL-specific syntax such as `'[]'::json`, so Task #38 requires narrow actual `0013 -> 0014` verification rather than claiming the complete fresh SQLite migration chain passes.

## Task status

- Task #34: COMPLETE / merged
- Task #35: COMPLETE / merged
- Task #36: COMPLETE / merged
- Task #37: COMPLETE / merged
- Task #38: COMPLETE / merged

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

## Task #38 release certification

- Publication API: 17 passed
- Pinterest gateway: 7 passed
- Pinterest publisher: 37 passed
- Publication scheduler: 11 passed
- Combined focused foundation: 72 passed
- Focused foundation warning count: 1
- Migration 0014: PASS
- Task #34 regressions: 8 passed
- Task #35 regressions: 12 passed
- Task #36 regressions: 30 passed
- Task #37 regressions: 59 passed
- Full backend: 347 passed, 0 failures, 0 errors, 2 warnings
- Python compile: PASS
- Task #38 import verification: PASS (`TASK38_IMPORT_OK`)
- Alembic: sole head `0014`; `0014` directly follows `0013`
- Frontend: `npm run build` passed (`tsc -b` and `vite build`)

`RELEASE_MATRIX=PASS`. The final release matrix executed against Task #38 branch SHA `92614f876f10947c6c37c7f9bef056b07eefbb21` with tree `48335065aad89272b791362c4d8d56f238944b5e`. Merge commit `010e238c2750be8c85efa4d4c83b9aed48f3192e` has the same tree, so the content merged to `main` is exactly the certified tree.

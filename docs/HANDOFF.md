# Project Handoff

Diamond Shelf Social Studio (`intssere/SM_Poster`) turns trusted Shopify catalog facts into reviewable social creative while preserving authentic branded product imagery, immutable audit records, and explicit human control.

## Current baseline

- Authoritative current main for Task #39 audit continuity: `4b508a610cb1005fba4e7495d377341177d90be6`
- Current main tree: `23a9f3d61f5dcf1748ede054d3dd9f8b0f7640ba`
- Current main Alembic head: `0014`; Task #39 branch head: `0015`
- Task #38 branch: `task-38-publisher-scheduler-foundation-v1`
- Task #38 certified branch SHA: `92614f876f10947c6c37c7f9bef056b07eefbb21`
- Certified and merged tree: `48335065aad89272b791362c4d8d56f238944b5e`
- PR #18: MERGED
- Issue #17: CLOSED

Historical Task #38 starting baseline: main `4242b80e6626886b528641749beeb64cf7e4ea62`, tree `ac35cffe2f58c1d47deac578e43cabe12189e519`, Alembic `0013`. Do not confuse that starting point with the current merged main state above.

## Task status

- Task #34: COMPLETE / merged
- Task #35: COMPLETE / merged
- Task #36: COMPLETE / merged
- Task #37: COMPLETE / merged
- Task #38: COMPLETE / merged

## Non-negotiable rules

- Branded products, bottles, packages, and logos must come from authentic persisted Shopify product media.
- AI may generate approved supporting material only; it may not substitute an unapproved branded creative.
- Revisions are immutable; active-version selection is explicit.
- Human approval must bind exact revision/original and creative identity.
- Publication snapshots are provider-independent database/audit records.
- No auto-approval, autonomous publishing, autonomous scheduler worker, live Pinterest write, OpenAI call, browser automation, or automatic `PUBLISH_UNKNOWN` retry.
- Never place secrets in source, APIs, telemetry, logs, docs, or frontend storage.

## Task #38 implementation snapshot

Task #38 supplies provider-independent immutable publication snapshots, exact approved content/creative/source-media identity, real PinterestConnection and PinterestBoard binding, immutable external board/title/description/alt/destination/UTM/media snapshots, duplicate fingerprints, explicit human schedule/reschedule/cancel, timezone-aware UTC scheduling, deterministic due discovery, bounded batches of 25, transactional compare-and-set claims, durable STARTED attempts, unique attempt numbering, mockable Pinterest gateway boundary, conservative provider outcome classification, safe attempt metadata, authenticated publication APIs, server-derived readiness, hardened Publications/Scheduler frontend, ordered sanitized attempt-history display, controlled scheduling/rescheduling/cancellation, and no enabled live Publish UI.

Explicit human scheduling exists; autonomous/background scheduling does not.

## Provider gates and protected state

Provider execution requires complete snapshot data, exact approval identity, exact creative/source-media identity, connected Pinterest connection, matching active/eligible Pinterest board, unchanged external board snapshot, `PUBLISHING_ENABLED=true`, already-granted `pins:write`, public Pinterest-fetchable HTTPS media, and a matching request/attempt fingerprint.

Protected repository state remains `PUBLISHING_ENABLED=false`. Live OAuth requested scopes remain exactly `user_accounts:read`, `boards:read`, and `pins:read`; Task #38 must not request `pins:write` or `boards:write`. Fixture-only `pins:write` may appear in tests. These are two independent live-write blockers.

`PUBLISH_UNKNOWN` is never automatically retried. `PublicationReconciliationError` remains distinguishable through the API boundary.

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

## Repository/test state versus runtime state

Task verification uses isolated test databases. These fixtures do not represent deployed business data. No production database was connected or queried during this documentation pass.

Latest carried-forward runtime snapshot, historical and not freshly queried: 4 content revisions, 1 selection (Adagio v3), 5 telemetry records, 0 generated assets, 0 approvals, and 0 publications. Re-query deployed PostgreSQL before operational use.

## Pinterest SEO + Metadata + Creative Quality Release Gate

Before any future live publishing phase, apply the canonical gate in `docs/DECISIONS.md`: title/description relevance and limits, alt text, canonical URL/UTMs, eligible board/topic relevance, authentic provenance, public HTTPS media, vertical creative quality, Shopify metadata consistency, Open Graph / Schema.org / Rich Pin compatibility, unsupported-claim and keyword-stuffing rejection, duplicate Pin spam prevention, and topic/product-tag field review.

## Next implementation stage

Task #39 is the current PRE-MERGE task (Issue #20 OPEN), with Phase 1, Phase 2, and Phase 3A PASS and Phase 3B under independent certification. The next separately authorized dependency after Task #39 is Controlled Pinterest Write Enablement + Single-Pin Pilot; it is not authorized now.

Before any future live provider write, require write-scope authorization review, provider-access review, Pinterest SEO + Metadata + Creative Quality gate, public Pinterest-fetchable HTTPS media, duplicate prevention, final publication preview, explicit operator confirmation, safe `PUBLISH_UNKNOWN` reconciliation, runbook/incident handling, and controlled single-Pin validation. Do not introduce an autonomous worker as the immediate next step.
## Task #39 Phase 3B

Operator preview, readiness, authorization and PUBLISH_UNKNOWN reconciliation controls are available. Publishing remains disabled (`PUBLISHING_ENABLED=false`); live OAuth scopes are read-only. PUBLISH_UNKNOWN is never retried and requires explicit reconciliation.

Task #39 is PRE-MERGE remediation for Issue #20 (Phase 1, Phase 2, and Phase 3A passed; Phase 3B remains under independent certification). The authoritative starting main was `4b508a610cb1005fba4e7495d377341177d90be6` (tree `23a9f3d61f5dcf1748ede054d3dd9f8b0f7640ba`); PR #18 and PR #19 are merged continuity changes. Alembic remains `0015`. Dispatch authorization is a fifteen-minute, single-active, single-use server-bound confirmation; PUBLISH_UNKNOWN has only explicit reconciliation and no generic cancel/retry path. See `TASK39_OPERATOR_RUNBOOK.md` and `TASK39_SINGLE_PIN_PILOT_READINESS.md`. Runtime counts are carried-forward historical snapshots, not freshly queried production data. The next separately authorized dependency is Controlled Pinterest Write Enablement + Single-Pin Pilot.

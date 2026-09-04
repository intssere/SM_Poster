# Project Handoff

Diamond Shelf Social Studio (`intssere/SM_Poster`) turns trusted Shopify catalog facts into reviewable social creative while preserving authentic branded product imagery, immutable audit records, and explicit human control.

## Current baseline

- Authoritative Task #38 main baseline: `4242b80e6626886b528641749beeb64cf7e4ea62`
- Main baseline tree: `ac35cffe2f58c1d47deac578e43cabe12189e519`
- Main Alembic head: `0013`
- Task #38 branch: `task-38-publisher-scheduler-foundation-v1`
- Task #38 PRE-MERGE branch Alembic head: `0014`
- Code/frontend checkpoint entering documentation closure: `8c14903deac4978e36d9a65d62122d84d374978e`
- Checkpoint tree: `8ca7b7aa438cd1c6fd038e734a4ad24cdd5e4d53`

This SHA is the synchronized code/frontend checkpoint entering documentation closure. Do not place the documentation commit SHA inside this document; use the commit evidence after this pass as the next starting point for final release certification.

## Task status

- Task #34: COMPLETE / merged
- Task #35: COMPLETE / merged
- Task #36: COMPLETE / merged
- Task #37: COMPLETE / merged
- Task #38: PRE-MERGE

Task #38 has not been claimed as merged, PR_READY, or release certified. Do not create a Task #38 PR until the final release matrix and independent branch-vs-main audit are green and explicitly authorized.

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

## Focused verification history

- Publication API: 17 passed
- Pinterest gateway: 7 passed
- Pinterest publisher: 37 passed
- Publication scheduler: 11 passed
- Combined focused foundation: 72 passed
- Existing warning count: 1
- Frontend: `npm run build` passed (`tsc -b` and `vite build`)

This is not the final full release matrix.

## Repository/test state versus runtime state

Task verification uses isolated test databases. These fixtures do not represent deployed business data. No production database was connected or queried during this documentation pass.

Latest carried-forward runtime snapshot, historical and not freshly queried: 4 content revisions, 1 selection (Adagio v3), 5 telemetry records, 0 generated assets, 0 approvals, and 0 publications. Re-query deployed PostgreSQL before operational use.

## Pinterest SEO + Metadata + Creative Quality Release Gate

Before any future live publishing phase, apply the canonical gate in `docs/DECISIONS.md`: title/description relevance and limits, alt text, canonical URL/UTMs, eligible board/topic relevance, authentic provenance, public HTTPS media, vertical creative quality, Shopify metadata consistency, Open Graph / Schema.org / Rich Pin compatibility, unsupported-claim and keyword-stuffing rejection, duplicate Pin spam prevention, and topic/product-tag field review.

## Remaining release steps

1. Final release-certification matrix
2. Independent branch-vs-main audit
3. PR_READY authorization
4. PR creation
5. Independent PR review
6. Explicit human merge authorization

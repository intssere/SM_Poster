# Roadmap

## Completed and merged

- Shopify catalog sync and deterministic product intelligence
- Controlled proposal generation and authentic-product creative rendering
- Review-only OpenAI text/background foundation with fail-closed hosted behavior
- Immutable revisions, explicit version selection, and read-only comparison previews
- Task #34: exact approval binding and immutable publication identity snapshots
- Task #35: internal single-admin authentication and API authorization
- Task #36: Pinterest Account Connection/OAuth v1 with read-only scopes
- Task #37: Pinterest Board Sync & Board Manager v1, read-only board/section sync, connection-level sync timestamp, strict provider validation, and local eligibility/routing
- Task #38: Publisher + Scheduler Foundation v1, PR #18 merged, Issue #17 closed, Alembic `0014`

## Current state

Task #38 added immutable provider-independent publication snapshots, explicit human scheduling/rescheduling/cancellation, bounded due discovery, transactional claims, durable attempts, readiness reporting, outcome classification, safe attempt metadata, authenticated publication APIs, and a hardened scheduler frontend.

Task #38 does not turn publishing on. `PUBLISHING_ENABLED=false` remains authoritative; live OAuth requested scopes remain exactly `user_accounts:read`, `boards:read`, and `pins:read`; `pins:write` and `boards:write` are not requested live.

## Pinterest SEO + Metadata + Creative Quality Release Gate

Before any separately authorized live publishing phase, the canonical release gate in `docs/DECISIONS.md` must pass. It covers title/description limits and relevance, accurate alt text, canonical destination and UTM URLs, board/topic relevance, authentic creative provenance, public Pinterest-fetchable HTTPS media, vertical creative quality, Shopify metadata consistency, Open Graph / Schema.org / Rich Pin compatibility, unsupported-claim rejection, keyword-stuffing rejection, duplicate Pin spam prevention, and review of any supported Pinterest topic/product-tag fields.

## Future phases

Task #39 is COMPLETE / MERGED (PR #21; Issue #20 CLOSED). The next separately authorized dependency is Controlled Pinterest Write Enablement + Single-Pin Pilot; it is not authorized now. Analytics ingestion and automation remain later separate phases.

Required future readiness gates include Pinterest SEO + Metadata + Creative Quality validation, public Pinterest-fetchable HTTPS media, write-scope authorization review, provider-access review, duplicate prevention, final publication preview, explicit operator confirmation, safe `PUBLISH_UNKNOWN` reconciliation, runbook/incident handling, and controlled single-Pin validation.

Do not introduce an autonomous worker as the immediate next step.

Analytics ingestion remains a later separate phase. No later phase is enabled by Task #38.

Task #39 Phase 3B is complete and merged. The operator UI provides server-derived preview/readiness, explicit dispatch authorization and revocation, sanitized history, and PUBLISH_UNKNOWN reconciliation without enabling provider writes. The next separately authorized dependency is Controlled Pinterest Write Enablement + Single-Pin Pilot.
### Task #39 Phase 3B

Operator experience and runbook are complete; next separately authorized stage is controlled Pinterest write enablement and a single-Pin pilot.

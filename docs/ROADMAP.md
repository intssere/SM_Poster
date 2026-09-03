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

## Current pre-merge work

Task #38 is the PRE-MERGE Publisher + Scheduler Foundation on `task-38-publisher-scheduler-foundation-v1`. It adds immutable provider-independent publication snapshots, explicit human scheduling/rescheduling/cancellation, bounded due discovery, transactional claims, durable attempts, readiness reporting, outcome classification, safe attempt metadata, authenticated publication APIs, and a hardened scheduler frontend.

Task #38 does not turn publishing on. `PUBLISHING_ENABLED=false` remains authoritative; live OAuth requested scopes remain exactly `user_accounts:read`, `boards:read`, and `pins:read`; `pins:write` and `boards:write` are not requested live.

## Pinterest SEO + Metadata + Creative Quality Release Gate

Before any separately authorized live publishing phase, the canonical release gate in `docs/DECISIONS.md` must pass. It covers title/description limits and relevance, accurate alt text, canonical destination and UTM URLs, board/topic relevance, authentic creative provenance, public Pinterest-fetchable HTTPS media, vertical creative quality, Shopify metadata consistency, Open Graph / Schema.org / Rich Pin compatibility, unsupported-claim rejection, keyword-stuffing rejection, duplicate Pin spam prevention, and review of any supported Pinterest topic/product-tag fields.

## Future phases

The next stage after Task #38 merge is Publisher + Scheduler live-readiness design, not automatic enablement. It requires separate authorization for write-scope access, provider-access review, Pinterest SEO + Metadata + Creative Quality gate, public-media readiness, sandbox/mock validation, operational dispatcher/deployment authorization, and reconciliation/runbook readiness.

Analytics ingestion remains a later separate phase. No later phase is enabled by Task #38.

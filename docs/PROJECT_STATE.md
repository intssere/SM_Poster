# Project State

Diamond Shelf Social Studio is an internal Shopify-catalog-to-editorial-content review system. Task #38 authoritative starting `main` is `4242b80e6626886b528641749beeb64cf7e4ea62` (tree `ac35cffe2f58c1d47deac578e43cabe12189e519`).

Current capabilities include catalog normalization, controlled proposal generation, deterministic authentic-product creative rendering, immutable AI-assisted copy revisions, review-only generated backgrounds and video specifications, explicit version selection, approval auditing, and read-only revision previews.

Database head is Alembic `0013`. Migration 0013 adds nullable `PinterestConnection.boards_last_synced_at`; migrations add only compatible identity and read-only board snapshot structures.

Task #34 / PR #10 is merged on `main` at `6bff2e6cf36bbbac0c3f7831fe6680868d07a1be` (tree `22bae2de2346e408552eeeba57786959d38c2f2c`). Task #35 adds internal single-admin authentication without a migration.

Admin authentication protects operational API routes centrally. Sessions are signed, HttpOnly, Secure in exposed mode, SameSite strict, short-lived, and never stored in browser storage. Production fails closed without server-side auth configuration; authentication bypass is permitted only when explicitly enabled in local/test mode.

## Repository and test state

Task #34 adds models, migration logic, services, and isolated regression fixtures. Test-created approval/publication rows are ephemeral and do not represent deployed business data. No production database was connected or queried during this repository-only verification.

## Latest verified runtime state

The latest previously verified runtime snapshot was recorded during the controlled Adagio v3 verification, before Task #34:

- Content revisions: 4
- Version selections: 1 (Adagio v3 selected)
- AI telemetry: 5
- Generated assets: 0
- Approvals: 0
- Publications: 0

These are carried-forward runtime values, not fresh Task #34 measurements. Task #34 used isolated test databases and did not query the production PostgreSQL database. Re-verify against the deployed database before operational use.

`AISettings.enabled=false` and `PUBLISHING_ENABLED=false`. No provider or Pinterest publishing call is part of publication identity operations.

Task #38 is PRE-MERGE: Alembic `0014` adds immutable Pinterest publication destination/payload snapshots and durable publication attempts. Snapshot creation is database-only; provider dispatch separately requires publishing enabled, `pins:write`, approved identity, eligible destination, and public HTTPS media. No autonomous scheduler or `PUBLISH_UNKNOWN` retry is enabled.
## Task #36 status

Pinterest Account Connection/OAuth v1 is complete as a read-only account foundation. Alembic head is 0013; publishing and AI/provider modes remain disabled.

## Task #37 status

Pinterest Board Sync & Board Manager v1 is COMPLETE; PR #15 is MERGED and Issue #14 is CLOSED. It provides connection-level successful-sync state (including zero-board success), five-minute token preflight using Task #36 refresh at most once, strict provider metadata validation, and local-only eligibility/routing. No Pinterest board or Pin writes, scheduler, publishing, or analytics ingestion are enabled. Alembic head is `0013`.
### Task #38 current state
Task #38 is PRE-MERGE on `task-38-publisher-scheduler-foundation-v1` with Alembic `0014`. It provides immutable publication snapshots, explicit human scheduling, bounded due discovery, transactional claims, durable attempts, provider execution gates, and server-derived readiness. Live publishing, `pins:write` OAuth requests, autonomous workers, and automatic `PUBLISH_UNKNOWN` retries remain disabled.

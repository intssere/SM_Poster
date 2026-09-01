# Project State

Diamond Shelf Social Studio is an internal Shopify-catalog-to-editorial-content review system. The GitHub `main` baseline for Task #34 is merge commit `d2674c98eec5da08f6e5c1843aa3526fce0f857c` (PR #8).

Current capabilities include catalog normalization, controlled proposal generation, deterministic authentic-product creative rendering, immutable AI-assisted copy revisions, review-only generated backgrounds and video specifications, explicit version selection, approval auditing, and read-only revision previews.

Database head is Alembic `0010`. This migration adds nullable identity fields for historical compatibility; it does not invent revision identities for existing rows.

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

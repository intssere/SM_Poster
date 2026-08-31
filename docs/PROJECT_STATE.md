# Project State

Diamond Shelf Social Studio is an internal Shopify-catalog-to-editorial-content review system. The GitHub `main` baseline for Task #34 is merge commit `d2674c98eec5da08f6e5c1843aa3526fce0f857c` (PR #8).

Current capabilities include catalog normalization, controlled proposal generation, deterministic authentic-product creative rendering, immutable AI-assisted copy revisions, review-only generated backgrounds and video specifications, explicit version selection, approval auditing, and read-only revision previews.

Database head is Alembic `0010`. This migration adds nullable identity fields for historical compatibility; it does not invent revision identities for existing rows.

Protected production state at the Task #34 boundary:

- Content revisions: 2
- Version selections: 0
- AI telemetry: 5
- Generated assets: 0
- Approvals: 0
- Publications: 0

`AISettings.enabled=false` and `PUBLISHING_ENABLED=false`. No provider or Pinterest publishing call is part of publication identity operations.

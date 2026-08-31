# Phase 0 Architecture Decision Record

## Scope

Internal Diamond Shelf application. No SaaS tenancy, no browser automation, no bulk automatic Pin publishing.

## Core boundaries

1. **Commerce domain** — Shopify-derived product data and Diamond Shelf enrichment.
2. **Content domain** — concepts, angles, keyword clusters, copy drafts.
3. **Creative domain** — authentic product images + deterministic template rendering.
4. **Workflow domain** — review, approval, scheduling and audit trail.
5. **Provider boundary** — Pinterest is an adapter, not the product database.

## Publishing invariant

A Pin cannot enter the schedule without an explicit approved draft. A publication whose create request has an unknown outcome enters `PUBLISH_UNKNOWN`; it cannot automatically transition back to `PUBLISHING`.

## Duplicate controls

- Concept SHA-256 fingerprint
- Text SHA-256 fingerprint
- Creative SHA-256 fingerprint
- Publication unique fingerprint
- Perceptual image hashing planned for rendered creatives
- Text similarity planned with PostgreSQL `pg_trgm`

## Creative invariant

For a Pin representing a specific branded product, the merchandise layer must originate from a real catalog image. Generative systems may alter backgrounds/decorative layers, not recreate the product bottle.

## Phase 1 acceptance target

Generate and review high-quality editorial Pin drafts from Shopify products without production publishing.

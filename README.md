# Diamond Shelf Pinterest Engine

Internal catalog-to-editorial-content system for Diamond Shelf.

## Current status

**Phase 0 implementation scaffold. Pinterest production publishing is deliberately disabled.**

Implemented in this checkpoint:
- FastAPI backend scaffold
- PostgreSQL-ready SQLAlchemy domain model
- Initial Alembic migration
- Product scoring service
- UTM attribution service
- Multi-layer duplicate fingerprint service
- Shopify GraphQL gateway boundary
- Pinterest API v5 gateway boundary
- Publication state machine with `PUBLISH_UNKNOWN`
- Audit-ready data model
- React/Vite internal dashboard scaffold
- Docker Compose development environment
- Unit tests for deterministic core logic

## Architecture

```text
Shopify GraphQL -> Product DB -> Selection/Content -> Creative -> Approval -> Scheduler -> Pinterest API
                                      |                             |
                                      +---- duplicate controls -----+
```

The application owns product/content/creative data. Pinterest-originated data is isolated behind the Pinterest integration boundary.

## Quick start on Replit

Backend:

```bash
cd backend
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Frontend:

```bash
cd frontend
npm run dev
```

Replit provides `DATABASE_URL` and the PostgreSQL variables automatically after the
project database is enabled. The configured `Start application` workflow runs the
frontend on port 5000 and proxies `/api` requests to the backend on port 8000.
The configured `Backend API` workflow applies the existing Alembic migrations and
then runs the FastAPI service separately. Replit does not use the Docker database
URL from `.env.example`.

Optional local Docker development:

```bash
docker compose up --build
```

For Docker Compose, copy `.env.example` to `.env`; its `DATABASE_URL` targets the
Compose `db` service and is separate from Replit PostgreSQL.

## Safety gate

`PUBLISHING_ENABLED=false` by default. Phase 1 must remain generation/review focused. Production publishing is a Phase 2 capability after Pinterest OAuth/testing/access requirements are satisfied.

## Shopify catalog sync

The Products screen reads the normalized catalog from PostgreSQL and shows Shopify
connection state, sync progress, product eligibility, and normalization status.
Catalog synchronization uses the official GraphQL Admin API bulk operation flow.

To connect a store, configure:

- `SHOPIFY_SHOP` as the store's `myshopify.com` subdomain without the suffix
- `SHOPIFY_CLIENT_ID` as a Replit Secret
- `SHOPIFY_CLIENT_SECRET` as a Replit Secret
- `SHOPIFY_API_VERSION` as an optional environment variable (defaults to `2026-07`)

The server exchanges Client Credentials for an Admin API token, caches the token
only in server memory, and renews it before Shopify's returned expiration time
(currently approximately 24 hours). The app version in Shopify must include
`read_products` and `read_inventory`.

`SHOPIFY_ACCESS_TOKEN` remains supported as an optional legacy fallback when a
complete Client Credentials configuration is not present. Client Credentials
always take priority when both modes are configured.

If these values are absent, the app remains available and shows “Shopify not
connected.” Pinterest publishing remains disabled independently of Shopify.

## ProductIntelligence Normalization v2

Normalization uses only explicit Shopify/catalog facts. It does not use AI or
infer attributes from brand stereotypes. The current completeness rules are:

| Category | Required for `COMPLETE` | Optional when unknown |
|---|---|---|
| Fragrance | Brand, audience, concentration, fragrance family, price band | Size, brand classification, notes, gift suitability, season, occasion |
| Gift set | Brand, explicit gift suitability, price band | Audience, size, concentration, family, notes, season, occasion |
| Bath & body | Brand, price band | Audience, size, concentration, family, notes, season, occasion |
| Beauty / skin / hair | Brand, price band | Audience, size, concentration, family, notes, season, occasion |
| Home fragrance / candles | Brand, price band | Audience, size, concentration, family, notes, season, occasion |
| Other | Brand, price band | All category-specific enrichment |

`UNKNOWN` is reserved for records with no usable trusted source facts.
Identifiable products missing category-required facts are `PARTIAL`.
Eligibility is evaluated separately and retains the existing stock, active
status, and catalog-image gate.

Audience, concentration, size, gift suitability, and fragrance family are
normalized only from explicit metafields, tags, collections, variant titles,
product types, or product-title text appropriate to that attribute. Size
provenance and parsed value/unit are retained in `normalized_data`. Fragrance
notes, season, and occasion remain unknown unless trusted source fields exist.

Designer, niche, and Arabian/Middle Eastern classifications use the reviewed
exact-vendor sets in `backend/app/services/product_taxonomy.py`. Unknown vendors
remain unclassified. Product-type inconsistencies are recorded as QA warnings;
the app never rewrites Shopify source taxonomy.

Re-normalize the persisted catalog without contacting Shopify:

```bash
cd backend
PYTHONPATH=. python scripts/renormalize_product_intelligence.py
```

The command reports before/after counts, category totals, 25 representative
products, and a second-pass idempotence check. It does not create a catalog sync
job and does not call Shopify.

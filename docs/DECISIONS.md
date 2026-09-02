# Decisions

## Authentic Shopify product imagery

Branded products and packaging must come from persisted Shopify product images. AI may create approved background/supporting material only and may not recreate products, packages, or logos.

## Human control

Generation never implies selection, approval, scheduling, or publication. Version selection is explicit. Approval binds the exact selected revision/creative and does not cover future revisions.

## Immutable publication identity

Publication rows are snapshots, not joins to mutable current state. Nullable v2 columns preserve historical rows without fabricating provenance. Duplicate publication identity is enforced by a deterministic unique fingerprint.

## Provider policy

Hosted text defaults to `gpt-5.6-luna`; `gpt-5.6-terra` is explicit-only escalation. Images use `gpt-image-2`. Hosted failures fail closed. Video remains a reviewable specification with rendering disabled.

## Publishing policy

`PUBLISHING_ENABLED=false` is authoritative. No Pinterest API, scheduling worker, auto-approval, or automatic publication is permitted in the current phase.

## Authentication v1

Use a stateless HMAC-signed, short-lived HttpOnly cookie for the single admin. Keep credentials and secrets in server configuration, enforce Origin checks for unsafe API requests, and fail closed in exposed environments. Do not add a database migration.
## Task #36

Use Authorization Code OAuth with one-time hashed state and Fernet-encrypted credentials. Restrict scopes to account/board/Pin reads; retain publishing disabled.

## Task #37

Board synchronization persists normalized read snapshots through additive migration `0012`. Board and section mutations, Pin writes, scheduling, and analytics ingestion are prohibited; local selection never authorizes publication.

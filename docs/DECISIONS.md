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

Board synchronization persists normalized read snapshots through additive migrations `0012` and `0013`; `0013` stores connection-level successful-sync time. Board and section mutations, Pin writes, scheduling, and analytics ingestion are prohibited; local selection never authorizes publication.

### Safe Pinterest token refresh

Connection refresh uses a prepare-then-commit boundary: provider data, scopes,
expirations, and replacement credentials are validated and encrypted into
locals before the connection is mutated. If no replacement refresh token is
provided, the existing encrypted credential is retained. Provider, parsing,
validation, or encryption failures preserve credential metadata and record only
a sanitized error code; database failures roll back without exposing provider
payloads or secrets. Refresh is explicitly invoked and provider-read-only; no
scheduler or background refresh job is enabled.

## Task #38 publisher boundary

Publication identity snapshots are pure database/audit operations independent of `PUBLISHING_ENABLED`. Pinterest execution is a separate gate requiring publishing enabled and `pins:write`; current OAuth requests remain read-only and no autonomous scheduler is enabled.
# Board-sync token refresh policy

Board synchronization performs a five-minute access-token expiry preflight. Healthy tokens are not refreshed; expired or imminently expiring tokens invoke the existing Task #36 `refresh_connection` helper at most once. The refreshed encrypted credential is used for discovery. Refresh failure fails closed before board/section requests, reconciliation, or advancement of `boards_last_synced_at`; there is no background scheduler or retry loop, and OAuth scopes remain read-only.

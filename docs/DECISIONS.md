# Decisions

## Authentic Shopify product imagery

Branded products, bottles, packages, and logos must come from authentic persisted Shopify product media. AI may create approved supporting/decorative material only and may never recreate branded products, packages, or logos. Task #38 provider execution uses the exact approved creative/source-media identity.

## Human control

Generation never implies selection, approval, scheduling, or publication. Version selection is explicit. Approval binds the exact selected revision/original version and creative. Task #38 allows explicit human scheduling, rescheduling, and cancellation; it does not add autonomous scheduling, publishing, or retry behavior.

## Immutable publication identity

Publication snapshots are provider-independent audit records, not joins to mutable current state. They freeze exact approved revision/original-content identity, exact creative identity, authentic source-image provenance, PinterestConnection and PinterestBoard identities, external board ID, title, description, alt text, destination/UTM URL, media URL, and a deterministic duplicate-protection fingerprint.

## Publication state machine

`APPROVED -> SCHEDULED -> CANCELLED`; `SCHEDULED -> PUBLISHING -> CANCELLED`; `PUBLISHING -> PUBLISHED | PUBLISH_FAILED | PUBLISH_UNKNOWN`; `PUBLISH_FAILED -> SCHEDULED` only by explicit human/admin reschedule or `CANCELLED`; `PUBLISH_UNKNOWN -> PUBLISHED` only by explicit/manual reconciliation or `CANCELLED`. `PUBLISHED` and `CANCELLED` are terminal. `PUBLISH_UNKNOWN` is never automatically retried.

## Scheduling and concurrency policy

Task #38 provides reusable scheduling, due-query, and claim primitives. Only due `SCHEDULED` rows are claimable. Claiming is transactional compare-and-set to `PUBLISHING`, creates a durable STARTED `PublicationAttempt` before provider execution, and revalidates attempt identity/request fingerprint before dispatch. Batches are bounded to 25. No Python-only mutex, cron, startup worker, or background scheduler is relied upon.

## Provider-write blockers

`PUBLISHING_ENABLED=false` remains authoritative. Live Pinterest OAuth requested scopes remain exactly `user_accounts:read`, `boards:read`, and `pins:read`. Task #38 does not request `pins:write` or `boards:write`. Provider execution requires both `PUBLISHING_ENABLED=true` and already-granted `pins:write`, so protected state has two independent write blockers. Test fixtures may simulate `pins:write`; live OAuth does not request it.

## Readiness and dispatch gates

Provider execution requires complete snapshot data, exact approval identity, exact creative/source-media identity, connected Pinterest account, matching active and eligible Pinterest board, unchanged external board snapshot, enabled publishing, already-granted `pins:write`, public Pinterest-fetchable HTTPS media, and matching attempt fingerprint. Readiness inspection is non-mutating and may return `PUBLISHING_DISABLED`, `INVALID_PUBLICATION_STATE`, `NOT_DUE`, `INCOMPLETE_SNAPSHOT`, `PUBLISHING_SCOPE_REQUIRED`, `INVALID_DESTINATION`, `DESTINATION_MISMATCH`, `INVALID_APPROVAL`, `INVALID_CREATIVE`, `MEDIA_NOT_PUBLISHABLE`, or `READY`.

## Publisher outcome policy

Valid provider success with a validated provider Pin ID becomes `PUBLISHED`. Definitive provider rejection becomes `PUBLISH_FAILED`. Timeout, reset, ambiguous transport, 5xx, invalid/missing successful Pin ID, or uncertain persistence becomes `PUBLISH_UNKNOWN`. Provider success followed by local persistence uncertainty uses `PUBLISHED_STATE_PERSISTENCE_UNKNOWN`. `PublicationReconciliationError` is preserved through the API boundary rather than collapsed into a generic provider 502.

## Safe attempt metadata

Allowed attempt metadata keys are exactly `validated_pin_id`, `http_status`, `provider_error_code`, `request_id`, and `correlation_id`. Do not persist or serialize access tokens, refresh tokens, Authorization or Bearer credentials, `client_secret`, raw bodies, raw JSON, tracebacks, exceptions, raw provider error bodies, or arbitrary metadata. Attempt DTOs expose only `attempt_number`, `status`, `started_at`, `completed_at`, `provider_pin_id`, `error_code`, and `safe_response_metadata`; `request_fingerprint` remains hidden.

## Authentication v1

Use a stateless HMAC-signed, short-lived HttpOnly cookie for the single admin. Keep credentials and secrets in server configuration, enforce Origin checks for unsafe API requests, and fail closed in exposed environments.

## Pinterest SEO + Metadata + Creative Quality Release Gate

Before any separately authorized live publishing phase, each candidate Pin must pass this gate:

- relevant and complete title within Pinterest/provider limits
- useful, non-spammy, keyword-relevant description within provider limits
- accurate alt text
- canonical destination URL and intentional UTM parameters
- correct eligible destination board and board/topic relevance
- approved authentic product/creative provenance
- public Pinterest-fetchable HTTPS image
- strong vertical Pinterest creative, typically 2:3 where appropriate
- current target creative such as 1000x1500 when appropriate
- Shopify product metadata consistency
- Open Graph / Schema.org / Rich Pin compatibility where applicable
- no unsupported claims
- no keyword stuffing
- no duplicate or near-duplicate editorial Pin spam
- supported Pinterest topic/product-tag fields evaluated before future enablement rather than silently omitted

This is a future live-publishing release gate. It does not enable provider writes now.

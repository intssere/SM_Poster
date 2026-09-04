# Security and Safety

## Current controls

- `PUBLISHING_ENABLED=false` blocks protected-state publication.
- Live Pinterest OAuth requested scopes are exactly `user_accounts:read`, `boards:read`, and `pins:read`.
- Task #38 does not request `pins:write` or `boards:write`; fixture-only `pins:write` may appear in tests.
- Protected AI settings remain disabled; hosted provider failures fail closed.
- Operational APIs require Task #35 authentication, and unsafe API methods retain Origin/CSRF protection.

## Authentication

Task #35 centralizes single-admin authentication. Sessions use signed HttpOnly cookies, SameSite strict, Secure in exposed mode, automatic expiry, and no browser token storage. Exposed production/Replit modes fail closed without server-side auth configuration. Credentials and secrets must remain in server configuration only.

## Publication safety

Publication snapshots reject incomplete provenance, duplicate fingerprints, identity mismatches, invalid destinations, and unsafe media. Snapshot creation is provider-independent and database-only. Provider execution is separately gated by preflight and execution revalidation.

No provider call may occur when gates fail. Provider execution requires complete immutable snapshot, exact approval identity, exact creative/source-media identity, connected PinterestConnection, matching active/eligible PinterestBoard, unchanged external board snapshot, `PUBLISHING_ENABLED=true`, already-granted `pins:write`, public Pinterest-fetchable HTTPS media, and matching request/attempt fingerprint.

`PUBLISH_UNKNOWN` is never automatically retried. Automatic scheduling/publishing is prohibited; explicit human scheduling is allowed. No autonomous scheduler/background worker exists in Task #38.

## Attempts and provider outcome safety

Durable STARTED attempts are created before provider execution. Valid provider success with a validated Pin ID becomes `PUBLISHED`; definitive provider rejection becomes `PUBLISH_FAILED`; ambiguous transport, timeout, reset, 5xx, missing/invalid Pin ID, or persistence uncertainty becomes `PUBLISH_UNKNOWN`. Provider success followed by local persistence uncertainty uses `PUBLISHED_STATE_PERSISTENCE_UNKNOWN`. `PublicationReconciliationError` remains typed and distinguishable through the API boundary.

Attempt metadata is allowlisted to:

- `validated_pin_id`
- `http_status`
- `provider_error_code`
- `request_id`
- `correlation_id`

Never persist or serialize access tokens, refresh tokens, Authorization or Bearer credentials, `client_secret`, raw bodies, raw JSON, tracebacks, exceptions, raw provider error bodies, or arbitrary metadata. Attempt DTOs do not expose `request_fingerprint`.

## Frontend safety

The Publications frontend has no enabled Publish control and no call to `POST /api/publications/{id}/publish`. It uses per-publication datetime-local state, converts schedule values to timezone-aware ISO strings, shows controlled server error details, relies on browser-managed Origin, keeps `credentials: 'include'`, displays identity/destination/readiness and ordered sanitized attempts, and stores no tokens, ciphertext, or client secrets.

## Authentic branded media

Branded products, bottles, packages, and logos must come from authentic persisted Shopify product media. AI-generated material, where separately authorized, may only serve supporting/decorative roles and must not substitute an unapproved product creative. Task #38 provider execution uses the exact approved creative/source-media identity.

## Pinterest read foundations

Task #36 OAuth uses one-time hashed state records, server-side code exchange, encrypted tokens, and read-only scopes. Task #37 board sync validates provider metadata before persistence; malformed containers or values fail closed without silent stringification/truncation. Board sync uses a five-minute token-expiry preflight and one bounded refresh. Refresh failures preserve inventory and sync timestamps, with no background refresh or provider retry loop.
### Phase 3B controls

Provider credentials remain server-side; the UI does not store tokens or invoke provider writes. Publishing is disabled and PUBLISH_UNKNOWN cannot be retried.

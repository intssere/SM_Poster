# Architecture

Diamond Shelf Social Studio separates commerce facts, editorial review, creative provenance, publication identity, scheduling primitives, and provider dispatch.

```text
Shopify catalog -> proposals/revisions -> authentic-image creatives
                                      |             |
                                      v             v
                              exact approval -> immutable publication snapshot
                                                        |
                         explicit scheduler/claim -> provider boundary (disabled)
```

## Primary Task #38 boundaries

- `publication_identity.py`: creates provider-independent immutable publication snapshots as pure database/audit records. Snapshot creation is independent of `PUBLISHING_ENABLED`.
- `publication_scheduler.py`: provides explicit human schedule/reschedule/cancel, deterministic due discovery, bounded batches of 25, request fingerprints, and transactional compare-and-set claims.
- `pinterest_publisher.py`: performs readiness and execution revalidation, classifies provider outcomes, sanitizes attempt metadata, and never auto-retries `PUBLISH_UNKNOWN`.
- `integrations/pinterest/gateway.py`: mockable Pinterest v5 boundary. Provider writes remain separately gated and disabled in protected state.
- API/admin route boundary: publication operational APIs are authenticated, unsafe methods remain behind Origin/CSRF protection, and DTOs expose only safe identity/readiness/attempt fields.
- Frontend management boundary: Publications UI provides controlled human scheduling/rescheduling/cancellation and read-only readiness/attempt visibility, with no enabled Publish control.

## Claim and execution flow

Only due `SCHEDULED` publications may be claimed. Claiming uses a database compare-and-set transition:

```text
SCHEDULED -> PUBLISHING -> durable STARTED attempt -> execution revalidation -> provider boundary
```

The STARTED `PublicationAttempt` is committed before provider execution. A second concurrent dispatcher cannot claim the same publication. Attempt identity and request fingerprint are revalidated immediately before provider dispatch. No Python-only mutex is relied upon.

## State machine

```text
APPROVED -> SCHEDULED -> CANCELLED
SCHEDULED -> PUBLISHING -> CANCELLED
PUBLISHING -> PUBLISHED | PUBLISH_FAILED | PUBLISH_UNKNOWN
PUBLISH_FAILED -> SCHEDULED (explicit human/admin reschedule only) | CANCELLED
PUBLISH_UNKNOWN -> PUBLISHED (explicit/manual reconciliation only) | CANCELLED
PUBLISHED terminal
CANCELLED terminal
```

`PUBLISH_UNKNOWN` is never automatically retried.

## Readiness and provider gates

Provider execution requires complete immutable snapshots, exact approval identity, exact creative/source-media identity, a connected Pinterest connection, a matching active and eligible Pinterest board, unchanged external board snapshot, `PUBLISHING_ENABLED=true`, already-granted `pins:write`, public Pinterest-fetchable HTTPS media, and a matching request/attempt fingerprint.

Readiness reasons are:

- `PUBLISHING_DISABLED`
- `INVALID_PUBLICATION_STATE`
- `NOT_DUE`
- `INCOMPLETE_SNAPSHOT`
- `PUBLISHING_SCOPE_REQUIRED`
- `INVALID_DESTINATION`
- `DESTINATION_MISMATCH`
- `INVALID_APPROVAL`
- `INVALID_CREATIVE`
- `MEDIA_NOT_PUBLISHABLE`
- `READY`

Readiness inspection is non-mutating: it does not create attempts, claim rows, decrypt tokens, or perform provider HTTP.

## Outcome and metadata safety

Valid success with a validated provider Pin ID becomes `PUBLISHED`. Definitive provider rejection becomes `PUBLISH_FAILED`. Timeout, reset, ambiguous transport, 5xx, invalid/missing successful Pin ID, or persistence uncertainty becomes `PUBLISH_UNKNOWN`. Provider success followed by local persistence uncertainty uses `PUBLISHED_STATE_PERSISTENCE_UNKNOWN`. `PublicationReconciliationError` remains typed and is preserved through the API boundary.

Attempt metadata is allowlisted to `validated_pin_id`, `http_status`, `provider_error_code`, `request_id`, and `correlation_id`. Raw provider bodies, tokens, Authorization headers, client secrets, tracebacks, exceptions, and arbitrary metadata are not persisted or serialized. Attempt DTOs expose only attempt number, status, timestamps, provider Pin ID, error code, and sanitized metadata; they do not expose request fingerprints.

## Pinterest foundations

Task #36 provides read-only OAuth with encrypted credentials and one-time hashed state. Task #37 provides read-only board/section snapshots, connection-level successful sync time, strict provider metadata validation, five-minute token refresh preflight, and local-only eligibility/routing. Task #38 binds new publications to real PinterestConnection and PinterestBoard identities without enabling live writes.

Protected state keeps `PUBLISHING_ENABLED=false` and live OAuth scopes exactly `user_accounts:read`, `boards:read`, and `pins:read`.
### Operator controls

Phase 3B uses server-derived publication preview/readiness and explicit authorization/reconciliation APIs; the frontend never handles provider credentials or invokes publish directly.

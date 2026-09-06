# Task #41 Phase 3B: Buffer pilot execution gate

**LIVE BUFFER/PINTEREST WRITE NOT AUTHORIZED**

**NO postsWrite CREDENTIAL AUTHORIZED**

**NO LIVE PIN AUTHORIZED**

Task #40 / Issue #23 remains open.

## Purpose

Phase 3B adds a pure, read-only execution-readiness evaluator for a future
separately authorized one-write Buffer pilot. It does not create an API route,
does not change frontend behavior, does not add a migration, does not introduce
a worker, and does not integrate with dispatch.

The service is `evaluate_buffer_pilot_execution_readiness(...)` in
`backend/app/services/buffer_pilot_execution_gate.py`.

## Static Vs Final Execution Readiness

Phase 3A answers whether a named publication is locally certifiable as
`STATIC_CANDIDATE_READY`. Phase 3B answers whether that exact candidate also
has due time, protected gates, Task #39 authorization, exact Buffer pilot
bindings, and explicit external evidence required for `FINAL_EXECUTION_READY`.

Current production/runtime defaults keep the result locked:

- `PUBLISHING_ENABLED=false`
- `BUFFER_PUBLISHING_ENABLED=false`
- `BUFFER_SINGLE_PIN_PILOT_ENABLED=false`

With those defaults, even a valid Phase 3A candidate remains
`LIVE_EXECUTION_LOCKED`.

## Immutable Execution Evidence

External evidence is passed through the frozen
`BufferPilotExecutionEvidence` dataclass. Its fields are non-secret:

- `publication_id`
- `publication_fingerprint`
- `request_fingerprint`
- `buffer_organization_id`
- `buffer_pinterest_channel_id`
- `board_service_id`
- `media_url`
- `observed_at`
- `write_credential_authorized`
- `provider_destination_live_verified`
- `media_live_fetch_verified`

All evidence booleans default to `false`. Credential presence, credential
shape, configured organization/channel IDs, static board binding, and static
media URL validity never imply these booleans.

## Evidence Freshness

Evidence must be fresh within the Task #39 authorization TTL. Missing,
future-dated, or stale evidence returns `EXECUTION_EVIDENCE_STALE`.
Malformed runtime evidence timestamps, such as strings, integers, or arbitrary
objects in `observed_at`, return `EXECUTION_EVIDENCE_INVALID` instead of
raising unbounded type/attribute errors.

Evidence identity must exactly match the current publication, current request
fingerprint, Buffer organization/channel settings, destination board service
ID, and media URL. Any mismatch returns `EXECUTION_EVIDENCE_MISMATCH`.

## Credential And Provider Boundaries

`BUFFER_API_KEY` presence is not write authorization. The gate only observes
the explicit `write_credential_authorized` boolean supplied by the operator or
release controller. The credential value, prefix, suffix, length, hash,
Authorization header, raw provider body, or secret-derived identity must never
appear in the result.

Provider destination and media fetch evidence are also external:

- `PROVIDER_DESTINATION_NOT_VERIFIED`
- `MEDIA_NOT_LIVE_VERIFIED`

The evaluator performs no Buffer read, no Pinterest read, no gateway
construction, no HTTP client construction, and no provider write.

## External Link Policy

Real Buffer-created Pinterest `externalLink` format is not a prerequisite for
the first live Pin because it cannot be observed until after that write. The
fixture/schema parser remains unchanged. If a future live pilot returns an
unexpected format, the publication must move to `PUBLISH_UNKNOWN`, no second
`createPost` is allowed, and parser broadening requires new evidence.

## Task #39 Authorization

Phase 3B reads the latest Task #39 authorization and validates it with the
existing authorization helper. It does not create, expire, revoke, consume, or
mutate authorization rows.

The authorization must be active, unexpired, unused, unrevoked, and bound to
the exact publication snapshot. Request-fingerprint exactness is enforced by
the current request fingerprint, Phase 3A result, Buffer pilot binding, and
Phase 3B evidence because the authorization model does not persist a request
fingerprint.

## TOCTOU Strategy

Each Phase 3B evaluation attempts a fresh persisted read from an independent
Engine-backed SQLAlchemy session, which is the normal production/PostgreSQL
path. Caller-bound SQLAlchemy `Connection` objects are rejected because Phase
3B must never take ownership of, commit, roll back, or close the caller's
transaction. SQLite `:memory:` sessions with a caller-owned active transaction
also fail closed because SQLAlchemy may share the same underlying connection in
that mode.

Any situation where independent persisted-state isolation cannot be guaranteed
returns `FRESH_PERSISTED_READ_UNAVAILABLE` instead of
`FINAL_EXECUTION_READY`. The caller's session is kept under `no_autoflush`, so
dirty unrelated caller objects are not flushed, discarded, or used as execution
evidence. This prevents stale identity-map objects from allowing publication
status, destination, or authorization drift to pass as `FINAL_EXECUTION_READY`.

Future live wiring should call this evaluator twice:

1. during operator/readiness review;
2. immediately before atomic claim in a separate release-authorized dispatch
   phase.

This catches candidate drift between static certification and execution. Phase
3B itself does not call `dispatch_buffer`, claim attempts, or consume
authorization.

## Bounded Statuses

`execution_status` is one of:

- `LIVE_EXECUTION_LOCKED`
- `FINAL_EXECUTION_READY`

`lock_reason` is `None` only for `FINAL_EXECUTION_READY`. Bounded lock reasons
include:

- `STATIC_CERTIFICATION_BLOCKED`
- `NOT_DUE`
- `PUBLISHING_DISABLED`
- `BUFFER_PUBLISHING_DISABLED`
- `BUFFER_PILOT_DISABLED`
- `BUFFER_PILOT_BINDING_MISMATCH`
- `BUFFER_PILOT_PRIOR_ATTEMPT`
- `FRESH_PERSISTED_READ_UNAVAILABLE`
- `AUTHORIZATION_REQUIRED`
- `AUTHORIZATION_EXPIRED`
- `AUTHORIZATION_REVOKED`
- `AUTHORIZATION_CONSUMED`
- `AUTHORIZATION_MISMATCH`
- `EXECUTION_EVIDENCE_REQUIRED`
- `EXECUTION_EVIDENCE_INVALID`
- `EXECUTION_EVIDENCE_STALE`
- `EXECUTION_EVIDENCE_MISMATCH`
- `WRITE_CREDENTIAL_NOT_AUTHORIZED`
- `PROVIDER_DESTINATION_NOT_VERIFIED`
- `MEDIA_NOT_LIVE_VERIFIED`

## No Runtime Capability Change

Phase 3B adds no live capability. It does not alter OAuth scopes, does not
authorize postsWrite, does not enable Buffer publishing, does not enable
Pinterest writes, does not add automatic retry, and does not add autonomous
polling or workers.

Future integration requires separate release authorization.

# Task #40 — Controlled Pinterest Write + Single-Pin Pilot Design

## Authorization boundary

**NOT AUTHORIZED FOR `pins:write`**  
**NOT AUTHORIZED FOR LIVE PIN WRITE**

This Phase 0 document is design-only. It does not request a write scope,
reconnect OAuth, create a Pin, or change runtime behavior.

## Baseline and protected state

Design baseline: main commit `73b2d0c5455f0c85d9c0ab8bae7908ff5231defe`,
tree `7d96013385b6b01360beaf6b27a26d821d3ca71f`, Alembic `0015`.
`PUBLISHING_ENABLED=false` is the repository default and remains authoritative.
Live OAuth requested scopes are exactly `user_accounts:read`, `boards:read`, and
`pins:read`; `pins:write` and `boards:write` are not requested. There is no
autonomous worker, automatic `PUBLISH_UNKNOWN` retry, browser automation, or
live provider request. The Task #39 operator runbook and single-Pin readiness
document remain required operational references.

## Existing provider call flow

An authenticated operator reaches the existing publication API, which loads an
immutable `PinPublication`. The manual dispatch service validates the persisted
publication, its approval, quality and duplicate state, then validates the
server-side `PublicationDispatchAuthorization` snapshot (fingerprint, quality,
readiness, duplicate result, actor, TTL and single-use status). An atomic
compare-and-set consumes that authorization, claims the publication, and writes
one durable `PublicationAttempt` in `STARTED` state. Post-claim readiness is
revalidated immediately before the gateway boundary. Credentials are decrypted
server-side and public HTTPS media is checked. The existing
`PinterestV5Gateway.create_pin` performs the provider POST only after every gate.
The response is classified as `PUBLISHED`, `PUBLISH_FAILED`, or
`PUBLISH_UNKNOWN`; persistence uncertainty is represented separately as
`PUBLISHED_STATE_PERSISTENCE_UNKNOWN`. Safe allowlisted attempt metadata is
persisted and raw bodies, headers, tokens, fingerprints, and secrets are never
serialized.

The Task #40 pilot gate belongs immediately before provider execution, inside
the trusted manual-dispatch orchestration, after all existing post-claim gates
and before gateway construction/POST.

## Proposed two-key pilot gate

Add a server-only, default-false setting conceptually named
`PINTEREST_SINGLE_PIN_PILOT_ENABLED`. Provider execution requires both this
flag and `PUBLISHING_ENABLED=true`; neither is browser-controlled and the
global kill switch remains meaningful. The pilot flag is temporary, reviewed,
and removed/disabled after one attempt. No flag bypasses approval, quality,
duplicate, destination, media, authorization, or attempt checks.

## Exact candidate binding

Reuse the existing `PublicationDispatchAuthorization` snapshot binding rather
than creating a second approval system. Immediately before dispatch, resolve
the publication and verify the authorization is ACTIVE, unexpired, single-use,
and bound to the unchanged publication fingerprint, exact
`pinterest_connection_id`, board record ID and external board ID, creative ID
and fingerprint, source image ID, and UTM destination. Re-run quality PASS,
duplicate SAFE_TO_CONTINUE, exact approval/revision/creative identity,
connected active/eligible board, public HTTPS media, no known Pin ID, and
non-PUBLISHED/non-PUBLISH_UNKNOWN state. The actor is derived from the
authenticated server session. The browser supplies none of these trusted
values.

## OAuth write-scope design (future Gate A only)

When a separately authorized server-side write-scope setting is false, OAuth
requests remain exactly the current three read scopes. A future, default-false
`PINTEREST_WRITE_SCOPE_ENABLED` may add only `pins:write` when Gate A is
approved; `boards:write` is permanently excluded. The callback must persist
the provider-granted scopes, prove `pins:write` was actually granted, and keep
credentials encrypted. Reconnection/upgrade is explicit, reversible by
revocation and reconnection, and never browser-controlled. This design does
not implement that setting or perform a reconnect.

## One-write and concurrency enforcement

The first valid invocation consumes the single-use authorization and claims the
publication via the existing transactional CAS, then creates one STARTED
attempt. A second invocation sees consumed authorization, a non-schedulable
state, an existing attempt/known Pin, or a duplicate and makes zero provider
calls. Concurrent sessions contend on the same row/unique authorization and
attempt invariants; only one transaction can claim. `PUBLISH_UNKNOWN` and
`PUBLISHED_STATE_PERSISTENCE_UNKNOWN` stop further writes and require explicit
reconciliation. A pilot-specific audit binding, if later needed, must be
validated in the same transaction and cannot create a second publisher.

## Route decision

Retain the existing authenticated `/api/publications/{id}/publish` route and
insert the pilot guard in the trusted manual-dispatch service. A second
endpoint would duplicate authorization and increase reachable write surface.
The frontend remains without a generic publish control; a future pilot must be
an explicitly authorized operator action against the exact candidate.

## Failure-state model

- Valid provider success with one validated Pin ID: `PUBLISHED`.
- Definitive provider rejection: `PUBLISH_FAILED`; no automatic retry.
- Timeout, reset, 5xx, missing/invalid Pin ID, duplicate Pin, or ambiguous
  persistence: `PUBLISH_UNKNOWN` (or the typed persistence-uncertainty code).
- `PUBLISH_UNKNOWN` transitions only through explicit provider Pin confirmation
  or `CANCELLED_UNKNOWN` reconciliation; it is never retried.
- Any failed gate, disabled key, missing scope, invalid media, identity
  mismatch, or credential-decryption failure makes zero provider calls.

## Gate A evidence checklist

Before requesting authorization: all implementation tests pass; defaults remain
read-only; `boards:write` is absent; write-scope capability is default false;
no real provider write occurred; exact Pinterest account/connection is
identified; reconnect and rollback/revocation procedures are documented; and
tokens remain encrypted/server-side. Gate A permits only requesting and
verifying `pins:write`; it never permits Pin creation.

## Gate B evidence checklist

Gate A is complete and `pins:write` is actually granted; `boards:write` remains
absent; one exact publication, account, board, approved revision/creative,
quality PASS, duplicate SAFE_TO_CONTINUE, public media, exact UTM destination,
ACTIVE unexpired dispatch authorization, and no prior Pin/unknown/published
state are proven. The pilot flag and global publishing flag are temporarily
enabled by separate authorization, no autonomous worker or retry exists, and
incident/runbook evidence is ready. Gate B permits exactly one mocked-first,
then controlled live `POST /v5/pins`.

## Post-pilot shutdown

Immediately after the one attempt set `PUBLISHING_ENABLED=false`, disable the
pilot gate, disable the write-scope request gate, and run no worker or retry.
An already-granted provider token may retain `pins:write`; control that risk by
server-side revocation/reconnection, encrypted storage, least privilege, and
recorded operator audit. Requested OAuth scopes and already-granted persisted
scopes are distinct and must never be conflated.

## Automated test plan

Tests use mocked Pinterest traffic only and isolated databases. Cover default
read-only OAuth, conditional `pins:write`, impossible `boards:write`, both
pilot keys disabled, wrong publication/account/board, fingerprint and identity
mismatches, expired/consumed authorization, PUBLISHED and PUBLISH_UNKNOWN,
duplicates, missing scope, media failure, exact valid candidate with one
mocked call, second invocation with zero additional calls, parallel invocation
with at most one call, definitive versus ambiguous provider outcomes, safe
metadata, and no automatic retry. No test contacts `api.pinterest.com`.

## Migration decision

No database migration is required for Phase 0. Existing
`PublicationDispatchAuthorization`, `PublicationAttempt`, reconciliation, and
immutable publication snapshot records provide the required binding, TTL,
single-use, audit, and outcome durability. Alembic remains `0015`.

## Threat analysis and unresolved questions

Threats include browser-supplied trusted IDs, replay/double-submit, concurrent
claims, stale approvals, token leakage, ambiguous provider outcomes, and an
operator accidentally leaving a write flag enabled. Server-derived binding,
transactional CAS/uniqueness, encrypted credentials, allowlisted metadata,
two-key gating, and mandatory reconciliation mitigate these risks. Before any
future Gate A/B approval, confirm the exact Pinterest account identity,
provider Pin verification procedure, token revocation SLA, and operational
ownership for a one-Pin incident. These are unresolved design questions, not
permissions to execute them.

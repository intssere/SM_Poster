# Task #40 — Controlled Pinterest Write + Single-Pin Pilot Design

## Authorization boundary

**NOT AUTHORIZED FOR `pins:write`**  
**NOT AUTHORIZED FOR LIVE PIN WRITE**

This Phase 0 document is design-only. It does not request a write scope,
reconnect OAuth, create a Pin, or change runtime behavior.

## Certification status

PHASE 0 DESIGN = PASS
PHASE 1A DEFAULT-OFF IMPLEMENTATION = PASS
GATE A = NOT AUTHORIZED
GATE B = NOT AUTHORIZED

Certified branch head: `4580b5eadee9babf23aab5e574503c4ecb154e0a`.
Task #40 is not complete and live publishing is not authorized.

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

The pilot has two checks. A PRE-CLAIM check runs before token decryption where
practical, gateway construction, `atomic_authorized_claim`, authorization
consumption, publication transition, or attempt creation. A disabled or
mismatched pilot causes zero provider calls and leaves the publication
`SCHEDULED`. A POST-CLAIM recheck runs after the atomic claim and post-claim
validation, immediately before gateway construction and the network POST.

## Proposed two-key pilot gate

Add a server-only, default-false setting conceptually named
`PINTEREST_SINGLE_PIN_PILOT_ENABLED`. Provider execution requires both this
flag and `PUBLISHING_ENABLED=true`; neither is browser-controlled and the
global kill switch remains meaningful. The pilot flag is temporary, reviewed,
and removed/disabled after one attempt. No flag bypasses approval, quality,
duplicate, destination, media, authorization, or attempt checks.

## Exact candidate binding

Reuse the existing `PublicationDispatchAuthorization` snapshot binding. It
stores `publication_id`, `publication_fingerprint`, quality/readiness/duplicate
snapshots, actor, TTL, and single-use status; it has no dedicated connection,
board, creative, source-image, UTM, title, description, or media columns. Those
values are bound indirectly by the publication fingerprint and recomputation.
Add a server-only exact allowlist of pilot publication ID, publication
fingerprint, and `request_fingerprint_for(publication)`, empty by default. The
browser route ID is only an untrusted selector; every configured value must
match or the request makes zero mutation and zero provider calls. The
publication fingerprint binds draft/revision-or-original/creative/source image,
destination identities, destination URL and UTM; request fingerprint also
binds external board, title, description, alt text, UTM, and media URL.

## OAuth write-scope design (future Gate A only)

Define `READ_SCOPES = ("user_accounts:read", "boards:read", "pins:read")` and a
server-only `requested_scopes(settings)` helper. With the write gate false,
requested scopes are exactly READ_SCOPES; with Gate A true they add only
`pins:write`. `boards:write` is never allowed. Callback validation always
requires READ_SCOPES, persists actual granted scopes, and proves pins:write
only during Gate A. Refresh always requires READ_SCOPES and never
re-escalates. Requested scopes are distinct from already-granted persisted
scopes; disabling the request gate does not remove an existing grant.

## One-write and concurrency enforcement

For Gate B the candidate must have ZERO existing PublicationAttempt rows. Any
prior FAILED, UNKNOWN, SUCCEEDED, or persistence-uncertain attempt yields
`PILOT_ALREADY_ATTEMPTED` and zero provider calls. The pre-claim check enforces
this before authorization consumption. Existing transactional CAS ensures
concurrent first requests have at most one winner; the loser makes zero
provider calls. Rescheduling or reauthorization cannot create a second pilot
POST after the first attempt.

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
incident/runbook evidence is ready. Gate B authorizes exactly ONE live provider
create-Pin request. All mocked tests must already pass before Gate B; test
activity is not part of the Gate B authorization itself.

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

## Complete pre-provider checklist

Immediately before the provider POST, require: both runtime keys true; exact
configured pilot publication, publication fingerprint, and request fingerprint;
zero prior attempts before the first claim; valid unexpired authorization
consumed by the atomic claim; unchanged authorization snapshots; quality PASS;
duplicate SAFE_TO_CONTINUE; manual readiness; publication PUBLISHING and
attempt STARTED with the exact request fingerprint; exact CONNECTED connection;
matching board record/external ID that is active and eligible; persisted
`pins:write` with `boards:write` absent; public HTTPS media; provider title,
description, and alt limits; canonical destination and exact UTM; no Pin ID and
no PUBLISH_UNKNOWN blocker. Re-evaluate the exact pilot binding immediately
before the network call.

## Post-pilot token recommendation

Immediately set both execution keys false, disable write-scope requests, run no
worker, and perform no retry. Prefer revoking/reconnecting the token back to
read-only after evidence capture. Retain a write-scoped token temporarily only
with documented operational justification and every server execution gate
false; requested scopes and persisted grants remain distinct.

## Threat analysis

- Accidental broad publishing — exact candidate allowlist plus two-key gate.
- Wrong route ID — route ID is an untrusted selector and must match config.
- Pilot boolean without binding — empty/mismatched configured fingerprints fail.
- Scope escalation or Gate A flag changes — server-only settings, READ_SCOPES
  always required, `boards:write` impossible, no automatic re-escalation.
- Unexpected grants — persist actual scopes and verify explicitly at Gate A.
- Double click/parallel dispatch — single-use authorization, CAS claim, unique
  attempt, and zero-prior-attempt pilot rule.
- Retry after FAILED/UNKNOWN/persistence uncertainty — prior-attempt blocker
  and explicit reconciliation-only unknown state.
- Stale approval/creative/board/UTM/media — immutable snapshot and complete
  post-claim identity/readiness revalidation.
- Token/raw-body leakage — encrypted server credentials and allowlisted safe
  metadata only.
- Configuration left enabled — immediate shutdown procedure and two keys.
- Write-scoped token retained — preferred revoke/reconnect to read-only.
- Future autonomous worker — explicitly prohibited and requires a new task.

## Recommended Phase 1 implementation order

1. Add default-false configuration fields.
2. Separate `READ_SCOPES` from `requested_scopes`.
3. Implement the exact pilot-binding helper.
4. Add the pre-claim pilot guard.
5. Defer credential decryption and gateway construction.
6. Add the zero-prior-attempt hard blocker.
7. Preserve the atomic authorized claim.
8. Add the post-claim pilot-binding recheck.
9. Add comprehensive mocked tests.
10. Update the runbook and continuity documentation.
11. Run full regression certification.
12. Stop before Gate A; no live OAuth or provider write occurs in Phase 1.

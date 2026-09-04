# Task #40 Controlled Pinterest Pilot Runbook

## NOT YET AUTHORIZED FOR LIVE PIN WRITE

**GATE A NOT AUTHORIZED**  
**GATE B NOT AUTHORIZED**

This runbook describes the controlled procedure only. Merging code does not
authorize a live Pin.

## Purpose and protected defaults

The pilot prepares exactly one reviewed Pin while preserving human approval and
all Task #39 checks. Defaults are `PUBLISHING_ENABLED=false`,
`PINTEREST_WRITE_SCOPE_ENABLED=false`, and
`PINTEREST_SINGLE_PIN_PILOT_ENABLED=false`; candidate ID, publication and
request fingerprints are empty. OAuth requests only
`user_accounts:read boards:read pins:read` by default.

## Dispatch sequence and safety

Dispatch validates the active authorization and readiness, performs the exact
pre-claim pilot binding, checks provider readiness, retrieves/decrypts the
server-side token, constructs the gateway, atomically claims the authorization,
revalidates publication/auth/attempt state, performs the post-claim pilot
binding, and then calls the existing publisher/gateway boundary. There is no
parallel publisher, second route, frontend Publish Now action, autonomous
worker, or automatic retry. Any prior attempt blocks the pilot; only one first
provider attempt can be made.

## Gate A — write-scope enablement

Gate A requires explicit human authorization before temporarily setting
`PINTEREST_WRITE_SCOPE_ENABLED=true` or reconnecting OAuth. It authorizes only
requesting `pins:write`, reconnecting the intended business connection, and
verifying the granted scopes. It never authorizes `POST /v5/pins`.

### Gate A procedure

1. Identify the intended business connection and record safe identifiers only.
2. Verify it is connected and currently read-only.
3. Obtain explicit Gate A approval.
4. Enable only the write-scope request flag temporarily.
5. Start OAuth from the server-generated route; the browser supplies no scopes.
6. Request read scopes plus `pins:write`; never request `boards:write`.
7. Validate required read scopes and persist the actual encrypted credentials.
8. Verify persisted `pins:write`; do not dispatch a Pin.
9. Return the request flag to false and capture safe evidence.

Success requires all read scopes, actual `pins:write`, no `boards:write`,
encrypted tokens, no token exposure, no Pin, and protected flags still false.
Failures (denial, missing scopes, unexpected `boards:write`, callback or
refresh errors) mean STOP, restore the request flag to false, and do not enter
Gate B. If desired, reconnect read-only and verify the persisted write scope
is gone; no unverified revocation semantics are assumed.

## Gate B — exact one-Pin authorization

Gate B is separate and requires completed Gate A, actual write scope,
`boards:write` absent, one exact certified candidate, all Task #39 gates,
zero prior attempts, exact pilot bindings, active unexpired human
authorization, and reconciliation procedures. Only after explicit approval may
the existing manual dispatch call one `POST /v5/pins`; there is no retry,
bulk publishing, board write, update, or delete.

The candidate dossier may contain safe IDs/fingerprints, approval/revision/
creative/source identities, connection/board identities, approved copy and
URLs, quality/duplicate/readiness/authorization state, scope readiness, and
reconciliation history. Never include tokens, Authorization headers, client
secrets, OAuth code/state, encrypted ciphertext, or raw provider bodies.

## Outcomes and shutdown

Validated Pin ID yields `PUBLISHED`; definitive rejection yields
`PUBLISH_FAILED` and STOP; ambiguous/timeout/5xx or missing ID yields
`PUBLISH_UNKNOWN` and STOP for reconciliation. Local persistence uncertainty is
`PUBLISHED_STATE_PERSISTENCE_UNKNOWN` and also requires reconciliation.

## Required post-write verification

After a validated provider Pin ID is persisted, verify through an approved
Pinterest/provider read path if available: the ID matches, the live Pin exists
on the exact intended board, the approved creative/media and title/description
match the certified snapshot, alt text matches where exposed, the destination
and exact UTM URL are preserved, no duplicate Pin exists, the publication is
`PUBLISHED`, exactly one attempt exists, and safe audit evidence is captured.
If any verification is uncertain, STOP and do not perform a second write; use
the existing reconciliation procedure.

## Pilot evidence checklist

Before Gate A: protected defaults, intended business connection identified,
read-only current scope, and explicit Gate A approval. After Gate A: intended
connection verified, all read scopes present, actual `pins:write` persisted,
`boards:write` absent, request flag returned false, and no Pin created.
Before Gate B: complete candidate dossier, zero prior attempts, quality PASS,
`SAFE_TO_CONTINUE` duplicate result, exact board/account and creative/source,
exact destination/UTM, active unexpired authorization, exact pilot bindings,
and explicit Gate B approval. After one attempt: outcome and any Pin-ID/live
verification recorded, reconciliation status captured when needed, flags false,
bindings cleared, no second attempt, and no automatic retry. Never record token
values, ciphertext, Authorization headers, OAuth secrets, or raw provider
bodies as evidence.

After the first attempt, set publishing and pilot flags false, clear candidate
bindings, make no second Pin, and run no worker or retry. Preserve write scope
only if explicitly intended; otherwise use the documented read-only reconnect.

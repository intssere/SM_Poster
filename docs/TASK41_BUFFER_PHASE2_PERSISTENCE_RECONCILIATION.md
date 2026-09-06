# Task #41 Phase 2: persistence and explicit reconciliation

**BUFFER/PINTEREST LIVE WRITE NOT AUTHORIZED**

**NO postsWrite CREDENTIAL AUTHORIZED**

**PHASE 2 USES MOCKED MUTATIONS ONLY**

Implementation-review checkpoint, not release/live authorization. Branch:
`task-41-buffer-persistence-reconciliation-v1`, starting main
`9bdfe304f6f65f74606ec2fff674b59dd527d2cd`, tree
`94d2f8111ec792dd64ca50034a4a681c0c4f1221`. No production data/key was inspected.
Historical runtime counts and Phase 1 operator observations were not refreshed.

## Distinct identities and schema

Migration `0016` follows `0015`. PublicationAttempt adds `dispatch_provider`
(default/server default `pinterest_direct`), nullable `provider_operation_id`,
`provider_operation_status`, `provider_external_link`, `provider_submitted_at`,
and `provider_last_observed_at`. A partial unique index on provider + operation
ID prevents assignment of one non-null operation to multiple attempts. Null
operation IDs do not conflict. SQLite and PostgreSQL predicates are supplied.

A Buffer Post ID belongs only in `provider_operation_id`, never in
`PinPublication.pinterest_pin_id` or `PublicationAttempt.provider_pin_id`.
Those remain actual destination Pinterest Pin identities. Existing attempts
and reconciliation events retain their data and default to direct Pinterest.
Reconciliation events add provider/operation/status, not raw response data.
The new constrained audit action `PROVIDER_FAILURE_CONFIRMED` permits explicit
`PUBLISH_UNKNOWN -> PUBLISH_FAILED`; no new publication state or retry action
exists. Existing direct confirmation/cancellation transitions remain.

The narrow isolated migration test constructs the required tables with historical
0014/0015 migrations, stamps 0015 only after that schema exists, checks preserved
rows and unchanged unrelated tables, upgrades, checks uniqueness/constraints,
downgrades to 0015, and re-upgrades. No claim is made about the historical fresh
SQLite chain or live PostgreSQL. Downgrade refuses `BUFFER_HISTORY_PREVENTS_DOWNGRADE`
if Buffer attempts/operations or new failure events exist: it must not erase
provider evidence to fit the old schema. This guard needs operator-led review,
not automatic deletion or downgrade of a used production database.

## Internal dispatch and gates

`dispatch_buffer` is a service only. No API/provider selector/frontend integration
was added. Buffer credentials are not direct Pinterest OAuth credentials: the
service never decrypts Pinterest tokens and does not require granted pins:write.
Real Pinterest connection/board identity, approval, creative, media, quality,
duplicates and due schedule still participate in Task #39 snapshot validation.

Order:

1. Validate existing ACTIVE, unexpired Task #39 authorization and exact snapshot.
2. Require global publishing, Buffer publishing and Buffer pilot flags; all
   default false. Bind exact publication ID/publication fingerprint/request
   fingerprint, with zero prior attempts.
3. Validate server configuration, verify exact organization/channel/board using
   read-only gateway methods, then build the exact approved payload.
4. Reuse `atomic_authorized_claim(dispatch_provider="buffer")`: its existing
   compare-and-set consumes authorization, claims SCHEDULED to PUBLISHING and
   creates STARTED atomically. Direct callers still default to pinterest_direct.
5. Reload publication/authorization/attempt, validate the consumed Task #39
   binding and one owned STARTED Buffer attempt, repeat pilot and destination
   checks, reload/revalidate after I/O, rebuild the payload, then invoke one create.

Pre-claim failures leave ACTIVE authorization/SCHEDULED/no attempt/no mutation.
Post-claim pre-mutation failures leave CONSUMED authorization/FAILED attempt/
PUBLISH_FAILED with `BUFFER_POSTCLAIM_VALIDATION_FAILED`, zero mutations.
Prior-attempt gating prevents a second pilot mutation, including failed attempts.

Payload is exact description, title, UTM, board serviceId, approved public HTTPS
image and alt text. `automatic`, `shareNow`, **needsApproval=false** are explicit;
no draft/queue substitution. Public schema references for the fixed query:
[PostInput/reference](https://developers.buffer.com/reference.html),
[Pinterest metadata](https://developers.buffer.com/types/PinterestPostMetadata.html),
[image assets](https://developers.buffer.com/examples/get-posts-with-assets.html).
Public documentation access used no Buffer API credential or API request.

## Outcomes and recovery

| Observation | Persisted outcome |
| --- | --- |
| Definitive mutation rejection | FAILED / PUBLISH_FAILED / PROVIDER_REJECTED |
| Timeout, transport, 5xx, malformed/uncertain mutation | UNKNOWN / PUBLISH_UNKNOWN; no automatic retry |
| scheduled, sending, draft, needs_approval | operation ID persisted; UNKNOWN / PUBLISH_UNKNOWN / BUFFER_FINAL_OUTCOME_PENDING |
| create success with error status | operation persisted; FAILED / PUBLISH_FAILED / BUFFER_PROVIDER_FAILED |
| create success with sent status | one exact read verifies final evidence; otherwise BUFFER_SENT_LINK_UNVERIFIED and UNKNOWN |

Provider acceptance alone is never Pinterest success. The create result is first
persisted with its operation ID. A sent result gets one inline verification read,
not a poll/worker. Other pending observations require a later explicit invocation.

If result persistence fails after a known operation, rollback occurs before one
bounded recovery transaction preserving operation/status/safe link, marking
PUBLISH_UNKNOWN with `BUFFER_STATE_PERSISTENCE_UNKNOWN`. If recovery also fails,
`PublicationReconciliationError` is raised. The provider is never called again.
Uniqueness/state conflicts cannot silently overwrite another attempt's evidence.

## Explicit reconciliation

`reconcile_buffer` requires UNKNOWN, one relevant Buffer attempt, a persisted
operation ID, nonconflicting identities, unchanged request fingerprint and exact
server channel/organization matching the attempt's safe dispatch identifiers.
Missing ID returns `BUFFER_OPERATION_ID_REQUIRED` before HTTP; history scanning
is forbidden. `post(input: {id})` uses variables and is called once.

The gateway requires an exact returned ID, Pinterest channel service, allowlisted
status, timezone-aware timestamps, safe links, Pinterest metadata, and exactly
one ImageAsset. Reconciliation compares channel, text, board serviceId, title,
UTM URL, image source and alt text byte-for-byte. No URL/content normalization.
Malformed reads/timeouts/5xx preserve state with a bounded error; snapshot mismatch
raises `BUFFER_POST_SNAPSHOT_MISMATCH` without accepting the outcome.

Pending observations update only bounded observation data and remain UNKNOWN.
Exact sent evidence atomically sets actual Pin IDs/SUCCEEDED/PUBLISHED and adds
`PROVIDER_PIN_CONFIRMED`. Exact error evidence atomically sets FAILED/PUBLISH_FAILED
and adds `PROVIDER_FAILURE_CONFIRMED`, with `BUFFER_PROVIDER_FAILED_CONFIRMED`.
Publication and attempt compare-and-set updates plus the safe actor/reason/event
share one transaction. Commit failure rolls everything back and raises a typed
reconciliation error. Repeated terminal observations cannot add a second event.

The structural Pin-link parser accepts HTTPS pinterest.com/www.pinterest.com,
default HTTPS port, `/pin/<1-80 decimal digits>/` (optional final slash), no
query, fragment or userinfo. It makes no network request. This is an intentionally
strict fixture-certified format, not proof of a real delivered Pin. Phase 3 must
independently certify Buffer's observed externalLink format before live approval.

Multiple operation identities, mismatched known Pin IDs, or a Pin already assigned
to another publication/attempt fail closed. Existing manual CANCELLED_UNKNOWN and
manual Pin override cannot bypass exact reconciliation once a Buffer operation
is known. Direct Pinterest reconciliation without Buffer operation is unchanged.
UNKNOWN continues to block rescheduling and operational duplicates. No automatic
repair, write retry, or read retry exists.

## Protected defaults and Phase 3 prerequisites

PUBLISHING_ENABLED=false; BUFFER_PUBLISHING_ENABLED=false;
BUFFER_SINGLE_PIN_PILOT_ENABLED=false; PINTEREST_WRITE_SCOPE_ENABLED=false;
PINTEREST_SINGLE_PIN_PILOT_ENABLED=false. Buffer candidate ID/publication
fingerprint/request fingerprint defaults are empty; direct candidates untouched.
Pinterest READ_SCOPES remain user_accounts:read, boards:read, pins:read.
No pins:write or boards:write request; Gate A and Gate B remain unauthorized.

No postsWrite credential, real Buffer read/write/draft, Pinterest/OpenAI request,
frontend change, authenticated route, worker, polling, or autonomous dispatch.
Tests use fake keys and MockTransport. No raw provider body/message, token or
header is persisted. Only safe channel/organization identifiers are added to
attempt metadata; existing DTO allowlisting remains unchanged.

Phase 3 requires independent review, explicit credential/write authorization,
exact observed provider-format certification, a valid human-approved candidate,
media fetchability/provenance proof, migration deployment review, incident/runbook
and unknown-outcome handling, and explicit one-Pin approval. None is granted here.

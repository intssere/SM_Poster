# Task #41: Buffer Pinterest publisher adapter v1

**BUFFER/PINTEREST LIVE WRITE NOT AUTHORIZED**

**PHASE 1 — MOCKED WRITES ONLY**

## Baseline and purpose

Phase 1 was merged in PR #27 at `9bdfe304f6f65f74606ec2fff674b59dd527d2cd`
(tree `94d2f8111ec792dd64ca50034a4a681c0c4f1221`). The account below is
historical Phase 1 scope. Phase 2 adds internal, default-off persistence and
manual reconciliation; see [Phase 2 design](TASK41_BUFFER_PHASE2_PERSISTENCE_RECONCILIATION.md).
It remains unstaged for independent review and does not authorize live writes.

This unstaged Phase 1 implementation starts from main
`0080b733a006ae7916ea9346cb2bdce554cb9caa`, tree
`6f6f6ddb478b3df66ee4877e5c680f79df5cfeae`, on
`task-41-buffer-publisher-adapter-v1`. Alembic stays `0015`.
The release controller waived the fresh fetch for this run after independently
verifying the same GitHub commit/tree. Direct Pinterest developer production
access remains blocked pending the existing appeal; direct Pinterest remains
the preferred long-term integration. Buffer is an additional isolated provider
foundation, not a replacement inside Task #40 dispatch.

## Carried-forward read-only preflight

The operator supplied successful read-only preflight evidence: organization
`6a9d1addff025abd45d16723` (My organization), Pinterest channel
`6a9d1bdbcd8b9c702c1706c8` (diamondshelfllc), connected, unlocked and not
queue-paused, with 11 boards. These IDs are configuration, not credentials.
They are not hardcoded into application logic. This implementation does not
repeat that authenticated preflight or freshly query production/runtime data.

The current key in Replit Secrets is read-only. It must not be inspected,
printed, copied, or exercised to discover whether a mutation works. No
postsWrite key is requested or created. A draft is also an unauthorized write.

Pinterest board `serviceId` values are Pinterest board IDs. Verification checks
the configured organization's membership, the exact configured channel within
that organization, Pinterest service, connected/unlocked state, and exact
snapshot board serviceId. Names and fuzzy matching cannot authorize a board.
Returned identifiers are not human approval, duplicate clearance, or dispatch
authorization. The empty Buffer-known sent history is **not** evidence that
Pinterest has no existing Pins; existing duplicate protections remain required.

## Settings and boundaries

| Setting | Default |
| --- | --- |
| BUFFER_API_KEY | absent; excluded from Settings repr/export |
| BUFFER_API_BASE | https://api.buffer.com |
| BUFFER_ORGANIZATION_ID | absent |
| BUFFER_PINTEREST_CHANNEL_ID | absent |
| BUFFER_PUBLISHING_ENABLED | false |

The gateway accepts server Settings and an optional injected AsyncClient.
Only the official HTTPS origin is accepted for the configured base (an optional
trailing slash is allowed), so an accidental alternate base cannot receive the
key. Redirects are not followed. Timeouts are connect 5s, read 20s, write 10s,
pool 5s. The owned client does not use environment proxy configuration. There
are no retries; injected clients must also have retry-free transports.

Read queries expose allowlisted organization/channel/board fields and a single
recent-post page (1–100 items, default 20), filtered by exact organization,
channel and status, ordered newest-created first. There is no polling or cache.
Read failures raise bounded BufferReadError and never create attempts or change
publication state. A bounded page is not complete history.

`create_pinterest_post` requires both `PUBLISHING_ENABLED=true` and
`BUFFER_PUBLISHING_ENABLED=true` before any HTTP request. Both are false in
the protected defaults; enabled values exist only in isolated mocked tests.
This is a low-level provider foundation, **not** an approved execution path:
human approval, duplicate checks, claim/reconciliation and destination
revalidation must be integrated in a separately reviewed Phase 2. No current
route, manual dispatch, publisher, worker or frontend calls this gateway.

## Immutable payload mapping

| Buffer input | Source |
| --- | --- |
| channelId | server buffer_pinterest_channel_id |
| text | publication.description_snapshot |
| metadata.pinterest.boardServiceId | publication.pinterest_board_id_snapshot |
| metadata.pinterest.title | publication.title_snapshot |
| metadata.pinterest.url | publication.utm_url |
| assets[0].image.url | publication.media_url_snapshot |
| assets[0].image.metadata.altText | publication.alt_text_snapshot |
| schedulingType | automatic |
| mode | shareNow |
| needsApproval | false (explicit required Boolean; corrected in Phase 2) |

The builder is pure and neither queries nor mutates the database. Missing or
blank values fail; content is never truncated, rewritten, regenerated, or
silently replaced. Exactly one image is sent. UTM target must match immutable
destination scheme/host/effective port/path, non-UTM query parameters and
fragment. The original UTM URL is retained. HTTPS default port equivalence is
structural only; malformed ports fail closed.

Media must be an HTTPS public-host candidate: local/reserved names, private
addresses, malformed ports and userinfo are rejected. This does not perform
DNS resolution or prove public fetchability; later operational certification
must verify stable public media without changing its approved provenance.

The fixed GraphQL document uses variables; content is never interpolated into
GraphQL source. It requests PostActionSuccess and MutationError fragments.
MutationError is an interface; its message is aliased to errorMessage to detect
the error branch without relying on a concrete error typename. Raw messages
are discarded. This contract was checked against the public
[Buffer reference](https://developers.buffer.com/reference.html) and
[image-post example](https://developers.buffer.com/examples/create-image-post.html),
not by contacting the authenticated Buffer API.

## Outcomes and future persistence

Success is normalized to buffer_post_id, status, channel_id, created_at, due_at,
sent_at and external_link only. A nonempty validated ID and matching channel
are mandatory. Pending/sending success may lack externalLink. No full response,
provider message, credentials or arbitrary metadata is returned or persisted.

An explicit MutationError without success is definitive rejection. HTTP
400/401/403 is treated as definitive only with a recognized parse/validation/
authentication/permission GraphQL rejection and no data. Other client errors
remain uncertain. Timeout, transport/reset, 5xx, redirects, invalid JSON,
top-level GraphQL errors, malformed success/missing ID, or conflicting results
are BufferAmbiguousFailure. No failure is automatically retried. No provider
outcome changes a local publication in Phase 1.

A Buffer post ID is **not** a Pinterest Pin ID. Do not put it in
PinPublication.pinterest_pin_id or PublicationAttempt.provider_pin_id.
Phase 2 requires independent design/review of generic provider identity,
pending-vs-sent semantics, durable attempts, provider-success/DB-failure
reconciliation, and uncertain-outcome handling. Reuse Task #39 authorization,
exact approved snapshot/provenance, duplicate safeguards and explicit operator
confirmation; do not build a parallel approval system. Empty Buffer history
must never clear duplicate protection. Buffer schedulingType automatic means
Buffer would handle delivery in a future approved call; no autonomous local
worker or live scheduling is introduced here.

## Protected state and rollback

PUBLISHING_ENABLED, PINTEREST_WRITE_SCOPE_ENABLED,
PINTEREST_SINGLE_PIN_PILOT_ENABLED, and BUFFER_PUBLISHING_ENABLED remain false.
All three Task #40 candidate bindings remain empty. Default requested scopes
remain exactly user_accounts:read, boards:read, pins:read. No pins:write or
boards:write request is authorized. Gate A and Gate B remain NOT AUTHORIZED.
No live Buffer mutation, Pinterest request, OpenAI call, frontend change,
migration, automatic retry, worker or provider-ID persistence is part of this
phase. Existing Task #34–40 execution code remains untouched.

Rollback must keep both publishing flags false and remove/disable any future
integration before reverting adapter code. Do not retry an uncertain outcome
as part of rollback. If a key is exposed, revoke/rotate it securely through the
operator-controlled secrets workflow; never paste it into logs, source, tests,
docs or browser storage. This phase performs no secret rotation itself.

## Verification and review checkpoint

Tests use httpx.MockTransport and fake fixture keys exclusively. Required
verification: both Buffer suites; existing Pinterest gateway/publisher/manual
dispatch/pilot/authorization regressions; full backend; compile/import; Alembic
head 0015; and diff scope/whitespace checks. Exact run counts are reported with
the implementation handoff, not claimed as deployed runtime evidence.

Stop with unstaged local files for independent release-controller review.
No staging, commit, push, PR or GitHub issue is authorized in this pass.

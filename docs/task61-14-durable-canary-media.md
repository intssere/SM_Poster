# Task 61.14: durable canary media

## Purpose and boundary

Extend Task 61.6A's provider-free, one-item staged-media fixture so its
rendered creative can be verified and served after the application process or
instance that rendered it has disappeared. Keep the existing `PNGMediaStorage`
abstraction and its digest-addressed object keys. In an exposed runtime, the
final artifact must reside in the configured durable object store; a file in
the renderer's temporary directory is staging material, not published media.

This is a code and isolated-CI change only. It does not enable a scheduler,
worker, publishing gate, provider, or canary. It does not authorize an object
store upload, production database/media change, deployment, or fixture run.
Alembic revision `0031` remains the schema head; protocol and receipt metadata
belong in the creative's existing `render_spec`.

## Required promotion and receipt contract

Keep the source boundary from Task 61.6A: the caller supplies authentic local
source bytes and their selected, editorially eligible source-image identity.
Verify those bytes against the already-persisted source-image SHA-256; never
fetch a source URL. Render and validate the PNG locally, calculate its digest,
and stage it locally before attempting final promotion.

For an exposed runtime:

1. Bind a pending receipt to the fixture protocol version, creative identity,
   source-image identity and source digest, generation/input fingerprint,
   immutable `render_spec` fingerprint, rendered PNG digest, expected durable
   storage backend/protocol, and the exact `PNGMediaStorage` key independently
   recomputed for that creative and digest. Treat `render_spec` as immutable:
   any receipt/provenance change that alters its fingerprint invalidates
   reconciliation rather than silently rebinding the artifact.
2. Persist enough pending staged state to recover a transaction interrupted
   around promotion. Local staged bytes are not a successful final artifact.
3. Write the PNG through `PNGMediaStorage` to its digest-addressed object key.
   Read it back through that abstraction and require valid PNG bytes and an
   exact SHA-256 match **before** finalizing the creative as `RENDERED` or
   allowing its media into public output/admission.
4. Bind the finalized receipt to the verified durable protocol/key and the
   same creative, source, digest, and input provenance. Recompute and validate
   these bindings on reconciliation and reads; derive the key with the
   canonical `media_key` function instead of trusting persisted receipt text.
   Do not trust a path, URL, key, or digest merely because it appears in
   persisted JSON.

Promotion recovery is idempotent. If an object already exists at the exact
expected key, read and fully verify it, then reconcile the pending creative
and its existing fixture rows without duplicating business rows or creating a
second semantic fixture. A missing object may be promoted from the still-valid
pending stage. A wrong key, stale or mismatched receipt, unavailable backend,
missing object after write, invalid PNG, digest mismatch, or interrupted
finalization must fail closed; none makes the creative admissible. Remove
unreferenced local stage files safely **only after** the durable promotion
receipt has committed; preserve stages referenced by a recoverable pending
receipt.

Development/test may retain Task 61.6A's local-filesystem final-media behavior
where appropriate. That behavior is not a fallback for an exposed runtime:
an exposed runtime must select and verify the durable storage abstraction, and
a local-only receipt or local final file can never prove production media. An
exposed-runtime storage factory must also reject an explicitly injected
`LocalStorage` backend, not just avoid selecting it by default.

## Public reads and admission

In exposed runtimes, the public creative-media route, URL verification, and
canary admission must verify the same persisted durable receipt and read bytes
through the configured `PNGMediaStorage` backend. Verify PNG signature and
content digest on every media read. A local file, a syntactically valid public
URL, or a matching database digest alone is insufficient. A missing,
unavailable, corrupt, wrongly keyed, or provenance-mismatched object denies
the media response and admission; never fall back to node-local final bytes.

The exposed and development paths must remain distinct in both implementation
and tests. Do not weaken Task 61.6A source-byte verification, safety gates,
single-item scope, transaction recovery, or the existing admission protection
for pending staged creatives.

## Regression coverage and CI

The Task 61.14 CI workflow runs only repository tests with an in-memory
durable backend or isolated local fixtures. The global `tests/conftest.py`
SDK sentinel must run before application/storage test imports and reject real
`Client` construction and SDK I/O, including read, exists, upload, and
download methods. It is defense in depth even if unexpected credentials are
present in the environment. CI also scrubs hosted-storage and provider
credentials from later test steps. The conftest socket guard permits only
loopback and Unix-socket access, so unexpected code cannot call a remote
provider or media endpoint; fail test setup if either guard is unavailable.
PostgreSQL-dependent Task 59.5, Task 59.6, and Task 61.1 tests use a disposable
job-scoped PostgreSQL service through explicit test-only URLs, while
`DATABASE_URL` stays in-memory SQLite. Task 61.6A tests initialize disposable
local PostgreSQL clusters. No job attaches Replit Object Storage, requires
production secrets, performs a real provider request, or runs the
fixture/canary.

Required focused cases include successful local-stage → durable write →
read-back verification; reading in a fresh instance with no final local file;
idempotent recovery of an already-present object; storage unavailable,
missing-object, corrupt-object, and digest-mismatch failures; interrupted
database finalization and safe retry; orphan stage cleanup; stale receipt,
wrong key/protocol, and wrong creative/source/input fingerprint; public-media
mismatch; admission blocked until durable verification succeeds; exposed
runtime rejection of local-only receipts; and continued development
filesystem-media behavior.

The pull-request workflow also runs the Task 61.6A PostgreSQL/filesystem
fixture and local-media/admission tests, all nine Task 61.1 one-shot scheduler
canary/readiness/schema-guard suites, Task 59.5 and 59.6 offline certification
regressions, and related storage, proposal, and publication compatibility
tests. Test names that include "live" exercise mocked/offline certification
logic; they do not authorize or make live calls.

Passing isolated tests demonstrates the tested code paths and backend
contract only. It does **not** prove production storage attachment,
permissions, availability, existing source-image hash readiness, successful
production promotion, deployed public-media reachability, or production
canary readiness. Any future production source qualification, object write,
or fixture execution requires its own explicit authorization.

## Storage-effect incident note

An unsanitized test in this engineering session reached a real SDK path before
the sentinel was in place. Do not claim that the session caused zero object
storage writes, and do not infer an object-store effect in either direction
from test intent alone. The main agent is investigating recorded logs only;
that investigation must not read, list, probe, or otherwise access object
storage.

## Local engineering verification

The final isolated regression evidence covers **596 distinct passing cases in
41 suites**, with **zero unresolved failures, errors, or skips**. This includes
**44 new durable-media, consumer, real-fixture recovery, and isolation cases**.
The comprehensive run's 537 passing non-durable-fixture cases were retained;
the corrected exposed-runtime test harness and added generation/execution
coverage then passed 59 cases (11 durable PostgreSQL fixture cases and 48
generation/execution cases). This is aggregate evidence across final runs,
not a claim that an earlier failing run passed. Syntax compilation and
`git diff --check` also passed. GitHub CI remains a separate verification.
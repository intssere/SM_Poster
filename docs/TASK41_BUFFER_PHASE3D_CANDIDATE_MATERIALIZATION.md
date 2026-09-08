# Task #41 Phase 3D — Buffer pilot candidate materialization

This phase creates only an offline structural foundation. `PUBLISHING_ENABLED`,
Buffer pilot flags and Pinterest write flags remain false. No Buffer/Pinterest
request, live media fetch, approval, publication, authorization, board binding
or provider mutation is performed against runtime data. Issue #23 remains open.

Buffer uses the existing generic `Board` as the editorial destination. Structural
readiness requires an active board whose persisted `pinterest_board_id` exactly
matches the immutable publication snapshot, the proposal concept’s board and
store, and no direct Pinterest connection or board record. Direct Pinterest is
the default provider mode and retains its existing connection/board checks.

Authorization snapshots carry the provider mode through creation, validation and
post-claim checks, preventing cross-provider reuse. Buffer destination binding is
an explicit, offline evidence service: evidence must be fresh, exact-match the
configured organization/channel, prove the bounded board service ID and contain
no secrets. It writes only the generic board ID plus a safe audit event; conflicts
are refused and exact repeat binding is idempotent.

Rendered creatives may derive an immutable public URL only from a validated
HTTPS `PUBLIC_MEDIA_BASE_URL`, creative ID and persisted SHA-256. The exact
anonymous GET/HEAD route verifies render state, digest, file bytes and immutable
cache semantics. Other protected APIs remain authenticated; no network fetch or
regeneration is performed. Publication snapshots derive media server-side and
never accept a caller-supplied URL.

Alembic remains at `0016`; no migration is part of this phase.

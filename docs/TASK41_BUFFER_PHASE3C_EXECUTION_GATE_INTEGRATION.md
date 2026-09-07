# Task #41 Phase 3C — Buffer execution-gate integration

LIVE BUFFER/PINTEREST WRITE NOT AUTHORIZED.
NO postsWrite CREDENTIAL AUTHORIZED. NO LIVE PIN AUTHORIZED.
Task #40 / Issue #23 remains open.

This offline integration requires explicit `BufferPilotExecutionEvidence` from
the caller of `dispatch_buffer`. Missing, stale, malformed or mismatched evidence,
disabled gates, invalid persisted candidates and unavailable isolated reads lock
execution with `BUFFER_EXECUTION_GATE_LOCKED` before gateway construction,
provider access or an atomic claim. Evidence is neither synthesized nor persisted.
The exception contains only the bounded code, not evidence or credentials.

Execution order:

1. Explicit caller evidence and existing local authorization/pilot validation.
2. Phase 3B fresh persisted execution readiness, under no-autoflush.
3. If and only if `FINAL_EXECUTION_READY`, existing configuration validation and
   provider preclaim verification.
4. Atomic authorized claim.
5. Existing postclaim validation and final provider destination verification.
6. Existing final DB/pilot checks and one createPost mutation.
7. Existing reconciliation and no-retry handling.

The Phase 3B evaluator runs only before claim. Its pre-attempt contract must not
be applied to the owned STARTED attempt. Existing postclaim guards remain intact.
`FINAL_EXECUTION_READY` is a technical readiness result, NOT itself authorization
to publish. Separate release authorization is required for all live provider access.

Protected defaults remain `PUBLISHING_ENABLED=false`,
`BUFFER_PUBLISHING_ENABLED=false`, and `BUFFER_SINGLE_PIN_PILOT_ENABLED=false`.
No configuration, migrations, API, frontend, worker or direct Pinterest behavior
is changed. No automatic retry is added.

Acceptance tests use committed file-backed SQLite candidates so the real Phase 3B
gate can independently inspect persisted state. Drift is committed by a second
session while the caller retains cached rows. Blocked cases assert unchanged
persisted publication/authorization rows, zero attempts and zero provider/claim
boundary calls. The valid hypothetical case checks gate/gateway/claim ordering
and preserves the uncertain-outcome no-retry rule with a fake provider.

The existing Phase 2 test fixture now uses file-backed SQLite and supplies
explicit hypothetical evidence to the dispatch invocation. Its mocked HTTP
transport and original outcome/reconciliation assertions remain in place.

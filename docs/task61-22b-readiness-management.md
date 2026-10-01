# Durable readiness management — engineering only

This change does not authorize migration application, configuration changes,
Publish, execution of the readiness probe, or any business/provider action.
The original probe is unchanged. Its original endpoint prohibition is superseded
only for this specifically authorized, default-disabled management seam.

## Admission and schema contract

Migration `0032` follows `0031` and creates only the PostgreSQL
`public.management_readiness_admissions` table and two trigger functions.
The table uses separate SQLAlchemy Core metadata, not business ORM metadata;
Alembic nevertheless includes that metadata for schema discovery. There are no
business foreign keys, business DML, provider imports or business lifecycle calls.

The primary key is `(operation, release_commit_sha, release_tree_sha)`.
Grant IDs are globally unique. An atomic conflict-safe insertion commits before
the caller may spawn work. A different nonce, process, instance, restart or HTTP
retry cannot rearm the same release scope. Different releases are distinct scopes
and require separately approved release pins and signed authorization; this is
not a global prohibition against every future separately authorized probe.

Consumption is permanent within the scope. Rows cannot be deleted, truncated,
reset or rebound by ordinary DML; an ADMITTED row may transition once to a
terminal outcome, which cannot subsequently be changed. Downgrade refuses any
evidence. A pre-existing target table is refused, not silently adopted.

The production schema guard now requires reviewed revision `0032`.
Startup does not apply this migration. It verifies an already-recorded `0032`;
an existing `0031` database remains blocked by the new production guard until a
separately authorized migration/deployment process is completed. Historical
`0031` bookkeeping-adoption behavior remains available and explicitly tested.

## Protected operations

```
POST /api/internal/operations/object-storage-readiness
GET  /api/internal/operations/object-storage-readiness
```

Both operations require a genuinely verified configured admin session and one
allowlisted Origin header. AUTH_DISABLED is never sufficient, even locally.
The path is authenticated management routing, not private network ingress.
Neither operation is called by health, startup, scheduler, worker or a UI.
Responses have `Cache-Control: no-store`.

POST accepts **no body and no query parameters**. Its only execution inputs are:

- `X-Readiness-Confirmation:
  EXECUTE-ONE-OBJECT-STORAGE-READINESS-PROBE-V1`
- `X-Readiness-Authorization`: an opaque, signed, purpose-separated management
  grant issued with the existing session signing key.

The offline `issue_authorization` helper is not an HTTP endpoint. It requires
explicit configured release pins and creates a 180-second grant bound to the
admin actor hash, operation and complete server-derived descriptor. Session
tokens and management grants are not interchangeable. No actual grant was issued
using workspace credentials for this engineering task.

GET reads evidence for the currently configured descriptor without consuming
admission or relaunching. It remains available after closing the management
gate, subject to authentication, release/runtime and closed-state checks.

HTTP outcomes: 200 for PASS; 502 for a validated failed/blocked probe; 503 for an
unknown outcome or unavailable prerequisite; 409 for consumed admission; 401/403
for authentication/authorization refusal; 400 for prohibited request inputs.
Pre-admission refusals contain fixed error codes, never fabricated probe receipts.

## Default-closed runtime prerequisites

No configuration values are set by this change.

- `OBJECT_STORAGE_READINESS_MANAGEMENT_ENABLED` defaults to the exact string
  `false`; only exact `true` permits POST.
- `OBJECT_STORAGE_READINESS_PROBE_ENABLED` must remain absent/`false` in the
  application process. No application Settings field is added for that probe gate.
- All operational `_enabled` settings remain false; dry-run is true, batch and
  daily write limits are one; routine control is PAUSED, the local scheduler is
  stopped, and active/uncertain business work is absent.
- A published-runtime marker is required, but is not independent certification.
- The reviewed `0032` schema must match the frozen contract, including indexes,
  constraints, enabled triggers and exact trigger-function properties/bodies.

These configuration pins default empty and must be independently approved in a
future task:

```
READINESS_EXPECTED_CANONICAL_COMMIT_SHA
READINESS_EXPECTED_CANONICAL_TREE_SHA
READINESS_EXPECTED_RELEASE_COMMIT_SHA
READINESS_EXPECTED_RELEASE_TREE_SHA
READINESS_EXPECTED_OVERLAY_SHA256
READINESS_EXPECTED_PROBE_SHA256
READINESS_EXPECTED_TOPOLOGY
```

The descriptor also fixes probe version, schema revision, namespace, size and
payload digest. The process compares its manifest against the approved pins and
hashes its actual `.replit` overlay and fixed probe file. Manifest validity does
not prove live Git ancestry or independent platform release certification.
Adding this seam changes source identity; the old certified release cannot be
claimed unchanged.

## Child execution and evidence

After authentication and preconditions, the orchestration thread rechecks
authorization expiry, consumes admission, then rechecks expiry and preconditions
before launching. PostgreSQL also checks expiry immediately before insertion.
Expiry after admission preserves consumption and produces UNKNOWN without launch.

The runner invokes only the fixed interpreter/script with no CLI arguments and
no shell. It changes the probe gate only in a copied child environment. It never
sets the parent environment or persisted configuration. The CLI's existing signal
handlers and exit-code contract remain intact.

Stdout is bounded to 64 KiB, stderr discarded, and only a strict single JSON
receipt is retained. The runner has a fixed 90-second deadline; timeout requests
SIGTERM, then SIGKILL only if necessary after a short grace period. A timeout
is UNKNOWN, never an assertion of clean storage.

PASS requires a valid success receipt, exit zero, every stage passing, official
SDK evidence, distinct clients, upload/delete acknowledgement and confirmed
normal-sequence cleanup. Persistence and retrieval independently reject missing
or inconsistent success evidence. Valid failed/blocked receipts preserve actual
exit codes. Unknown outcomes may retain a valid successful probe receipt when
the overall business-baseline comparison failed; that is not an overall PASS.

The wrapper reads business baseline counters before and after the probe but
never writes those tables. The standalone probe still imports no application,
database or business/provider clients.

Logs contain fixed lifecycle/outcome codes, not cookies, grants, actor hashes,
environment dumps, bucket identifiers, raw child output or raw exceptions.

## Isolation and CI

Focused local/CI command:

```
python scripts/run_isolated_readiness_tests.py --with-postgres -q \
  tests/test_object_storage_readiness_management.py \
  tests/test_readiness_execution_runner.py \
  tests/test_readiness_execution_admission_0032.py \
  tests/test_readiness_execution_orchestration.py
```

The launcher copies only PATH/HOME/USER/LANG into a fresh pytest environment,
uses synthetic SQLite for application imports, refuses dotenv files, blocks
external network access and real SDK construction before collection, and creates
a disposable Unix-socket-only PostgreSQL cluster. It never uses an attached DB
URL. Enabled probe processes are never launched by tests; runner tests use fake
processes, and probe tests use injected in-memory SDK clients.

CI runs required PostgreSQL tests without skips on supported Python 3.12 and
relevant existing authentication, migration, media-isolation and startup suites.
Hosted CI execution is separate from local execution; defining a workflow does
not establish that GitHub ran it.

## Final local verification

- Focused management/authentication, runner, PostgreSQL admission/migration and
  failure-boundary suite: **149 passed**, no skips or failures (82.55 seconds).
- Relevant existing authentication, standalone probe, startup, schema adoption/
  repair, lineage, media-storage and isolation suite: **200 passed**, plus
  **28 subtests passed**, no skips or failures (117.48 seconds).
- Real PostgreSQL tests include clean upgrade to `0032`, additive `0031`→`0032`
  with unchanged business row counts, exact frozen catalog checks, concurrency
  across engines/processes/apps, crash/response-loss boundaries, unavailable or
  ambiguous coordination, evidence-preserving downgrade refusal and empty
  downgrade/restore.
- Python compilation and patch whitespace checks pass. The original probe has
  no source diff.
- Local `uv pip check` reports the pre-existing SDK/Python support mismatch
  documented below. `python -m pip check` is unavailable in this workspace
  because pip is not installed. The CI dependency check is configured for Python
  3.12 but hosted CI has not been invoked by this task.
- Independent static review: PASS after the authorization-deadline and absent-
  receipt persistence findings were corrected and covered by regressions.

## Explicit limitations

- At-most-once admission/launch is not guaranteed completion. A crash after
  consumption but before spawn can result in zero launches. No automatic retry
  is permitted. ADMITTED without terminal evidence is returned as UNKNOWN.
- Lost responses and coordinator/outcome-persistence errors never reset admission.
- SDK hangs, SIGKILL, runtime termination and late unacknowledged uploads cannot
  guarantee cleanup. Existing bucket-identity and collision limitations remain.
- Local closed-state checks are not independent fleet-wide quiescence proof.
  Future operators must establish consistent closed configuration across instances.
- The guarantees assume one authoritative PostgreSQL history and an immutable,
  independently approved deployment. DB-owner DDL, disabling triggers, changing
  signing keys/configuration, or restoring a pre-consumption backup are privileged
  operations outside this admission contract; they require separate reconciliation
  and must never be used as an automatic retry mechanism.
- Production migration, release certification, configuration activation, Publish
  and live invocation remain unauthorized by this engineering task.
- The current workspace interpreter is Python 3.13, outside the installed Object
  Storage SDK's declared `<3.13` support. Offline fake-SDK tests can run there,
  but this is not SDK/runtime certification. CI uses Python 3.12; no workspace
  toolchain or package versions are changed by this task.
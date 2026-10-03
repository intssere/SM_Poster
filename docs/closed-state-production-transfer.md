# Closed-state Replit 0031 → Railway 0034 state transfer

This is **engineering tooling, not an executed production migration**. Nothing
calls it from startup, Alembic, deployments, API routes, workers or schedulers.
Running against either real database requires separate operator authorization.
Do not merge/deploy this branch as a substitute for a transfer authorization.

## Scope and fixed policy

`backend/app/state_transfer/policy.py` explicitly enumerates all 48 application
tables from the read-only 2026-10-03 inventory, their exact source counts,
classification and preserve/exclude action. Total source rows: **12,459**.
Exactly **12,454** rows are transferable; five OAuth handshake rows are excluded.

- Preserve all business/history records, including cancelled publications,
  historical UNKNOWN attempts, consumed/expired permissions, provider operation
  identity, reconciliation evidence and PAUSED control state.
- Carry Pinterest ciphertext as opaque text. Never decrypt, refresh, re-encrypt
  or print it. The export is still **sensitive**, not an anonymized report.
- Do not operationally transfer `pinterest_oauth_states`; its count and source
  content digest are recorded, but its rows are not in the bundle.
- Never copy/stamp `public.alembic_version`, `_system.*` or its sequence.
- Target-only tables must exist canonically and stay empty:
  `management_readiness_admissions` (0032, changed by 0033),
  `management_readiness_outcomes` (0033),
  `routine_autonomous_batches` and `routine_autonomous_batch_entries` (0034).
  No receipts, permits, batch manifests or admissions are manufactured.

There is no allow-drift flag. Different counts, tables, columns, enum labels,
constraints, indexes, triggers/functions, row-security flags or revisions refuse.
Counts must still match the reviewed inventory; changed production data needs a
separate read-only inventory/contract review and a reviewed policy update.
Catalog hashes were frozen from a disposable, migration-created 0031 schema,
with the eight inventoried managed-source foundational FKs added explicitly,
not generated from whichever source happens to be connected at execution time.
The existing canonicality guard also verifies all revision-specific contracts.

The source and fresh target **do not have identical foundational FK catalogs**:
historical Phase 0 column cloning drops eight FKs on products, creatives,
approvals and publications. `FOUNDATION_FKS` pins the exact source inventory
keys; source hashes require them, and the target override hashes require the
unchanged canonical fresh target. The transfer supplements target FK ordering
and reference validation with those eight source edges. It does not add target
constraints or change historical migrations. Future target writes are still
governed by its existing canonical constraints, not by this one-time tool.

## CLI

Run from the repository root with the backend requirements installed:

```text
python scripts/transfer_production_state.py export \
  --dsn-file /secure/source-dsn --bundle /secure/state-bundle.json --dry-run

python scripts/transfer_production_state.py export \
  --dsn-file /secure/source-dsn --bundle /secure/state-bundle.json

python scripts/transfer_production_state.py plan \
  --bundle /secure/state-bundle.json --expected-manifest-sha256 <reviewed-digest>

python scripts/transfer_production_state.py plan \
  --dsn-file /secure/target-dsn --bundle /secure/state-bundle.json \
  --expected-manifest-sha256 <reviewed-digest>

python scripts/transfer_production_state.py import --dry-run \
  --dsn-file /secure/target-dsn --bundle /secure/state-bundle.json \
  --expected-manifest-sha256 <reviewed-digest>

python scripts/transfer_production_state.py import \
  --dsn-file /secure/target-dsn --bundle /secure/state-bundle.json \
  --expected-manifest-sha256 <reviewed-digest>

python scripts/transfer_production_state.py certify \
  --dsn-file /secure/target-dsn --bundle /secure/state-bundle.json \
  --expected-manifest-sha256 <reviewed-digest>
```

DSN files are explicit operator inputs; there is **no DATABASE_URL, dotenv,
credential, application-settings or attached-database fallback**. Protect DSN
files and bundles with owner-only permissions and secure transfer/storage.
Supply a SQLAlchemy PostgreSQL URL using an installed PostgreSQL driver.
Host (or explicit Unix socket), database and username must all be explicit.
Never put a DSN or ciphertext into command-line arguments, tickets, git or logs.

Output contains only fixed metadata, counts, dependency order and SHA-256
digests. It never includes rows, ciphertext, account profiles, DSNs or media URLs.
`export --dry-run` performs the read-only source checks but creates no file.
Offline `plan` verifies the bundle without opening any database. Target plan and
import dry-run additionally perform read-only target preflight; neither writes.
Refusal exits **2**, success exits **0**; driver exceptions are sanitized.
The CLI suppresses SQL/driver logging, and uses hidden SQL parameters.
Operators must also protect database-side logs; this tool does not alter server
logging configuration or claim control over external database log collectors.

The actual bundle is exclusively created with mode **0600**, fsynced, never
overwritten; a failed write removes the partial file. Do not print/read the
bundle with a generic command that dumps its rows. Its `manifest.media` contains
URLs which may themselves carry sensitive signed parameters.

## Managed SELECT-only source bridge

This alternative needs no source DSN, Python connection to the source, extension
installation or session mutation. It does **not** execute a production query:

```text
python scripts/transfer_production_state.py source-sql > /secure/source.sql

# After separately authorized, private retrieval of the complete JSON result:
python scripts/transfer_production_state.py wrap-source-result \
  --bundle /secure/state-bundle.json \
  --expected-capsule-sha256 <independently-retained-capsule-digest>
```

The wrapper reads JSON from **stdin**, never a credential or a source connection.
The expected capsule digest is optional for wrapping, but must come from an
independent trusted channel if used as an external integrity check. The printed
manifest digest remains mandatory for subsequent plan/import/certify.

Submit the generated SQL unchanged in **one** managed
`executeSql({sqlQuery: sql, environment: "production", target: "replit_database"})`
call after separate authorization. This is static SQL, with no caller-supplied
identifiers/literals or parameters. Never split it into per-table calls or try
to establish a transaction using multiple callbacks: those calls do not share a
session. The statement contains only SELECT CTEs and PostgreSQL builtins: no
BEGIN/SET, DDL/DML, advisory locks, custom functions, provider calls, `set_config`,
COPY, temporary tables, procedures, extensions or server-side file output.
Function/aggregate names, catalog relations and comparison/concatenation
operators are explicitly bound to `pg_catalog`; public overloads are not trusted.
Arithmetic uses builtin integer operands with `pg_catalog` first in the path.
Before descriptive catalog inspection or row rendering, a materialized gate
checks the exact primitive/enum type names, namespaces, kinds and nongenerated
column layout (including the version column), and rejects non-catalog cast
functions from source-column types. A subsequent catalog gate must pass before
the lateral row-rendering subqueries run. This prevents implicit custom JSON
casts or unreviewed domain/type output from bypassing helper qualification.

The statement refuses unless:

- The sole version row is `0031`; the public table/relation inventory is exact.
- All 48 source catalog descriptors match the pinned source hashes: columns,
  defaults, types, nullability, constraints, indexes, enum order, row security
  and noninternal triggers plus their function definitions. PostgreSQL 18's
  additional NOT NULL entries must still be validated/enforced/canonical.
- Every reviewed count matches, including the five excluded OAuth rows.
- Publication history is exactly four PUBLISHED and five CANCELLED; permissions
  are closed/consistent; the five runtime-work tables have no
  RUNNING/STARTED/QUEUED/PENDING records; exactly one PAUSED control exists.
- The executing transaction is read-only, timezone is UTC,
  standard-conforming strings are on, and the catalog is first in the effective
  search path. Incompatible sessions refuse; the SQL changes no setting.

All guards, rows, counts and digests are captured under the **same statement
MVCC snapshot**, even at READ COMMITTED. Guard failure produces no capsule:
division by zero is intentional, and callers must sanitize failure diagnostics.
The JSONB result has one column `capsule` and one row. Its payload includes actual
snapshot/server/transaction/statement-time evidence, verified catalog and
dependency metadata, source/exported content digests and the 47 preserved
tables' rows. OAuth rows are absent; their source count/digest remain present.
Alembic and `_system` contents are never exported.

The snapshot's `capture_mode=single_statement_mvcc` and `statement_count=1`
identify the new evidence contract. `isolation` is the **actual** session
isolation, not a falsely reported repeatable-read transaction. The query-template
digest hashes the UTF-8 generated query with its embedded template-digest field
replaced by `__QUERY_TEMPLATE_SHA256__`, avoiding a self-referential hash.
The capsule digest is SHA-256 of PostgreSQL `payload::jsonb::text` in UTF-8.
The offline codec reproduces JSONB key-length/byte ordering and spacing;
business numerics remain opaque PostgreSQL-rendered strings throughout.

Supported private JSON inputs are the raw capsule, `{"capsule": capsule}`,
`[{"capsule": capsule}]`, or the documented successful managed-result envelope
`{"success": true, "exitCode": 0, "exitReason": null, "output": "<JSON>"}`.
Pretty tables, missing/duplicate keys, multiple rows, failure envelopes,
truncation, catalog/dependency/evidence drift, inconsistent SQL-null masks,
row/count/content/capsule tampering and missing FK references refuse.
Do not strip an apparent truncation marker or repair a result manually.
There is no page/chunk/resume fallback: that would lose the single snapshot.

**The result/capsule is sensitive.** Use a separately approved private result
transport into stdin; do not render `result.output`, log tool results, place rows
in chat, persist a raw capsule with ordinary permissions or dump a bundle.
Only the offline wrapper writes the validated result as the ordinary bundle,
using the existing exclusive/fsynced **0600** writer. It prints safe counts and
digests only. Retrieval infrastructure that journals/renders its raw callback
result is **not** a confidential transport merely because application code avoids
`console.log`. A future production operation must establish private complete
delivery first. No production callback, delivery path, replica freshness,
provider/token viability or real-source export was tested during engineering.

The production callback reads a **replica**, not a live writer barrier. One
consistent replica snapshot does not certify that the primary has stopped
changing or that replication has caught up. Existing independent writer-freeze,
cutover, external digest, secret and media gates still apply.

The wrapper validates, then emits the existing v1 bundle format. Bridge bundles
carry the capsule digest; offline plan/import/certify reconstruct and verify its
binding to their rows and evidence. Legacy DSN bundles retain their original
repeatable-read verification path. Neither target transaction/locking logic,
target DDL, runtime wiring nor migration history is changed.

## Snapshot and deterministic encoding

DSN export uses one **REPEATABLE READ, READ ONLY** transaction. It records exact
source revision `0031`, server version/version number, transaction time,
`pg_current_snapshot()`, isolation and read-only evidence.

Each table has source/exported counts, source/exported content fingerprints and
its explicit transfer policy. Dependencies and self-FK columns come from the
verified catalog. The overall manifest digest binds all metadata, table digests
and media references. Identical canonical rows have identical table digests.
New snapshots have different time/snapshot metadata and may therefore produce
different overall manifest digests even when row content is unchanged.

Rows use PostgreSQL's canonical `to_jsonb(row)::text`, plus a separate SQL-null
column mask. This avoids Python floating-point conversion of decimal data and
preserves **SQL NULL versus JSON literal null**. Canonical outer JSON and sorted
row encodings make fingerprints independent of query order. JSON object key
order/whitespace/duplicate-key spelling is normalized; certification promises
semantic JSON content, not original textual formatting. UUID/string IDs,
timestamps, decimals, persisted fingerprints and opaque ciphertext are retained.

Digests detect corruption, not authenticity: securely retain the reviewed
manifest digest independently of the bundle and supply it explicitly. An
attacker who can replace both is outside a checksum's trust guarantee.

## Closed-state refusal

Export refuses:

- Anything except exact revision 0031 and the frozen 48-table source contract.
- Any changed source table count.
- Any open authorization, permit or pilot activation, including time-expired
  records still marked ACTIVE. CONSUMED must have `consumed_at`; REVOKED must
  have `revoked_at`. Historical EXPIRED/REVOKED rows are not required to be consumed.
- Any publication distribution other than **4 PUBLISHED / 5 CANCELLED**,
  including PUBLISHING/PUBLISH_UNKNOWN.
- RUNNING routines or unfinished STARTED/QUEUED/PENDING catalog/autonomous work.
- Anything except one PAUSED routine control.

UNKNOWN **attempts** are historical evidence and are not refused, reset or
reconciled. All nine attempt rows and all seven reconciliation rows are covered
by exact content certification. Bundle verification independently checks closed
state, so rehashed open-state data still refuses before insertion.

An export snapshot does not stop other sessions writing after the snapshot.
Before a real cutover, separately authorize and establish an operational freeze,
then keep the source closed until the authorized cutover is complete. This tool
does not pause workers, change runtime configuration or freeze provider accounts.

## Target preflight, atomic import and certification

Target requires PostgreSQL, exactly Alembic **0034**, canonicality PASS, the exact
expected target relation set, all transferred business/history/authorization
tables empty, excluded OAuth state empty, and all target-only tables empty.

**Fresh-schema exception:** migration 0031 creates
`routine_publishing_control(id='default', state='PAUSED')`. An absent control or
that untouched scaffold (no pause actor/reason/time or last-unknown reference)
is allowed. The scaffold is replaced by the source PAUSED record **inside the
same import transaction**. No other preexisting business/control state is allowed.

Import takes transaction-scoped exclusive locks on the fixed target relation
set including Alembic. It does not disable triggers/FKs, adopt/stamp schemas,
run migrations, change config, call application services or grant permissions.
Table dependencies are topologically ordered; self references are inserted
parent-before-child. Missing references/cycles refuse. All current application
IDs are varchar/UUID strings, with no application sequences/identities; there
are consequently **no sequence-repair statements**. Future identity schema
changes require a new reviewed contract rather than speculative sequence resets.

Import and its certification are **one transaction**. Any validation, insert,
lock, timeout, FK or certification failure rolls back everything, including
scaffold replacement. There is no resume option or runtime-authorizing durable
receipt. A repeat import safely refuses nonempty state; standalone read-only
certification can be repeated. No partial continuation is supported.

Certification compares exact exported counts and canonical row fingerprints for
every source table (zero for excluded OAuth), rechecks schema and FK ordering,
requires zero target-only rows, closed permissions, PAUSED routine control,
4 PUBLISHED / 5 CANCELLED and no unfinished runtime work. It returns
`database_certification=PASS`, but **media/provider readiness=NOT_PERFORMED**.
No certification output is a runtime execution authorization.

## Token prerequisite — later, separately authorized

Before any later provider-readiness check, Railway must have the **exact existing
`PINTEREST_TOKEN_ENCRYPTION_KEY`** used to encrypt the retained ciphertext.
A newly generated key cannot decrypt it. The matching Pinterest OAuth client
configuration is also required for later refresh/reconnection. Buffer API key,
organization and channel configuration are separate from these database rows.

Engineering and this migration code do not read, validate, set or transfer any
of those secret values. No token viability or provider account access is claimed.

## Explicit later media-certification phase

Database export records creative IDs/rendered references/SHA-256 values and
publication IDs/media snapshot URLs; it **never fetches or copies object bytes**.
The reviewed inventory has 17 rendered creatives and nine media snapshots.

After separate authorization, and before provider readiness or execution:

1. Obtain an object inventory without publishing or provider mutation.
2. Match all 17 creative identities/digests to actual PNG bytes; transfer bytes
   through an explicitly approved storage path, preserving digest identity.
3. Certify PNG signature and SHA-256, required generated-asset references if any,
   and target media serving/durability. Account for relative rendered paths.
4. Review all nine frozen publication media URLs; do not silently rewrite
   historical snapshots. Record old-versus-target serving decisions separately.
5. Issue a separate media report; keep runtime execution closed until separately
   authorized provider readiness and execution approvals.

This phase is a documented gate, not an implemented storage client, and was
**not executed** during engineering.

## Disposable verification

`tests/test_migration_closed_state_transfer.py` constructs the complete synthetic
inventory on a fresh 0031 schema and round-trips it into fresh 0034. Its invalid
Fernet-like sentinel proves ciphertext opacity without real credentials.
The source fixture adds the exact inventoried eight foundational FKs to the fresh
0031 schema; the target remains the unmodified clean 0034 chain.
The existing isolated runner strips inherited service secrets before collection,
denies external networking and live storage-client construction, and creates
only disposable PostgreSQL clusters. The clean-chain CI workflow's
`test_migration*.py` glob includes these tests on PostgreSQL 16/17/18.

`tests/test_migration_select_source_bridge.py` adds DSN/SELECT row-digest
equivalence, one-cursor-statement observation, concurrent-write snapshot
consistency, Unicode/DEL/precise JSON/SQL-null codec coverage, in-SQL source
refusals, public overload/custom-cast/domain refusals, private wrapper file-mode
and stdout/error opacity, truncated/tampered result refusal, and ordinary target
plan/import/certify/re-import refusal. These use only synthetic disposable data.
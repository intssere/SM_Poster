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

## Snapshot and deterministic encoding

Export uses one **REPEATABLE READ, READ ONLY** transaction. It records exact
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
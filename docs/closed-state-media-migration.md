# Manual closed-state Replit → explicit S3 media migration

This is an engineering command, not a startup hook, worker, deployment step,
publishing admission, or authorization to operate production. Every execution
(including resuming a partial transfer) requires a separate operator authorization.
Neither an old acknowledgement nor this document authorizes re-execution.

## Contract

Run from `backend` with `python -m app.state_transfer.migrate_media`.
Arguments accept environment **names**, not credential values:

```text
--database-env SOURCE_METADATA_DSN
--source-root /explicit/local/media/root
--target-endpoint-env MIGRATION_S3_ENDPOINT
--target-bucket-env MIGRATION_S3_BUCKET
--target-access-key-env MIGRATION_S3_ACCESS_KEY
--target-secret-key-env MIGRATION_S3_SECRET_KEY
--target-region-env MIGRATION_S3_REGION
--target-path-style-env MIGRATION_S3_PATH_STYLE
--dry-run
```

These are illustrative names, not configuration added by this change. All seven
names must be distinct and explicitly supplied. The region has no default;
path-style must be exactly `true` or `false`. The target endpoint must be HTTPS.
The existing validated `S3Config` contract is reused; no application settings,
ambient AWS credentials, Replit target fallback, or Railway management API is used.

`--dry-run` authorizes source/target **reads**, not puts. To perform the separately
authorized transfer, replace `--dry-run` with `--execute`, or supply
`--execution-env NAME` whose value is exactly `MIGRATE_CLOSED_STATE_MEDIA_ONCE`.
Mixing dry-run with a write gate is refused. Missing/invalid gates are refused
before filesystem, database or storage access. There is no automatic retry,
resume, persistent enable flag, scheduler registration, or authorization-row write.

## Phases and fail-closed behavior

1. Validate explicit configuration and non-overlapping, non-symlink local roots.
2. Use one PostgreSQL read-only/repeatable-read transaction. Require **0031 only**,
   the existing authorization/job/control closed-state guards, exactly four
   PUBLISHED and five CANCELLED publications, and exactly 17 authoritative
   creative bindings with valid IDs, lowercase SHA-256, persisted sizes and
   RENDERED/STAGED status. Read no rendered URLs, tokens or publication content.
3. Resolve only `<root>/<creative_id>.png` and
   `<root>/creative/<creative_id>/<sha256>.png`. A duplicate, corrupt, linked,
   changed or unsafe local candidate is refused, not bypassed. Only an absent
   local candidate may use the exact digest key in the attached default Replit
   durable bucket. There is no listing, prefix search or HTTPS fallback. Orphan
   files are never accessed. Require verified bytes for **all 17** before even
   constructing the target client.
4. Read all 17 exact target keys. Existing objects must pass PNG signature,
   SHA-256 and exact size checks (`VERIFIED_EXISTING`). Missing keys are `MISSING`.
   Any invalid existing object is `CONFLICT`; any unreadable object also refuses
   the operation. No put occurs until this entire preflight succeeds.
5. Unless dry-run, put only missing keys using `If-None-Match: *`. This protects
   against a concurrent create between preflight and put; refusal stops rather
   than rewriting, retrying or silently adopting a race. Immediately get each
   uploaded object and repeat all byte checks before advancing to the next put.
   Never delete, rewrite a verified existing key, modify DB rows or alter
   historical publication fingerprints.

Each client has explicit timeouts, bounded reads and one application-level
attempt. The S3 adapter additionally rejects a second transport send (including
SDK region redirects). The Replit adapter disables SDK download and auth retries,
rejects redirects, permits only exact allowed keys, and permits only one call to
each required local credential/default-bucket endpoint. SDK incompatibility fails
closed, with no provider substitution. Source memory is capped at 256 MiB; each
object is capped at 100 MiB, and remote reads at expected size plus one byte.
The existing general-purpose storage adapters and backend-selection rules are
unchanged.

## Output and partial operations

Output is structured JSON: validated creative IDs/keys/digests/sizes, source
classes, target statuses, counts, fingerprints and safe terminal stages. No
exception messages, local paths, raw endpoint URLs, credentials or image bytes
are returned. Source fingerprint binds authoritative immutable metadata; target
fingerprint binds observed target statuses (verified skips and verified uploads
normalize identically). Transfer fingerprint binds source, target, and a hashed
noncredential endpoint/bucket/region/path-style identity.

`MEDIA_CERTIFIED` means all 17 exact target objects have been byte-verified;
`publishing_admission` remains `NOT_GRANTED`. Dry-run with missing keys is not
certification. A put/readback failure may leave a partially populated target:
output explicitly distinguishes `PUT_ATTEMPTED_UNCONFIRMED` and
`UPLOADED_UNVERIFIED`. Never automatically retry it. After new authorization,
a new invocation preflights every source and target again, skips only objects
that verify, and refuses conflicts before additional writes.

## Isolated verification

```sh
python scripts/run_isolated_readiness_tests.py --with-postgres -q \
  tests/test_media_migration.py tests/test_migration_storage.py \
  tests/test_media_migration_postgres.py tests/test_media_continuity.py \
  tests/test_media_continuity_postgres.py tests/test_s3_media_storage.py \
  tests/test_migration_closed_state_transfer.py
```

The runner strips attached service credentials before collection, installs live
storage/client and external-network fences, and creates its own disposable
PostgreSQL cluster. All migration storage operations in tests use memory doubles.
No production probe or transfer is included in tests or CI.

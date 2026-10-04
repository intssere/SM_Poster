# Explicit one-shot closed-state migration

This is an operator-invoked command for an otherwise disposable service, not an
application startup command. No application configuration, startup hooks,
migrations, provider gates, or deployment files change.

Configure two explicitly named environment variables in that service: one for
the source PostgreSQL DSN and one for the target PostgreSQL DSN. The target may
be supplied by an environment reference already resolved by the hosting
platform. The runner does not resolve references or fall back to `DATABASE_URL`.
Do not pass a DSN on the command line.

```sh
python scripts/migrate_closed_state_once.py \
  --source-env MIGRATION_SOURCE_DSN \
  --target-env MIGRATION_TARGET_DSN \
  --execute \
  --statement-timeout-ms 480000 \
  --lock-timeout-ms 10000
```

Alternatively replace `--execute` with `--execution-env MIGRATION_ACK`, and set
that explicitly named variable to exactly `IMPORT_CLOSED_STATE_ONCE`. Neither
variable names nor execution permission are inferred from service startup.
No execution flag/ack means refusal before any connection or temporary file.

Source statement timeout: 1,000–900,000 ms, default 480,000. Source lock timeout:
100–60,000 ms, default 10,000, no greater than the statement timeout. The capture
runner's existing callers retain their original 180,000/10,000 ms defaults.
Read-only/repeatable-read behavior, idle-in-transaction timeout, and every source
guard remain unchanged. Existing target import transaction timeouts stay unchanged.

The guarded source SELECT certifies the exact 0031 identity, reviewed catalog,
inventory counts, closed publication history, permissions, runtime work, and
PAUSED control in its single read-only snapshot. There is exactly one capture
SELECT per invocation and no paging, alternate capture SQL, or retry.

In the same process, the runner:

1. Writes only owner-only capsule and bundle files inside a private directory
   outside the checkout; verifies file ownership/modes and both digests on reload.
2. Uses existing `import_target(..., plan=True)` for read-only empty-target,
   0034 schema/canonicality, dependencies, row, and reference preflight.
3. Only after plan PASS, uses the existing locked atomic import. That import
   repeats preflight and certifies inside the transaction before commit.
4. Performs existing read-only certification again after commit.
5. Attempts overwrite/fsync/unlink of both ephemeral files and removes their
   private directory in `finally`, including on failures. Only safe status,
   counts, digests and allowlisted diagnostics go to stdout. No private paths,
   row data, ciphertext, DSNs, URLs, or exception messages are emitted.

Do not attach persistent volumes for export files. Overwriting is best effort:
SSD/journaled storage can retain copies; physical erasure is not guaranteed.
Use nonpersistent memory-backed temporary storage if that guarantee is required.
An uncatchable process kill cannot run `finally`; disposing the service/storage
remains necessary. The service supervisor's deadline must exceed the source
query deadline plus wrapping/import/certification/cleanup time.

Repeat invocation after success refuses on target nonemptiness. Concurrent
imports are serialized by the existing target locks and repeated empty-state
preflight. A failed import never automatically retries. Transport failure during
COMMIT may leave an unknown outcome; post-commit certification or cleanup failure
can occur after import committed. Output distinguishes these states. Obtain
separate authorization to investigate/certify the target; do not assume it is
empty or retry automatically.

This command copies database state only. It does not copy media bytes, contact
providers, enable publishing, create runtime receipts, or certify provider/media
readiness.
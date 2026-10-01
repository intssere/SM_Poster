# Isolated Object Storage readiness probe

This change is **code/CI only**. It authorizes no production execution, storage
operation, deployment, configuration change, or business/provider operation.

## Boundary and authorization

`backend/scripts/object_storage_readiness.py` is a standalone utility, not an
application route, health check, startup hook, scheduler, or worker. It imports
only the standard library until `OBJECT_STORAGE_READINESS_PROBE_ENABLED` is
exactly `true`. Missing/`false` means disabled; all other values are rejected.
The gate defaults to false and is not added to application Settings.

The SDK is imported lazily; clients are constructed with `Client()` and no
bucket argument, using the runtime's attached default bucket. No application
settings, database, ORM, business model, or Buffer/Pinterest/Shopify/AI client
is imported. There is no local-storage fallback.

A receipt's PASS means the backend round trip passed in the executing process.
It does **not** certify that this process was the intended production release,
that PNGMediaStorage consumers ran, or that public-media URLs work. The
published-runtime marker is evidence only, not independently verified
provenance. Independently establish an authorized production execution path
and certify that release before any future live invocation. Do not substitute
a workspace client, change production startup, or add an endpoint to make this
utility reachable.

## Object and sequence

The only allowed namespace is `task61-readiness/`. Each invocation generates
one cryptographically random 32-hex identifier internally:

`task61-readiness/<32-hex>.png`

There are no CLI arguments for keys, prefixes, buckets, URLs, listing, retries,
or business identifiers. Any CLI arguments are rejected without SDK loading.
An already-existing generated key aborts without write or deletion.

The fixed synthetic payload is a valid 1x1 RGBA PNG, 70 bytes:

`dc5687b50f70cb95379bfced5a0eae768dd4382cd6b393ee77d65bbdd6373fbf`

That SHA-256 belongs only to this synthetic payload. It is never persisted as
a source-image hash, creative receipt, business record, or database value.

Sequence:

1. Gate, generated-key and fixed-payload validation.
2. Initialize the default SDK client; check required method capabilities.
3. Confirm this exact key is unused.
4. Arm cleanup, then attempt exactly one `upload_from_bytes`.
5. Same-client download; verify bytes, size, PNG signature and SHA-256.
6. A distinct new `Client()` downloads and verifies the same exact object.
7. The original, already-bound writer deletes the exact key.
8. Another distinct new `Client()` must report `exists` false and raise the
   SDK's specific `ObjectNotFoundError` on download. Also verify absence using
   the original bound writer: fresh default-bucket resolution alone must not
   mask an object remaining in the writer's bucket.
9. Emit the JSON receipt; PASS requires verified absence.

Cleanup in `finally` uses only the original key and writer, never listing or
prefix deletion. If the normal sequence succeeds it does not delete again.
If a post-upload operation fails, cleanup attempts exact-key deletion and
fresh-client absence verification, retaining the original failure.

## Failure and observability limits

Receipts include version, generated key, expected digest/size, UTC timestamps,
per-stage results, SDK/default-bucket and distinct-client evidence, cleanup
status, explicit upload/delete acknowledgements and final status. They never
include environment dumps, bucket IDs, credentials, URLs, raw exceptions, or
stack traces. SDK stdout/stderr is discarded without retaining it.

Exit codes: `0` PASS, `1` FAILED, `2` BLOCKED.

An unacknowledged upload remains FAILED with cleanup unconfirmed even if an
immediate absence check passes: an interrupted server request could commit
later. Permission, authentication, bucket and transport errors are not
not-found evidence. SIGINT/SIGTERM enter the failure/cleanup path; SIGKILL,
network loss, SDK calls that hang, and forced runtime termination cannot
guarantee cleanup or receipt delivery. Never claim unconditional cleanup.

The public SDK does not expose conditional-create/generation-match controls.
A random reserved key plus preflight absence is not atomic collision
protection. The utility reports that default-bucket identity is not independently
verified; original-bound-writer absence is required as an additional safeguard.
Namespace reservation is a convention, not bucket-level access control. Report
cleanup uncertainty rather than inferring a clean state.

No business object or public-media URL is read. Cross-client success is not
independent-process proof. Existing PNGMediaStorage and local-only receipt
admission semantics remain unchanged.

## Offline verification

Run only the network-free injected-SDK suite for this engineering task:

```text
python -I -S -B -m unittest discover -s backend/scripts/tests -p test_object_storage_readiness.py -v
```

This suite denies network and DB/business/provider/real-SDK imports before
loading the utility. Fake storage is an in-memory dictionary. It does not
inherit the application test fixtures, attach a bucket, or create DB records.
No new model, schema, migration, package, or service is required.
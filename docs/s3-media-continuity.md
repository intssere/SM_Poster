# Explicit durable media and closed-state local-byte continuity

This change provides code only. It does not provision a bucket, transfer media,
change deployed configuration, grant publishing/admission, or replace existing
durable-canary receipts.

## Backend selection

With no explicit S3 configuration, development retains LocalStorage and exposed
Replit runtimes retain their attached Replit Object Storage client unchanged.
An explicitly supplied S3 setting takes precedence in either runtime. Partial,
empty, or invalid explicit configuration fails closed; it never falls back to
Replit or local storage.

Supply all four settings through the deployment's private configuration flow:
`OBJECT_STORAGE_ENDPOINT`, `OBJECT_STORAGE_BUCKET`, `OBJECT_STORAGE_ACCESS_KEY`,
and `OBJECT_STORAGE_SECRET_KEY`. Do not put credentials on command lines or in
source files. The existing bucket default is not sufficient for explicit S3:
the bucket must be explicitly supplied. Optional `OBJECT_STORAGE_REGION` defaults
to `us-east-1`; `OBJECT_STORAGE_PATH_STYLE` defaults to true and accepts the
settings system's boolean values. Select false for virtual-host addressing.
Endpoints support HTTP/HTTPS without embedded credentials, query or fragment.
Use HTTPS whenever traffic leaves a trusted private network.

The adapter uses boto3/SigV4, explicit credentials (no ambient AWS/IMDS chain),
bounded connection/read deadlines and one SDK attempt, with checksum extensions
only when required for compatibility. Importing/selecting it does not import or
construct a client. The first explicit put/get/exists operation constructs the
client. No bucket creation, listing, deletion, ACL changes or public access grants
are performed. Keys remain exactly `media_key(kind, id, sha256)`.

PNGMediaStorage still performs PNG signature/SHA verification and upload readback.
Existing staged/durable-canary promotion, receipt/provenance verification,
idempotent recovery, public-media checks and fail-closed admission remain the
source of truth. Provider failures are not local fallback or missing objects;
NoSuchBucket/access failures remain unavailable. Local inventory is not a
replacement for durable receipts.

## Manual inventory and zero-network target plan

From the backend WORKDIR:

```sh
python -m app.state_transfer.inventory_media_continuity \
  --database-env MEDIA_SOURCE_DSN \
  --source-root /explicit/local/generated-creatives \
  --execute
```

Repeat `--source-root` for disjoint roots. Add `--target-plan` to derive exact
digest-addressed target keys. Alternatively replace `--execute` with
`--execution-env MEDIA_INVENTORY_ACK`, whose value must be exactly
`INVENTORY_MEDIA_CONTINUITY_ONCE`. There is no DATABASE_URL fallback, inferred root,
startup/health hook, automatic execution or provider-object fetch. The gate
precedes reading the DSN, inspecting roots, connecting to DB, or constructing
storage. The command never loads provider settings or credentials.

The PostgreSQL metadata read is a bounded read-only/repeatable-read transaction.
It requires reviewed revision 0031/0034 and reuses the existing closed-state
checks, including exactly four historical PUBLISHED/five CANCELLED publications,
closed authorization/runtime work and one PAUSED routine control. It reads only
creative ID, SHA256, byte size and render status.

Coverage includes RENDERED/STAGED creatives and any other row with media evidence.
Non-media-bearing unrendered rows are counted separately, not silently treated
as copied. Valid media-bearing rows must have supported status, safe ID and a
lowercase SHA256. Nullable legacy size is allowed because SHA binds the full
bytes; a supplied size must match.

Supported sources are flat `<creative_id>.png` caches and existing
`creative/<creative_id>/<sha256>.png` layouts. Exact ID and digest are required:
matching another ID's bytes never satisfies coverage. Every candidate is read
without following symlinks, hashed, signature-checked and checked for concurrent
size/mtime changes. Duplicate candidates are ambiguous even if their bytes
match; nested/overlapping roots are refused. Symlinks, hardlinks, unsupported
names/files, unreadable data and orphan PNGs fail completeness. Limits are
100,000 metadata rows/PNGs and 100 MiB per PNG; empty required coverage does not
certify continuity.

Output contains only aggregate/status/digest metadata and, in target-plan mode,
safe exact target object keys, hashes and sizes. It distinguishes MATCHED,
MISSING, DIGEST_MISMATCH, DUPLICATE and UNSUPPORTED. Incomplete coverage exits 2;
complete inventory/plan exits 0. A failed plan may show its verified subset but
is not a successful complete plan.

The inventory fingerprint binds verified IDs, hashes, sizes and statuses; it is
not a persistent receipt and does not freeze files after the command exits.
Provider existence, target configuration, full production coverage, deployment
filesystem equivalence and publishing readiness are NOT certified. Uploading or
checking target objects is a separate explicitly authorized operation that must
verify the same bytes again. Neither mode uploads/deletes/fetches storage objects,
alters rows, creates receipts, or opens provider/publishing/autonomy gates.
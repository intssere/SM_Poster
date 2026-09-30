# Task 61.6A: local staged media protocol

This is a provider-free, filesystem-backed development/canary fixture protocol.
It does not enable a worker, scheduler, publishing gate, or live canary. It is
not a replacement for durable shared media storage.

## Safety prerequisites

Preparation and reconciliation fail closed unless the routine publishing
control and scheduler are safe: the control is paused; the scheduler is
dormant and supports its distributed lease; no run is active; scheduled
publication, active-permit, `PUBLISH_UNKNOWN`, and `PUBLISHING` counts are
zero; and all of these settings are exactly `false`:

- `publishing_enabled`, `buffer_publishing_enabled`,
  `routine_pinterest_worker_enabled`, `routine_buffer_dispatch_enabled`,
  `routine_pinterest_scheduler_enabled`,
  `routine_scheduled_live_admission_enabled`,
  `routine_scheduled_autonomy_enabled`, `routine_scheduler_canary_enabled`,
  `routine_autonomous_authorization_enabled`,
  `pinterest_autonomous_generation_enabled`,
  `pinterest_autonomous_execution_enabled`,
  `pinterest_portfolio_activation_enabled`, `pinterest_optimizer_apply_enabled`,
  `pinterest_autonomous_board_ensure_enabled`, `pinterest_write_scope_enabled`,
  `pinterest_board_write_scope_enabled`, `pinterest_board_provisioning_enabled`,
  `buffer_single_pin_pilot_enabled`, `pinterest_single_pin_pilot_enabled`.

The routine dry-run setting must be exactly `true`, with batch size and daily
write limit exactly `1`. The passed settings must agree with runtime settings.
Do not loosen these guards to run the fixture. It rejects a source unless the
caller provides its actual local bytes and the bytes' SHA-256 matches the
persisted eligible source-image digest. Source URLs are provenance only; the
renderer does not fetch them. No AI, Pinterest/provider, or other external
requests are made.

## Stage, commit, promote, finalize

Pass the verified source bytes, source image ID, one planned portfolio-item ID,
and a media root beneath the node's `/tmp` to
`prepare_local_canary_fixture(...)` in
`app.services.pinterest_local_canary_fixture`.

The renderer creates a PNG locally. It writes and fsyncs the artifact under
`<root>/.task61-6-staging/`, then stores a protocol receipt
(`TASK61_6A_LOCAL_MEDIA_STAGED_V1`) with path, source/artifact hashes, size,
and provenance in the creative's `render_spec`. The database transaction
commits a pending creative with `render_status=STAGED` before filesystem
promotion. A database rollback leaves no committed fixture; an orphan stage
file can remain after a crash.

Promotion atomically moves the staged file to
`<root>/creative/<creative-id>/<sha256>.png`; it verifies PNG signature,
size, digest, paths, and receipt. Reconciliation then rereads and verifies the
promoted bytes, rechecks fixture identity and safety preconditions, and
finalizes the database records. Only then is media `RENDERED` and the
publication scheduled with its routine dispatch permit. Admission blocks
pending staged media and also blocks local receipts whose promoted bytes
cannot be verified. Repeating preparation on an existing item reconciles it;
explicit recovery uses `reconcile_local_canary_fixture(...)` with the same
item ID and local root.

If process interruption occurs after the pending database commit, retain both
the database receipt and the same node-local root, and reconcile before
retrying or cleaning files. If promotion has already happened but finalization
has not, reconciliation verifies the final artifact and finishes safely.

## Orphan cleanup and limits

`cleanup_local_canary_orphans(db, local_media_root)` removes only files in
`.task61-6-staging` that are not referenced by committed staged-creative
receipts. It preserves referenced pending stages; it does not delete promoted
creative artifacts. Use the same database and node-local root used for
preparation. Do not clean a pending receipt as an orphan or copy its root to a
different host.

The artifact root is under node-local `/tmp`: it is ephemeral, not shared
between nodes, and not durable across replacement/restart. A configured
`public_media_base_url` is required and its HTTPS origin is structurally
checked to construct/compare the publication media URL. **Live URL
accessibility is not tested or validated**; this protocol does not upload or
serve the local artifact at that HTTPS URL. Do not interpret a structurally
valid URL or successful local reconciliation as proof that remote consumers
can retrieve the media.
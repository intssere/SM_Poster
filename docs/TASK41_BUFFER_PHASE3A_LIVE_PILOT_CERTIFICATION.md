# Task #41 Phase 3A: Buffer live-pilot certification

**LIVE BUFFER/PINTEREST WRITE NOT AUTHORIZED**

**NO postsWrite CREDENTIAL AUTHORIZED**

**NO LIVE PIN AUTHORIZED**

## Baseline

Branch: `task-41-buffer-live-pilot-certification-v1`

Starting commit: `3c16ea67ddab68d23651070ba2785ddee809ba59`

Starting tree: `1d78a7fb775196887a0e96e2f7784d780492fbb8`

Alembic head remains `0016`. No migration is introduced by Phase 3A.

## Purpose

Phase 3A adds an offline certification service for one explicit Buffer pilot
candidate. It answers whether a named publication has enough local evidence to
be considered statically ready for a future live pilot. It does not authorize
or perform the live pilot.

The service is `certify_buffer_pilot_candidate(db, publication_id, *, settings=None, now=None)`.
It requires a caller-provided publication ID and never selects a candidate
automatically.

## Static Vs Live Readiness

Static certification requires local persisted evidence only:

- the publication exists and is `SCHEDULED`;
- `scheduled_for` exists, but it may be in the future;
- immutable publication snapshot fields and fingerprints are complete;
- the Pinterest connection is `CONNECTED`;
- the Pinterest board row belongs to that connection, is active and eligible,
  and matches the immutable provider board snapshot;
- the approval, revision, creative, source image, media URL, template, and
  content fingerprints still match;
- quality is `PASS`;
- duplicate evaluation is `SAFE_TO_CONTINUE`;
- there are zero prior `PublicationAttempt` rows;
- no known Pinterest Pin ID exists;
- the Buffer organization, Pinterest channel, and allowlisted API base are
  configured;
- the Buffer payload can be built from the immutable snapshot without changing
  destination, UTM, title, description, alt text, or media provenance.

Protected runtime flags do not make static certification fail. When the local
candidate is valid and the flags remain false, the service returns
`status = STATIC_CANDIDATE_READY` and `live_execution = LOCKED`.

## Safe Dossier

The returned dossier is bounded and deterministic when `now` is supplied. Safe
fields include:

- publication ID, publication fingerprint, and request fingerprint;
- revision, creative, source image, and approval IDs;
- Pinterest connection ID, board row ID, and provider board service ID;
- title, description, alt text, destination URL, UTM URL, and media URL;
- quality status and policy version;
- duplicate status;
- attempt count and whether a known Pinterest Pin ID already exists;
- latest authorization status only;
- Buffer configuration booleans;
- protected gate booleans.

The service performs no commit, no flush, no authorization creation, no
publication mutation, no attempt creation, no provider read, and no provider
write.

All ORM reads run inside a `db.no_autoflush` boundary. Certification must not
flush pending unrelated session changes, even if the caller hands it a dirty
SQLAlchemy session.

Existing `PublicationReconciliationEvent` history for the selected publication
blocks static readiness with `RECONCILIATION_HISTORY_EXISTS`. The dossier may
include a safe event count, but it must not expose raw event reasons or provider
bodies.

## Credential Handling

The only credential signal is:

`credential_configured = isinstance(settings.buffer_api_key, str) and bool(settings.buffer_api_key.strip())`

The dossier must never include the credential value, length, prefix, suffix,
hash, derived token status, or inferred `postsWrite` permission.

`write_credential_authorized` is always `false` in Phase 3A.

Buffer organization and Pinterest channel identifiers are structural booleans,
not truthiness checks. They must match `[A-Za-z0-9_-]{1,255}` before
`organization_configured` or `channel_configured` can be true. Whitespace,
embedded whitespace, punctuation outside the allowlist, empty strings, and
missing values fail closed without exposing validation internals.

## Media Limitations

Media is checked structurally only. The certification service does not fetch the
media URL, perform DNS resolution, verify object-storage availability, or prove
that Buffer can ingest the image.

The media dossier reports:

- `structurally_valid`;
- `provenance_present`;
- `live_fetch_verified = false`.

## Provider Verification Limitation

The Buffer provider destination is not live-verified in Phase 3A. The service
does not call `verify_destination()`, does not construct `BufferGateway`, and
does not contact Buffer or Pinterest.

`provider_destination_live_verified` is always `false`.

## External Link Limitation

Buffer-created Pinterest `externalLink` parsing remains fixture/schema
certified only. A real Buffer-created Pinterest `externalLink` has not been
live-certified.

If Buffer returns an unexpected future external-link format during a later live
pilot, the correct outcome remains `PUBLISH_UNKNOWN`; no second `createPost`
call is allowed, and parser broadening requires new evidence.

`external_link_format_live_certified` is always `false`.

## Task #39 Authorization Timing

Static certification does not require Task #39 dispatch authorization. It reads
only the latest authorization status, if one exists.

Safe statuses are `NOT_CREATED`, `ACTIVE`, `EXPIRED`, `REVOKED`, and
`CONSUMED`. If a persisted row is `ACTIVE` but `expires_at <= now`, the dossier
reports `EXPIRED` logically without mutating the row and without calling
`expire_stale_active_authorizations()`.

## Future Credential Gate

A future live pilot still needs an independently authorized Buffer credential
with write permission. Phase 3A does not inspect the real credential, does not
create a `postsWrite` key, and does not infer write capability from the
presence of `BUFFER_API_KEY`.

## One-Write Rule And No-Retry Rule

The later live pilot must continue to permit at most one provider mutation for
the exact certified candidate. `PUBLISH_UNKNOWN` remains a terminal manual
reconciliation state and must never be automatically retried.

## Failure State Policy

`PUBLISH_FAILED` remains the outcome for definitive provider or post-claim
validation failures. `PUBLISH_UNKNOWN` remains the outcome for ambiguous
provider or persistence uncertainty.

Persistence-unknown handling must preserve the provider operation ID when it is
known, move the publication to `PUBLISH_UNKNOWN`, and require exact operation
reconciliation. It must not create a second Buffer post.

## Future Post-Pilot Shutdown

After any real single-pin pilot, operators must return the system to a locked
state unless a separate release gate explicitly authorizes continued live use.
The protected defaults remain:

- `PUBLISHING_ENABLED=false`;
- `BUFFER_PUBLISHING_ENABLED=false`;
- `BUFFER_SINGLE_PIN_PILOT_ENABLED=false`;
- `PINTEREST_WRITE_SCOPE_ENABLED=false`;
- `PINTEREST_SINGLE_PIN_PILOT_ENABLED=false`.

Pinterest live OAuth requested scopes remain read-only:

- `user_accounts:read`;
- `boards:read`;
- `pins:read`.

## Evidence Checklist

Before any later live pilot:

- certify exactly one explicit publication ID;
- independently verify the candidate dossier;
- verify a real Buffer write credential exists outside the dossier;
- confirm the Buffer organization and Pinterest channel with a separate
  authorized live-read step;
- confirm no prior publication attempt or known provider operation exists;
- confirm the candidate remains scheduled, approved, unique, and quality pass;
- confirm protected flags are deliberately enabled only for the authorized
  pilot window.

## Incident Stop Conditions

Stop immediately if any of the following appears:

- a provider write occurs during certification;
- a credential value or derivative appears in logs, test output, or a dossier;
- a certification run creates or mutates an authorization, attempt, publication,
  audit event, or provider state;
- Buffer returns an unrecognized external-link format during live observation;
- a provider operation ID is known but local persistence is uncertain;
- a publication enters `PUBLISH_UNKNOWN`;
- any request attempts automatic retry after uncertainty.

# Security and Safety

## Current controls

- `PUBLISHING_ENABLED=false` blocks production publication.
- Protected AI settings remain disabled; hosted provider failures fail closed.
- Approval requires an exact valid proposal version/creative identity.
- Publication snapshots reject cross-proposal revision/creative, cross-store board/account, incomplete provenance, and duplicate fingerprint combinations.
- Authentic Shopify `source_image_id` is retained in every new publication snapshot.
- Historical rows remain readable without invented provenance.

## Prohibited behavior

- No secrets in source, APIs, telemetry, logs, fixtures, or documentation.
- No Pinterest/OpenAI call from approval or publication identity operations.
- No generated branded product, package, or logo.
- No automatic selection, approval, schedule, publication, retry, or model escalation.
- Board synchronization is read-only; no Pinterest board, section, or Pin writes are permitted.

## Known gap

Internal proposal APIs do not yet have dedicated authentication/authorization. Address this in a separate security-hardening task before broader operational exposure.

## Task #35 status

Operational API authorization is now centralized behind a single-admin signed session. `APP_SECRET_KEY` must be at least 32 characters and `ADMIN_USERNAME` plus a PBKDF2 `ADMIN_PASSWORD_HASH` are required in exposed mode. `AUTH_DISABLED=true` is rejected by policy in exposed mode. Cookies are HttpOnly, SameSite strict, Secure when exposed, and expire automatically; credentials never enter frontend storage or logs.

Task #36 OAuth uses one-time hashed state records, server-side code exchange, and Fernet-encrypted tokens. Only read scopes are requested; publishing scopes and calls remain disabled.

## Task #37 status

Board Sync & Board Manager v1 stores normalized read snapshots behind authenticated routes only. It performs no Pinterest mutations, scheduling, analytics ingestion, or publication. `PUBLISHING_ENABLED=false` remains authoritative and AI/provider mode remains disabled.

Task #38 keeps snapshot creation provider-independent while requiring separate publishing and `pins:write` gates for any provider call. Attempts store only allowlisted metadata; credentials and raw provider bodies are never persisted. No autonomous scheduler or automatic `PUBLISH_UNKNOWN` retry exists.
# Pinterest provider validation

Provider metadata is type-validated before persistence. Malformed containers or values fail closed; provider values are never silently stringified or truncated. Failed malformed synchronization does not advance `boards_last_synced_at`. PR #15 is merged and publishing remains disabled.
# Board synchronization refresh safety

Expiry preflight uses a five-minute window and one bounded refresh. Refresh failures preserve inventory and sync timestamps, with no background refresh or provider retry loop.

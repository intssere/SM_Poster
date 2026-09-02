# Architecture

The application separates commerce, content, creative, workflow, and provider boundaries.

```text
Shopify catalog -> normalized products -> proposals/revisions -> authentic-image creatives
                                                        |             |
                                                        v             v
                                               exact approval -> immutable publication snapshot
                                                                      |
                                                        Pinterest adapter (disabled)
```

Approval v2 binds a decision to the proposal, selected revision (or explicit `original` version), creative, actor, and timestamp. Later selection changes cannot mutate that row.

Publication Identity v2 copies the approved revision, creative, authentic source image, template key/version, text and creative fingerprints, board/account identifiers, destination and UTM URLs, and schedule/provider identifiers into `pin_publications`. Snapshot fields are independent of later proposal, board, URL, selection, or creative changes.

`PublicationIdentityService` is a database-only review boundary. It refuses mismatched identities and duplicate fingerprints and contains no provider/Pinterest client. `PUBLISHING_ENABLED` must remain false.

Task #35 adds a centralized single-admin authentication boundary: operational API routes require a short-lived signed HttpOnly session, unsafe requests require an allowed Origin, and exposed deployments fail closed without server-side auth configuration.
### Pinterest account connection

Task #36 adds a server-side OAuth boundary for account connection only. Tokens are encrypted at rest and never returned to the frontend; no board or Pin write path exists.

Task #37 is complete: read-only board/section snapshots and an authenticated Board Manager are available. Synchronization never creates or modifies Pinterest boards, Pins, schedules, or analytics records.
# Pinterest board sync refresh boundary

Manual board sync checks access-token expiry using a five-minute preflight and may call the existing refresh helper once. Discovery uses the refreshed encrypted state; failures stop all provider calls and reconciliation. No scheduler or provider retry loop exists.

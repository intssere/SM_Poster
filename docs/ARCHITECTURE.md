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

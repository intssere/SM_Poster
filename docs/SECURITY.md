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

## Known gap

Internal proposal APIs do not yet have dedicated authentication/authorization. Address this in a separate security-hardening task before broader operational exposure.

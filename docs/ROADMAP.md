# Roadmap

## Completed

- Shopify catalog sync and deterministic product intelligence
- Controlled proposal generation and authentic-product creative rendering
- Review-only OpenAI text/background foundation with fail-closed hosted behavior
- Immutable revisions, explicit version selection, and read-only comparison previews
- Exact approval binding and immutable publication identity snapshots

## Next recommended task

Pinterest Account Connection/OAuth v1 (Task #36) is complete in scope; next is reviewed board sync design.

## Later phases

- Pinterest OAuth/account onboarding and reviewed board mapping
- Explicit scheduler design with idempotency and `PUBLISH_UNKNOWN` recovery
- Production publishing only after access review, sandbox validation, and a separate enablement decision
- Analytics ingestion and attribution after publication identity is proven end to end

None of these later phases is enabled by Task #34.

Task #35 completed the internal admin authentication boundary. Pinterest OAuth, board management, scheduler, publishing, analytics, and provider calls remain future work and disabled.

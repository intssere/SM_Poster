# Roadmap

## Completed

- Shopify catalog sync and deterministic product intelligence
- Controlled proposal generation and authentic-product creative rendering
- Review-only OpenAI text/background foundation with fail-closed hosted behavior
- Immutable revisions, explicit version selection, and read-only comparison previews
- Exact approval binding and immutable publication identity snapshots
- Pinterest account connection and read-only board/section sync with Board Manager v1

## Next recommended task

Task #35 authentication, Task #36 OAuth, and Task #37 reviewed board sync/Board Manager v1 are complete and merged.

## Later phases

- Separately authorized live-publishing and `pins:write` enablement (explicit authorization required)
- Explicit scheduler design with idempotency and `PUBLISH_UNKNOWN` recovery
- Production publishing only after access review, sandbox validation, and a separate enablement decision
- Analytics ingestion and attribution after publication identity is proven end to end

None of these later phases is enabled by Task #34.

Task #35 completed authentication; Task #36 OAuth and Task #37 read-only board management are implemented. Scheduler, publishing, analytics ingestion, and provider writes remain future work and disabled.

Task #38 is the PRE-MERGE Publisher + Scheduler Foundation: immutable snapshots, durable attempts, bounded due processing, and transactional claims. Live publishing remains disabled pending explicit authorization.

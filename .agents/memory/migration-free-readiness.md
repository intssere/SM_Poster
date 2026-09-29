---
name: Migration-free readiness work
description: Safety boundary for branch-only publishing readiness changes that include pending schema source.
---

When a publishing-readiness branch carries a new migration but migration execution is forbidden, keep the development workflow free of automatic schema upgrades. Treat a read-only DRY_RUN certificate as distinct from authorization for live dispatch; a quota headroom observation does not replace an atomic reservation.

**Why:** A background workflow can restart outside the agent's explicit test steps. An upgrade in its startup command could apply pending schema unexpectedly, and a concurrent worker can invalidate an unlocked quota observation.

**How to apply:** Before staging pending migrations, inspect auto-start commands and ensure no permitted workflow executes upgrades. Keep provider-write and autonomous-activation gates closed. Require a separately approved live phase with an atomic reservation at the claim boundary and real concurrency verification.
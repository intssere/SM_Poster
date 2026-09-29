---
name: GitHub push authorization
description: Why authenticated GitHub reads do not prove that the connected integration can publish branches.
---

GitHub repository reads and reported user push permission can both succeed while the connected integration rejects Git object writes with a permissions error. Local Git HTTPS push can also lack valid credentials. Treat these as separate authorization paths; do not infer write access from successful reads.

**Why:** A repository reported user push access, but the integration could not create a Git blob and the workspace Git remote rejected an HTTPS push. The provider response showed no granted OAuth scopes even though repository write scope was accepted for the operation.

**How to apply:** Before promising that a branch is pushed, verify the remote ref itself. If writes fail, inspect the connection's reauthorization context and effective scopes; request renewed authorization when a matching write scope is available. Never request or expose a raw token.
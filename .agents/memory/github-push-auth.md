---
name: GitHub branch push auth
description: Safe fallback when Git remotes reject a push despite an authenticated GitHub CLI.
---

If a GitHub HTTPS remote rejects a push, check whether the GitHub CLI is already authenticated and has repository write scope. A per-command Git credential helper backed by that CLI can publish the exact local Git commit without exposing or copying credentials. Do not force-push or rebase to work around a moved remote branch.

**Why:** The workspace Git credential helper can be invalid even while the CLI is authorized. A connected GitHub API integration may allow reads but reject Git-object writes; repeated retries or reconnect prompts then do not solve the Git push.

**How to apply:** Check branch ancestry and remote ref first, inspect CLI auth status without revealing its token, then use the CLI's Git credential helper for one non-force push. Verify the remote SHA afterward. If both mechanisms lack permission, stop and ask for access rather than handling credentials directly.
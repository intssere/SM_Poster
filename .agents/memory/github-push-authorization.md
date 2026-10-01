---
name: GitHub push authorization
description: Why authenticated GitHub reads do not prove that the connected integration can publish branches.
---

GitHub repository reads and reported user push permission can both succeed while the connected integration rejects Git object writes with a permissions error. Local Git HTTPS push can also lack valid credentials. Treat these as separate authorization paths; do not infer write access from successful reads.

**Why:** A repository reported user push access, but the integration could not create a Git blob and the workspace Git remote rejected an HTTPS push. Reauthorizing the connection did not change the GitHub App's `contents=write` denial.

**How to apply:** Before promising that a branch is pushed, verify the remote ref itself. If writes fail, inspect the connection's reauthorization context and effective scopes; one renewed authorization may help, but if `contents=write` is still denied, stop retrying and have the repository owner adjust the GitHub App's installation permissions. Never request or expose a raw token.

Managed Git/GitHub CLI authorization can also differ from connector-proxy
authorization. A proxy permission error on PR creation does not establish that
an already configured CLI cannot create the PR.

**Why:** PR creation through a healthy, broadly scoped connection returned an
integration permission denial while managed Git push and GitHub CLI PR creation
both succeeded. Reported scopes are not proof of a particular execution path's
effective permissions.

**How to apply:** When the user has authorized writes, consider an already
configured managed Git/CLI path before concluding that reconnecting is required.
Do not extract credentials. Independently verify the resulting remote ref and
PR, and do not substitute a different account or repository.
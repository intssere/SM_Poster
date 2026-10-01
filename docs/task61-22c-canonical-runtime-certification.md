# Task 61.22C: canonical lineage and supported-runtime certification

## Lineage reconciliation

GitHub canonical `main`, inspected for this task, remains
`a5418e44fc3f7bc352b640948eefeca7ed24a1bb` (tree
`d27529dc83baea33e0b929e0b633aab6cad42c30`).

The original 61.22B commit was based on:

1. Canonical `main` above.
2. `b5b9c0d814bb2a2876a15b3e2611d6f53aced20a`: closed-state checkpoint
   adding explicit canary-disable entries only in `.replit`.
3. `df5232324818144b2cf32931de7d9e86151f9bd7`: publication metadata commit
   with no file/tree changes from its parent. Its tree and the checkpoint tree
   are both `7053e771e7bf768999d79d5d37895347313a7faa`.
4. `2958fbec452d3476d91167dd5887f188ec3e9f4e`: the 61.22B engineering commit.

Therefore the reported immediate parent was correct but was not canonical main.
The checkpoint/publication ancestry predates 61.22B; neither this task nor
61.22B performed that publication.

Reconciliation uses a new branch from canonical main and cherry-picks only
61.22B. Original branch/history remain intact; no rebase of a published branch
or force push is used. Stable patch IDs match before certification-only changes.
The reconciled `.replit` is identical to canonical main. No unrelated checkpoint
overlay enters the PR. The original workspace/runtime is left on 61.22B;
reconciliation is isolated in a separate Git worktree.

## Supported-runtime proof

The dedicated 61.22B CI now also targets the reconciled branch. It selects
Python 3.12 and asserts PostgreSQL 16 binaries. An isolated child imports the
real installed Object Storage SDK and checks its Requires-Python metadata while
denying socket/network operations before the import. It imports classes but
never constructs Client or calls storage. The existing runner's offline
fake-process tests then execute under that same Python 3.12 interpreter.

The focused and relevant broader suites, migration/schema checks, dependency
check, compilation, and no-skip assertion remain in the dedicated workflow.
JUnit and sanitized runtime-certification evidence are uploaded as CI artifacts.
Configured CI is not a claim that hosted CI passed: final job/run evidence must
be inspected separately.

Hosted evidence for certification code commit
`0020a64e07679009e4c0fb7eea780068fff0fdcd`:

- Dedicated workflow push run `36904235694`: SUCCESS.
- Python **3.12.14**, PostgreSQL **16.15**, SDK **1.0.2**.
- Actual SDK import: PASS, Requires-Python `>=3.8.0,<3.13`; network denied,
  no Client construction.
- Focused suites: **149 passed**, zero failures/skips (69.52 seconds).
- Broader regressions: **200 passed**, **28 subtests passed**, zero
  failures/skips (105.39 seconds).
- Dependency check: “No broken requirements found.” Compilation/whitespace,
  real PostgreSQL frozen-head migration checks and no-skip assertion passed.
- PR **191** is open against canonical main; it is not merged and auto-merge
  is not enabled. PR-event and final-HEAD results must also be checked before
  declaring all PR checks green.

## Production-runtime limitation

Canonical `.replit` declares `python-base-3.13`. Production build/run dispatches
generic Python, and startup subsequently uses sys.executable. Neither build,
startup, health, nor source attestation proves the deployed interpreter's version
or SDK compatibility. Installed SDK 1.0.2 declares Python `>=3.8.0,<3.13`.
Python 3.12 CI success therefore does not certify the current deployed runtime.

A separately authorized supported-runtime selection/change and independently
verified deployment would be required to close this gap; no runtime-version,
deployment configuration, or production change is made here.

## Full-suite logging isolation

The first hosted full-suite runs exposed an order-dependent logging assertion:
in-process Alembic fileConfig disables existing loggers before the readiness
logging test. Dedicated readiness suites passed; several full-suite matrix jobs
failed solely because that assertion captured no messages.

The logging test now owns/restores its logger's disabled/propagation state,
selects that logger's INFO capture level, and covers both initially enabled and
disabled states. No application, admission, runner, probe, migration or production
logging configuration changes accompany this test-harness correction. It adds
one focused case: the final expected focused count is 150 rather than the 149
from the earlier code-certification runs above. Broader dedicated counts remain
200 plus 28 subtests. Final hosted results must be verified on the corrected HEAD.

## Scope

No merge, production migration, Publish/deploy, live Object Storage/business
provider call, production/business-data mutation, or scheduler/worker/canary/
permit/publication activation is authorized or performed. GitHub branch/PR/CI
control-plane operations alone are authorized.
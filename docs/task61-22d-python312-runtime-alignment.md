# Replit Python 3.12 runtime alignment — engineering only

## Reviewed source change

Baseline: canonical main `488fdc033a875cb53bfde456b65b7d5466cd80b9`,
tree `f8f6abaa703c748681364ab99016f557c9261f07`.

The Replit module catalog confirms `python-base-3.12` is supported. The sole
functional configuration change replaces `python-base-3.13` with that module.
Module order, Node.js/PostgreSQL selections, Nix packages, deployment commands,
operational gates and all application/test/migration/probe code remain unchanged.

`.replit` SHA256:

- Before: `684ad1cc5cf56bc6de51366d01b83719a7e8af040adc90b5e156468bd811250d`
- After: `f0547da905e85a9fb6b9c009ada5884a1e0a599643bb1c5c2d60c6eb7ce606d0`

## Managed environment and verification

The Replit-managed `.pythonlibs` environment was rebuilt for CPython **3.12.12**.
All **56 managed package versions** were retained exactly; requirements and
dependency constraints were not changed. Installer-generated project scaffolding
and incidental Nix additions were removed rather than introducing a new project
or dependency-management system.

Uvicorn **0.53.0** resolves through the Python 3.12 environment. Object Storage SDK
**1.0.2** declares Python `>=3.8.0,<3.13`; its real import passed with network
operations denied and no client construction.

The installed Uvicorn entry point uses the Nix Python wrapper and needs the
managed site-packages bootstrap in its environment. It reports CPython 3.12.12
both in the normal workspace and in a credential-cleared environment with only
the explicit managed site-packages path restored. Production's existing
`sys.executable -m uvicorn` path also reports 3.12.12 with the plain credential
whitelist. Stripping the bootstrap from the standalone entry point causes an
import error; this is not a failed production interpreter selection.

Local offline verification used existing PostgreSQL **16.9** binaries selected
only in test-process PATH and fresh Unix-socket-only disposable clusters:

- Focused readiness management/runner/admission/orchestration: **150 passed**.
- Existing compatibility regressions: **200 passed + 28 subtests passed**.
- Release-build/provenance and deployment-attestation regressions: **31 passed**.
- All three suites: zero failures/errors/skips.
- Managed dependency integrity, Python 3.12 syntax and patch whitespace: PASS.

Release-build and startup semantics are verified through isolated tests with
synthetic expectations and mocked subprocesses. The actual release build,
startup supervisor and live readiness probe were not run. Hosted CI has not
been run for this change; earlier CI is not presented as validation of its HEAD.

## Provenance and future release boundaries

The changed overlay hash must not be represented as the old certified overlay.
No existing expected canonical/release/overlay settings, credentials or signed
authorizations were read or changed for this engineering work.

This engineering branch also contains documentation/memory records, so it must
not be treated as a `.replit`-only release checkpoint against the old baseline.
The unchanged release preflight requires an independently reviewed canonical
identity and an exact allowed checkpoint overlay. A future release must first
establish its approved canonical commit/tree and release topology, regenerate
source provenance, and independently approve the new overlay hash and any
readiness release pins. No provenance guard is relaxed here.

Build/startup still resolve generic Python and propagate `sys.executable`; the
readiness runner therefore inherits the selected application interpreter. A
workspace version or SDK import does not certify the actual deployed build or
Autoscale interpreter. That requires separately authorized publication and
independent runtime evidence.

Schema **0032 after 0031** and its production startup guard remain unchanged.
Tests certify only disposable schemas, not the production database. A runtime
alignment neither applies 0032 nor grants readiness admission or probe execution.

No production migration, Publish/deploy, live storage/business-provider call,
business-data mutation or scheduler/worker/canary/permit/publication activation
was performed.
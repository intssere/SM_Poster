---
name: Replit runtime-only updates
description: Keep managed runtime/package refreshes from widening a reviewed source overlay.
---

Use the module catalog to select a supported runtime and verify the resolved
interpreter and managed site-packages after installation. Reinstall the existing
reviewed dependency versions rather than implicitly upgrading application packages.

**Why:** A module switch changed the interpreter while dependencies remained in
the old interpreter's site-packages. The managed package installer then generated
a sample project, lockfile and extra Nix entries despite a runtime-only scope.

**How to apply:** Capture managed dependency versions before refreshing. Compare
the managed site-packages, not every globally visible Nix bootstrap distribution.
Inspect the complete tracked/untracked delta after installation; remove only
incidental generated scaffolding and preserve the approved configuration.
Protected Replit configuration must pass the platform replacement validator.
Normalize terminal CRLF output before using it to recreate a byte-reviewed file.

The standalone Uvicorn entry point can use a Nix interpreter shebang and depend
on the managed site-packages bootstrap, unlike a managed Python invocation.

**Why:** Removing every Python search-path entry from an isolated entry-point
check produced an import error even though the normal entry point and the
production-style managed Python invocation both worked.

**How to apply:** For credential-cleared entry-point checks, restore only the
explicit managed site-packages path, not the parent environment. Also test the
production `sys.executable -m uvicorn` invocation separately.

Documentation is not a permissible extra path in a release checkpoint whose
provenance contract allows only the Replit configuration overlay.

**Why:** Engineering documentation and memory are useful, but they alter source
identity and must not silently enter an already-approved overlay-only release.

**How to apply:** Keep engineering approval distinct from release certification.
Do not relax provenance guards; independently establish the future canonical
identity and release topology before publication.
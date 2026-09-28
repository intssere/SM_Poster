---
name: Backend auth tests in Replit
description: Process-level test isolation when workspace authentication settings affect local API tests.
---

Run local-only authentication and origin tests with a process-scoped test environment, not the inherited preview settings. The preview domain makes development count as exposed; inherited allowed origins can exclude localhost; inherited admin configuration changes unconfigured-auth expectations.

**Why:** These environment differences caused otherwise unrelated API tests to report authorization failures even though the opt-in endpoint and its PostgreSQL regressions passed.

**How to apply:** For local-only suites, unset the preview-domain and deployment indicators in the test process. Set a localhost-only allowed origin for tests that require it; unset inherited admin configuration for tests expecting auth to be unconfigured. Do not alter production or persistent workspace configuration to satisfy test assertions.
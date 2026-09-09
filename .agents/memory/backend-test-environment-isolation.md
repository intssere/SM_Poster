---
name: Backend test environment isolation
description: Environment isolation needed for deterministic backend auth and CORS tests in Replit workspaces.
---

Run the backend test suite with Replit deployment indicators and workspace admin credentials removed from the test process. Set the test origin list before application import.

**Why:** Workspace-level `REPLIT_DEV_DOMAIN`, deployment, and admin-auth variables can make test cookies secure or make intentionally unconfigured-auth cases appear configured. Application CORS origins are also captured at import time. This creates false auth/CORS failures even when isolated feature tests pass.

**How to apply:** For full backend pytest runs, unset `REPLIT_DEV_DOMAIN`, `REPLIT_DEPLOYMENT`, workspace admin username/password-hash aliases, and `SESSION_SECRET`; set `AUTH_ALLOWED_ORIGINS` to the origins required by the tests before invoking pytest.
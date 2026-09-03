# Changelog

## Task #34 — Publication Identity v2

- Added Alembic migration `0010` with nullable approval/publication identity columns.
- Bound new approvals to an exact active revision or explicit original version and creative.
- Added immutable publication snapshots for approval, authentic source image, template, fingerprints, board/account, URLs, schedule, and future provider Pin identity.
- Added deterministic duplicate snapshot protection and fail-closed identity validation.
- Preserved readability of historical rows with null v2 identity fields.
- Kept AI and publishing disabled; no provider or Pinterest integration was added.

## Task #35 — Internal Admin Authentication & API Authorization v1

- Added single-admin login/logout/session endpoints, centralized API authorization, restrictive CORS, and Origin/CSRF protection.
- Added a minimal frontend login gate without browser storage tokens.
- No AI, Pinterest, scheduler, publishing, or database migration changes.

## Task #36 — Pinterest Account Connection / OAuth v1

- Added read-only Pinterest OAuth account connection with hashed one-time state, server-side token exchange, encrypted credentials, refresh safety, safe redirects, and mocked provider tests.
- Live requested scopes remain `user_accounts:read`, `boards:read`, and `pins:read`.

## Task #37 — Pinterest Board Sync & Board Manager v1

- Added migrations `0012` and `0013`.
- Added authenticated read-only Pinterest board/section synchronization, connection-level successful-sync state, strict provider metadata validation, five-minute token preflight, and local-only eligibility/routing.
- Added Board Manager UI for reviewing and explicitly configuring persisted board metadata.
- Kept Pinterest writes, publishing, scheduling workers, and analytics disabled.
- PR #15 merged; Issue #14 closed.

## Unreleased — Task #38 Publisher + Scheduler Foundation v1

- Added migration `0014` for publication scheduler/publisher foundations.
- Added provider-independent immutable publication snapshots with PinterestConnection and PinterestBoard binding.
- Added immutable approved content, creative, source-image, external board, title, description, alt text, destination/UTM, and media snapshots.
- Added duplicate publication fingerprint protection.
- Added explicit human scheduling, rescheduling, and cancellation.
- Added timezone-aware UTC scheduling, bounded due discovery, transactional compare-and-set claims, and durable STARTED publication attempts.
- Added unique publication attempt numbering and ordered attempt history.
- Added mockable Pinterest gateway dispatch boundary and conservative outcome classification.
- Added readiness gates and readiness reasons, including publishing, scope, destination, approval, creative, and media boundaries.
- Added safe attempt metadata sanitization and API DTOs that hide request fingerprints and raw provider data.
- Preserved typed `PublicationReconciliationError` through the API boundary.
- Added authenticated publication API coverage and hardened Publications/Scheduler frontend management.
- Focused acceptance baseline: publication API 17, gateway 7, publisher 37, scheduler 11, combined 72, with 1 existing warning.
- Frontend `npm run build`, `tsc -b`, and `vite build` passed.
- Publishing remains disabled; this is not final release certification.

# Changelog

## Task #34 — Publication Identity v2

- Added Alembic migration `0010` with nullable approval/publication identity columns.
- Bound new approvals to an exact active revision (or explicit original version) and creative.
- Added immutable publication snapshots for approval, authentic source image, template, fingerprints, board/account, URLs, schedule, and future provider Pin identity.
- Added deterministic duplicate snapshot protection and fail-closed identity validation.
- Preserved readability of historical rows with null v2 identity fields.
- Added authoritative architecture, project state, roadmap, decisions, handoff, changelog, and security documentation.
- Kept AI and publishing disabled; no provider or Pinterest integration was added.

## Task #35

- Added single-admin login/logout/session endpoints, centralized API authorization, restrictive CORS, and Origin/CSRF protection.
- Added a minimal frontend login gate without browser storage tokens.
- No AI, Pinterest, scheduler, publishing, or database migration changes.

## Task #36

- Added read-only Pinterest OAuth account connection with hashed one-time state and encrypted credentials.

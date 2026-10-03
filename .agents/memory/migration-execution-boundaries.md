---
name: Executable versus managed migrations
description: Why fresh PostgreSQL executable progression must coexist with strict historical managed pre-apply adoption.
---

Support canonical empty PostgreSQL executable upgrades outside development/test without changing the historical managed pre-apply/bookkeeping adoption policy. Do not interpret support for fresh executable schema progression as permission to transform populated readiness evidence in production.

**Why:** A deployment that builds its schema through ordinary Alembic is not a managed Publish pre-apply deployment. An environment-only rejection blocks the former even when its frozen predecessor is exact and empty; removing all restrictions would also authorize a different, populated-evidence transformation that was not requested.

**How to apply:** Gate executable permission on locked, verified predecessor contracts and empty readiness evidence. Preserve the separate exact managed adoption route, frozen contracts, existing nonempty-evidence refusals outside development/test, and existing downgrade restrictions.
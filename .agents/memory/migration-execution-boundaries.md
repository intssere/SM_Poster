---
name: Executable versus managed migrations
description: Why fresh PostgreSQL executable progression must coexist with strict historical managed pre-apply adoption.
---

Support canonical empty PostgreSQL executable upgrades outside development/test without changing the historical managed pre-apply/bookkeeping adoption policy. Do not interpret support for fresh executable schema progression as permission to transform populated readiness evidence in production.

**Why:** A deployment that builds its schema through ordinary Alembic is not a managed Publish pre-apply deployment. An environment-only rejection blocks the former even when its frozen predecessor is exact and empty; removing all restrictions would also authorize a different, populated-evidence transformation that was not requested.

**How to apply:** Gate executable permission on locked, verified predecessor contracts and empty readiness evidence. Preserve the separate exact managed adoption route, frozen contracts, existing nonempty-evidence refusals outside development/test, and existing downgrade restrictions.

Production-state transfer engineering is not authorization to access real
databases, secret key material, storage or providers. A database transfer
certificate must never serve as media/provider readiness evidence or execution
authorization. Historical UNKNOWN attempts and consumed/expired permissions must
retain their historical meaning rather than becoming work to replay.

**Why:** The user explicitly separated provider-free, disposable engineering
from a later authorized production transfer, media certification and provider
readiness phase.

**How to apply:** Keep engineering isolated from attached credentials and
services. Require separate authorization for real transfer or media/provider
checks, and keep runtime execution closed after database-only certification.

Do not assume a managed source and a fresh executable database have identical
catalogs just because their Alembic revisions agree.

**Why:** Historical Phase 0 column cloning loses foreign keys that remain in
the managed source. Revision-only checks or a single shared catalog fingerprint
can therefore reject the real source or miss transfer dependencies.

**How to apply:** Pin source and target contracts separately, preserve the
source's reference integrity during transfer, and do not silently broaden
adoption or rewrite historical migrations to conceal the difference.

Managed SELECT-only export must keep all source guards and rows in one statement;
separate managed callbacks do not establish a shared transaction.

**Why:** The managed production surface permits SELECT on a replica, not a
persistent Python source connection. Chaining calls would silently lose the
cross-table snapshot even if each individual read succeeds.

**How to apply:** Keep the real single-statement snapshot evidence distinct from
the DSN export's repeatable-read transaction. Require independent primary writer
freeze/replica freshness checks before a real cutover.

Do not treat suppressed application printing as confidential capsule transport.

**Why:** A managed callback runtime may journal its raw result independently of
console output. Migration capsules contain opaque ciphertext and other sensitive
records even when the query is read-only.

**How to apply:** Establish an approved private, complete result path into the
offline wrapper before retrieving real source rows; never send a real capsule
through a journaled or rendered tool result merely to avoid needing a DSN.
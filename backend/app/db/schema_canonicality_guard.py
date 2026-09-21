"""Read-only production schema canonicality guard.

This module never repairs or mutates schema. It exits non-zero when a PostgreSQL
database at Alembic head does not match the frozen migration catalog contracts.
"""
from __future__ import annotations

from app.db.migration_adoption import verify_frozen_schema_at_head
from app.db.session import engine


def main() -> int:
    with engine.connect() as connection:
        verify_frozen_schema_at_head(connection)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

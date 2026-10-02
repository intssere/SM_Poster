"""Bookkeeping-only adoption of exact empty managed schema pre-applied at 0033.

No DDL, providers, permits, publishing, or application processes are started.
An already-recorded 0033 is verified without mutation. No executable migration
is run; absent, partial, drifted, populated or historical 0032 states refuse.
"""
from __future__ import annotations

from app.db.migration_adoption import adopt_managed_preapplied_0033
from app.db.session import engine


def main() -> int:
    with engine.begin() as connection:
        adopt_managed_preapplied_0033(connection)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.db import migration_adoption, schema_canonicality_guard


class RevisionConnection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, revisions):
        self.revisions = revisions
        self.queries = []

    def execute(self, query):
        self.queries.append(str(query))
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: self.revisions)
        )


@pytest.mark.parametrize("revisions", [[], ["0029"], ["0030", "0029"]])
def test_revision_check_refuses_any_state_other_than_exact_0030(revisions):
    connection = RevisionConnection(revisions)
    with pytest.raises(migration_adoption.SchemaAdoptionRefused, match="Alembic revision 0030"):
        migration_adoption._require_exact_alembic_revision(connection, "0030")
    assert connection.queries == ["SELECT version_num FROM alembic_version ORDER BY version_num"]


def test_revision_check_accepts_exact_0030_without_writes():
    connection = RevisionConnection(["0030"])
    migration_adoption._require_exact_alembic_revision(connection, "0030")
    assert connection.queries == ["SELECT version_num FROM alembic_version ORDER BY version_num"]


def test_guard_requires_postgresql_before_schema_verification(monkeypatch):
    connection = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    monkeypatch.setattr(
        schema_canonicality_guard.engine, "connect", lambda: nullcontext(connection)
    )
    monkeypatch.setattr(
        schema_canonicality_guard,
        "verify_frozen_schema_at_head",
        lambda *_args, **_kwargs: pytest.fail("non-PostgreSQL must not be accepted"),
    )
    with pytest.raises(RuntimeError, match="requires PostgreSQL"):
        schema_canonicality_guard.main()


def test_guard_checks_exact_0030_before_accepting(monkeypatch):
    connection = RevisionConnection(["0030"])
    monkeypatch.setattr(
        schema_canonicality_guard.engine, "connect", lambda: nullcontext(connection)
    )
    checked = []

    def verify(conn, *, revision):
        checked.append((conn, revision))
        migration_adoption._require_exact_alembic_revision(conn, revision)

    monkeypatch.setattr(schema_canonicality_guard, "verify_frozen_schema_at_head", verify)
    assert schema_canonicality_guard.main() == 0
    assert checked == [(connection, "0030")]
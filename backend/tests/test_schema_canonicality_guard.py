from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.db import migration_adoption, schema_canonicality_guard


class RevisionConnection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, revisions):
        self.revisions = revisions
        self.queries = []

    def exec_driver_sql(self, query):
        self.queries.append(query)

    def execute(self, query):
        self.queries.append(str(query))
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: self.revisions)
        )


@pytest.mark.parametrize("revisions", [[], ["0030"], ["0031", "0030"]])
def test_revision_check_refuses_any_state_other_than_exact_0031(revisions):
    connection = RevisionConnection(revisions)
    with pytest.raises(migration_adoption.SchemaAdoptionRefused, match="Alembic revision 0031"):
        migration_adoption._require_exact_alembic_revision(connection, "0031")
    assert connection.queries == ['SELECT version_num FROM "public"."alembic_version" ORDER BY version_num']


def test_revision_check_accepts_exact_0031_without_writes():
    connection = RevisionConnection(["0031"])
    migration_adoption._require_exact_alembic_revision(connection, "0031")
    assert connection.queries == ['SELECT version_num FROM "public"."alembic_version" ORDER BY version_num']


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


def test_guard_checks_exact_0031_before_accepting(monkeypatch):
    connection = RevisionConnection(["0031"])
    monkeypatch.setattr(
        schema_canonicality_guard.engine, "connect", lambda: nullcontext(connection)
    )
    checked = []

    def verify(conn, *, revision):
        checked.append((conn, revision))
        migration_adoption._require_exact_alembic_revision(conn, revision)

    monkeypatch.setattr(schema_canonicality_guard, "verify_frozen_schema_at_head", verify)
    assert schema_canonicality_guard.main() == 0
    assert checked == [(connection, "0031")]


class ScheduledQuotaInspector:
    def __init__(self):
        self.columns = [
            {"name": "id", "type": migration_adoption.sa.String(36), "nullable": False, "default": None},
            {"name": "publication_id", "type": migration_adoption.sa.String(36), "nullable": False, "default": None},
            {"name": "plan_id", "type": migration_adoption.sa.String(36), "nullable": False, "default": None},
            {"name": "plan_item_id", "type": migration_adoption.sa.String(36), "nullable": False, "default": None},
            {"name": "product_id", "type": migration_adoption.sa.String(36), "nullable": False, "default": None},
            {"name": "vendor_key", "type": migration_adoption.sa.String(255), "nullable": False, "default": None},
            {"name": "board_id", "type": migration_adoption.sa.String(36), "nullable": False, "default": None},
            {"name": "scheduled_for", "type": migration_adoption.sa.Date(), "nullable": False, "default": None},
            {"name": "month_start", "type": migration_adoption.sa.Date(), "nullable": False, "default": None},
            {"name": "reserved_at", "type": migration_adoption.sa.DateTime(timezone=True), "nullable": False, "default": "now()"},
        ]
        self.uniques = [
            {"name": "uq_routine_scheduled_quota_publication", "column_names": ["publication_id"]}
        ]
        self.indexes = [
            {"name": "ix_routine_scheduled_quota_day", "column_names": ["scheduled_for", "product_id", "vendor_key", "board_id"], "unique": False, "dialect_options": {}},
            {"name": "ix_routine_scheduled_quota_month", "column_names": ["month_start"], "unique": False, "dialect_options": {}},
        ]
        self.foreign_keys = [
            {"name": "fk_routine_scheduled_quota_publication", "constrained_columns": ["publication_id"], "referred_table": "pin_publications", "referred_columns": ["id"], "options": {"ondelete": "RESTRICT"}},
            {"name": "fk_routine_scheduled_quota_plan", "constrained_columns": ["plan_id"], "referred_table": "pinterest_portfolio_plans", "referred_columns": ["id"], "options": {"ondelete": "RESTRICT"}},
            {"name": "fk_routine_scheduled_quota_plan_item", "constrained_columns": ["plan_item_id"], "referred_table": "pinterest_portfolio_plan_items", "referred_columns": ["id"], "options": {"ondelete": "RESTRICT"}},
            {"name": "fk_routine_scheduled_quota_product", "constrained_columns": ["product_id"], "referred_table": "products", "referred_columns": ["id"], "options": {"ondelete": "RESTRICT"}},
            {"name": "fk_routine_scheduled_quota_board", "constrained_columns": ["board_id"], "referred_table": "boards", "referred_columns": ["id"], "options": {"ondelete": "RESTRICT"}},
        ]
        self.primary_key = {"name": "routine_scheduled_quota_reservations_pkey", "constrained_columns": ["id"]}

    def get_table_names(self, schema=None):
        return ["routine_scheduled_quota_reservations"]

    def get_columns(self, table, schema=None):
        return self.columns

    def get_pk_constraint(self, table, schema=None):
        return self.primary_key

    def get_unique_constraints(self, table, schema=None):
        return self.uniques

    def get_indexes(self, table, schema=None):
        return self.indexes

    def get_foreign_keys(self, table, schema=None):
        return self.foreign_keys

    def get_check_constraints(self, table, schema=None):
        return []


@pytest.fixture
def scheduled_quota_inspector(monkeypatch):
    inspector = ScheduledQuotaInspector()
    monkeypatch.setattr(migration_adoption.sa, "inspect", lambda _connection: inspector)
    return inspector


def test_0031_scheduled_quota_contract_accepts_exact_read_only_schema(scheduled_quota_inspector):
    migration_adoption._require_0031_scheduled_quota_schema(object())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda inspector: inspector.columns.pop(), "column contract mismatch"),
        (lambda inspector: inspector.columns[5].update(type=migration_adoption.sa.String(254)), "length mismatch"),
        (lambda inspector: inspector.columns[0].update(nullable=True), "nullability mismatch"),
        (lambda inspector: inspector.columns[9].update(default=None), "default mismatch"),
        (lambda inspector: inspector.columns[9].update(type=migration_adoption.sa.DateTime(timezone=False)), "timezone mismatch"),
        (lambda inspector: inspector.primary_key.update(name="unexpected_pkey"), "primary key mismatch"),
        (lambda inspector: inspector.uniques[0].update(column_names=["plan_id"]), "unique contract mismatch"),
        (lambda inspector: inspector.indexes[0].update(column_names=["scheduled_for", "product_id"]), "index contract mismatch"),
        (lambda inspector: inspector.foreign_keys[0].update(options={"ondelete": "CASCADE"}), "foreign key contract mismatch"),
        (lambda inspector: inspector.foreign_keys.append({"name": "extra_fk", "constrained_columns": ["id"], "referred_table": "products", "referred_columns": ["id"], "options": {}}), "foreign key contract mismatch"),
    ],
)
def test_0031_scheduled_quota_contract_refuses_schema_drift(
    scheduled_quota_inspector, mutation, message
):
    mutation(scheduled_quota_inspector)
    with pytest.raises(migration_adoption.SchemaAdoptionRefused, match=message):
        migration_adoption._require_0031_scheduled_quota_schema(object())
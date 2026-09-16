from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa


MIGRATION_PATH = Path(__file__).parents[1] / "alembic" / "versions" / "0018_routine_pinterest_publishing_foundation.py"
spec = importlib.util.spec_from_file_location("migration_0018", MIGRATION_PATH)
assert spec and spec.loader
m0018 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m0018)


def _type(kind, detail):
    if kind == "string":
        return sa.String(detail)
    if kind == "datetime":
        return sa.DateTime(timezone=bool(detail))
    if kind == "json":
        return sa.JSON()
    if kind == "integer":
        return sa.Integer()
    raise AssertionError(kind)


def _default_value(semantic):
    if semantic is None:
        return None
    if semantic == "now":
        return "now()"
    if semantic == "0":
        return "0"
    return f"'{semantic.upper() if semantic in {'active', 'paused', 'running'} else semantic}'::character varying"


class FakeInspector:
    def __init__(self, mode: str):
        self.tables = set(m0018.TARGET_TABLES)
        self.columns = {}
        for table, definitions in m0018.EXPECTED_COLUMNS.items():
            rows = []
            for name, (kind, detail, nullable, final_default, adoption_default) in definitions.items():
                semantic = final_default if mode == "final" else adoption_default
                rows.append(
                    {
                        "name": name,
                        "type": _type(kind, detail),
                        "nullable": nullable,
                        "default": _default_value(semantic),
                        "identity": None,
                        "computed": None,
                    }
                )
            self.columns[table] = rows

        self.pks = {table: {"constrained_columns": ["id"]} for table in m0018.TARGET_TABLES}
        self.fks = {}
        for table, signatures in m0018.EXPECTED_FKS.items():
            self.fks[table] = [
                {
                    "constrained_columns": list(local),
                    "referred_table": remote_table,
                    "referred_columns": list(remote_columns),
                    "options": {"ondelete": ondelete},
                }
                for local, remote_table, remote_columns, ondelete in signatures
            ]

        self.checks = {}
        for table, definitions in m0018.EXPECTED_CHECKS.items():
            self.checks[table] = [
                {"name": name, "sqltext": " ".join(tokens)} for name, tokens in definitions.items()
            ]

        self.indexes = {}
        for table, definitions in m0018.EXPECTED_INDEXES.items():
            rows = []
            for name, (columns, unique, predicate) in definitions.items():
                effective_unique = unique
                if mode == "adoption" and table == "routine_attempt_boundaries" and name == m0018.ATTEMPT_INDEX:
                    effective_unique = True
                rows.append(
                    {
                        "name": name,
                        "column_names": list(columns),
                        "unique": effective_unique,
                        "dialect_options": {"postgresql_where": f"status = '{predicate.upper()}'"} if predicate else {},
                    }
                )
            self.indexes[table] = rows

        self.uniques = {table: [] for table in m0018.TARGET_TABLES}
        if mode == "final":
            self.uniques["routine_attempt_boundaries"] = [
                {"name": m0018.ATTEMPT_UNIQUE_CONSTRAINT, "column_names": ["attempt_id"]}
            ]
            # Model the PostgreSQL backing index as an inspector duplicate so
            # explicit-index validation ignores it.
            self.indexes["routine_attempt_boundaries"].append(
                {
                    "name": m0018.ATTEMPT_UNIQUE_CONSTRAINT,
                    "column_names": ["attempt_id"],
                    "unique": True,
                    "duplicates_constraint": m0018.ATTEMPT_UNIQUE_CONSTRAINT,
                    "dialect_options": {},
                }
            )

    def clone(self):
        return copy.deepcopy(self)

    def get_table_names(self):
        return sorted(self.tables)

    def get_columns(self, table):
        return copy.deepcopy(self.columns[table])

    def get_pk_constraint(self, table):
        return copy.deepcopy(self.pks[table])

    def get_foreign_keys(self, table):
        return copy.deepcopy(self.fks[table])

    def get_check_constraints(self, table):
        return copy.deepcopy(self.checks[table])

    def get_indexes(self, table):
        return copy.deepcopy(self.indexes[table])

    def get_unique_constraints(self, table):
        return copy.deepcopy(self.uniques[table])

    def set_default(self, table, column, value):
        for item in self.columns[table]:
            if item["name"] == column:
                item["default"] = str(value)
                return
        raise AssertionError((table, column))

    def add_unique_constraint(self, name, table, columns):
        self.uniques[table].append({"name": name, "column_names": list(columns)})
        self.indexes[table].append(
            {
                "name": name,
                "column_names": list(columns),
                "unique": True,
                "duplicates_constraint": name,
                "dialect_options": {},
            }
        )

    def drop_index(self, name, table):
        self.indexes[table] = [item for item in self.indexes[table] if item["name"] != name]

    def add_index(self, name, table, columns, unique=False, **kwargs):
        options = {}
        where = kwargs.get("postgresql_where") or kwargs.get("sqlite_where")
        if where is not None:
            options["postgresql_where"] = str(where)
        self.indexes[table].append(
            {"name": name, "column_names": list(columns), "unique": bool(unique), "dialect_options": options}
        )


class FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class FakeBind:
    def __init__(self, inspector: FakeInspector, duplicates=False):
        self.inspector = inspector
        self.duplicates = duplicates
        self.dialect = SimpleNamespace(name="postgresql")

    def execute(self, statement):
        assert "GROUP BY attempt_id" in str(statement)
        return FakeScalarResult(self.duplicates)


class MutatingOp:
    def __init__(self, inspector: FakeInspector):
        self.inspector = inspector
        self.calls = []

    def alter_column(self, table, column, **kwargs):
        self.calls.append(("alter_column", table, column))
        self.inspector.set_default(table, column, kwargs["server_default"])

    def create_unique_constraint(self, name, table, columns):
        self.calls.append(("create_unique_constraint", name, table, tuple(columns)))
        self.inspector.add_unique_constraint(name, table, columns)

    def drop_index(self, name, table_name=None, **kwargs):
        self.calls.append(("drop_index", name, table_name))
        self.inspector.drop_index(name, table_name)

    def create_index(self, name, table, columns, unique=False, **kwargs):
        self.calls.append(("create_index", name, table, tuple(columns), bool(unique)))
        self.inspector.add_index(name, table, columns, unique=unique, **kwargs)


class RecordingOp:
    def __init__(self):
        self.tables = {}
        self.indexes = []

    def create_table(self, name, *items, **kwargs):
        self.tables[name] = list(items)

    def create_index(self, name, table, columns, unique=False, **kwargs):
        self.indexes.append((name, table, tuple(columns), bool(unique), kwargs))


def _patch_inspect(monkeypatch, inspector):
    monkeypatch.setattr(m0018.sa, "inspect", lambda bind: bind.inspector)


def _column(inspector, table, name):
    return next(item for item in inspector.columns[table] if item["name"] == name)


def test_audited_adoption_contract_is_accepted(monkeypatch):
    inspector = FakeInspector("adoption")
    bind = FakeBind(inspector)
    _patch_inspect(monkeypatch, inspector)
    m0018._validate_postgresql_contract(bind, "adoption")


def test_canonical_final_contract_is_accepted(monkeypatch):
    inspector = FakeInspector("final")
    bind = FakeBind(inspector)
    _patch_inspect(monkeypatch, inspector)
    m0018._validate_postgresql_contract(bind, "final")


@pytest.mark.parametrize(
    "mutator",
    [
        lambda i: i.tables.remove("routine_publishing_runs"),
        lambda i: _column(i, "routine_dispatch_permits", "status").update(nullable=True),
        lambda i: _column(i, "routine_dispatch_permits", "status").update(default="'ACTIVE'"),
        lambda i: i.fks["routine_dispatch_permits"][0]["options"].update(ondelete="CASCADE"),
        lambda i: i.checks["routine_publishing_runs"][0].update(sqltext="status running failed blocked"),
        lambda i: i.indexes["routine_dispatch_permits"][2]["dialect_options"].update(postgresql_where="status = 'CONSUMED'"),
        lambda i: i.indexes["routine_dispatch_permits"].append(
            {"name": "unexpected_index", "column_names": ["status"], "unique": False, "dialect_options": {}}
        ),
        lambda i: i.uniques["routine_attempt_boundaries"].append(
            {"name": "unexpected_unique", "column_names": ["attempt_id"]}
        ),
    ],
)
def test_adoption_fails_closed_on_unexpected_schema_drift(monkeypatch, mutator):
    inspector = FakeInspector("adoption")
    mutator(inspector)
    bind = FakeBind(inspector)
    _patch_inspect(monkeypatch, inspector)
    with pytest.raises(RuntimeError, match="0018 adoption refused"):
        m0018._validate_postgresql_contract(bind, "adoption")


def test_duplicate_attempt_ids_block_adoption_before_any_ddl(monkeypatch):
    inspector = FakeInspector("adoption")
    bind = FakeBind(inspector, duplicates=True)
    fake_op = MutatingOp(inspector)
    _patch_inspect(monkeypatch, inspector)
    monkeypatch.setattr(m0018, "op", fake_op)

    with pytest.raises(RuntimeError, match="duplicate attempt_id"):
        m0018._adopt_audited_postgresql_schema(bind)
    assert fake_op.calls == []


def test_adoption_corrects_only_audited_drift_and_converges_to_final_contract(monkeypatch):
    inspector = FakeInspector("adoption")
    bind = FakeBind(inspector, duplicates=False)
    fake_op = MutatingOp(inspector)
    _patch_inspect(monkeypatch, inspector)
    monkeypatch.setattr(m0018, "op", fake_op)

    m0018._adopt_audited_postgresql_schema(bind)
    m0018._validate_postgresql_contract(bind, "final")

    alters = [call for call in fake_op.calls if call[0] == "alter_column"]
    assert len(alters) == 12
    assert ("create_unique_constraint", m0018.ATTEMPT_UNIQUE_CONSTRAINT, "routine_attempt_boundaries", ("attempt_id",)) in fake_op.calls
    assert ("drop_index", m0018.ATTEMPT_INDEX, "routine_attempt_boundaries") in fake_op.calls
    assert ("create_index", m0018.ATTEMPT_INDEX, "routine_attempt_boundaries", ("attempt_id",), False) in fake_op.calls


def test_fresh_creation_declares_same_final_defaults_uniqueness_and_indexes(monkeypatch):
    recorder = RecordingOp()
    monkeypatch.setattr(m0018, "op", recorder)
    m0018._create_fresh_schema()

    assert set(recorder.tables) == set(m0018.TARGET_TABLES)

    for table, definitions in m0018.EXPECTED_COLUMNS.items():
        actual_columns = {item.name: item for item in recorder.tables[table] if isinstance(item, sa.Column)}
        assert set(actual_columns) == set(definitions)
        for name, (_, _, _, final_default, _) in definitions.items():
            column = actual_columns[name]
            actual_default = None if column.server_default is None else m0018._default_semantic(column.server_default.arg)
            assert actual_default == final_default

    attempt_constraints = [
        item for item in recorder.tables["routine_attempt_boundaries"] if isinstance(item, sa.UniqueConstraint)
    ]
    assert len(attempt_constraints) == 1
    assert attempt_constraints[0].name == m0018.ATTEMPT_UNIQUE_CONSTRAINT
    assert tuple(column.name for column in attempt_constraints[0].columns) == ("attempt_id",)

    attempt_indexes = [item for item in recorder.indexes if item[1] == "routine_attempt_boundaries"]
    explicit_attempt = next(item for item in attempt_indexes if item[0] == m0018.ATTEMPT_INDEX)
    assert explicit_attempt[2] == ("attempt_id",)
    assert explicit_attempt[3] is False


def test_partial_table_presence_is_rejected_before_fresh_or_adoption(monkeypatch):
    inspector = FakeInspector("adoption")
    inspector.tables.remove("routine_attempt_boundaries")
    bind = FakeBind(inspector)
    _patch_inspect(monkeypatch, inspector)
    monkeypatch.setattr(m0018, "op", SimpleNamespace(get_bind=lambda: bind))

    with pytest.raises(RuntimeError, match="only some routine-publishing tables exist"):
        m0018.upgrade()

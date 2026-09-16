"""Controlled routine Buffer-mediated Pinterest publishing foundation.

Revision 0018 supports two fail-closed upgrade paths:

* fresh 0017 databases, where none of the routine-publishing tables exist;
* the exact audited Replit development schema that already contains the four
  routine-publishing tables but is still stamped at 0017.

The adoption path validates the complete known structure before changing
anything.  It corrects only the audited drift: missing server defaults and the
attempt_id uniqueness representation.  Any other drift aborts the migration.
"""
from __future__ import annotations

from typing import Any

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

TARGET_TABLES = (
    "routine_dispatch_permits",
    "routine_publishing_control",
    "routine_publishing_runs",
    "routine_attempt_boundaries",
)
ATTEMPT_UNIQUE_CONSTRAINT = "uq_routine_attempt_boundaries_attempt_id"
ATTEMPT_INDEX = "ix_routine_attempt_boundaries_attempt_id"

# name -> (kind, length/timezone, nullable, final default semantic, adoption default semantic)
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, Any, bool, str | None, str | None]]] = {
    "routine_dispatch_permits": {
        "id": ("string", 36, False, None, None),
        "publication_id": ("string", 36, False, None, None),
        "dispatch_provider": ("string", 40, False, "buffer", None),
        "approval_id": ("string", 36, False, None, None),
        "pinterest_board_record_id": ("string", 36, False, None, None),
        "publication_fingerprint": ("string", 64, False, None, None),
        "request_fingerprint": ("string", 64, False, None, None),
        "scheduled_for_snapshot": ("datetime", True, False, None, None),
        "quality_policy_version": ("string", 80, False, None, None),
        "quality_snapshot": ("json", None, False, None, None),
        "duplicate_snapshot": ("json", None, False, None, None),
        "readiness_snapshot": ("json", None, False, None, None),
        "authorized_by": ("string", 255, False, None, None),
        "authorized_at": ("datetime", True, False, None, None),
        "expires_at": ("datetime", True, False, None, None),
        "status": ("string", 20, False, "active", None),
        "consumed_at": ("datetime", True, True, None, None),
        "revoked_at": ("datetime", True, True, None, None),
        "revoked_by": ("string", 255, True, None, None),
        "revoke_reason": ("string", 255, True, None, None),
        "created_at": ("datetime", True, False, "now", "now"),
    },
    "routine_publishing_control": {
        "id": ("string", 36, False, None, None),
        "state": ("string", 20, False, "paused", None),
        "pause_reason": ("string", 255, True, None, None),
        "paused_at": ("datetime", True, True, None, None),
        "paused_by": ("string", 255, True, None, None),
        "last_unknown_publication_id": ("string", 36, True, None, None),
        "updated_at": ("datetime", True, False, "now", "now"),
    },
    "routine_publishing_runs": {
        "id": ("string", 36, False, None, None),
        "mode": ("string", 20, False, None, None),
        "started_at": ("datetime", True, False, None, None),
        "heartbeat_at": ("datetime", True, True, None, None),
        "completed_at": ("datetime", True, True, None, None),
        "status": ("string", 20, False, "running", None),
        "scanned": ("integer", None, False, "0", None),
        "eligible": ("integer", None, False, "0", None),
        "skipped": ("integer", None, False, "0", None),
        "claimed": ("integer", None, False, "0", None),
        "dispatched": ("integer", None, False, "0", None),
        "published": ("integer", None, False, "0", None),
        "failed": ("integer", None, False, "0", None),
        "unknown": ("integer", None, False, "0", None),
        "error_code": ("string", 100, True, None, None),
        "metadata_json": ("json", None, False, None, None),
    },
    "routine_attempt_boundaries": {
        "id": ("string", 36, False, None, None),
        "attempt_id": ("string", 36, False, None, None),
        "publication_id": ("string", 36, False, None, None),
        "routine_dispatch_permit_id": ("string", 36, False, None, None),
        "claimed_at": ("datetime", True, False, None, None),
        "provider_mutation_started_at": ("datetime", True, True, None, None),
        "safe_metadata": ("json", None, False, None, None),
        "created_at": ("datetime", True, False, "now", "now"),
    },
}

EXPECTED_FKS = {
    "routine_dispatch_permits": {
        (("publication_id",), "pin_publications", ("id",), "RESTRICT"),
        (("approval_id",), "pin_approvals", ("id",), "RESTRICT"),
        (("pinterest_board_record_id",), "pinterest_boards", ("id",), "RESTRICT"),
    },
    "routine_publishing_control": set(),
    "routine_publishing_runs": set(),
    "routine_attempt_boundaries": {
        (("attempt_id",), "publication_attempts", ("id",), "RESTRICT"),
        (("publication_id",), "pin_publications", ("id",), "RESTRICT"),
        (("routine_dispatch_permit_id",), "routine_dispatch_permits", ("id",), "RESTRICT"),
    },
}

EXPECTED_CHECKS = {
    "routine_dispatch_permits": {
        "ck_routine_dispatch_permit_provider": ("dispatch_provider", "buffer"),
        "ck_routine_dispatch_permit_status": ("status", "active", "consumed", "revoked", "expired"),
    },
    "routine_publishing_control": {
        "ck_routine_publishing_control_state": ("state", "paused", "dry_run", "live"),
    },
    "routine_publishing_runs": {
        "ck_routine_publishing_run_status": ("status", "running", "succeeded", "failed", "blocked"),
    },
    "routine_attempt_boundaries": {},
}

# index name -> (columns, unique, predicate token or None)
EXPECTED_INDEXES = {
    "routine_dispatch_permits": {
        "ix_routine_dispatch_permits_publication_id": (("publication_id",), False, None),
        "ix_routine_dispatch_permit_expires_at": (("expires_at",), False, None),
        "uq_routine_dispatch_permit_active": (("publication_id",), True, "active"),
    },
    "routine_publishing_control": {},
    "routine_publishing_runs": {
        "ix_routine_publishing_run_started_at": (("started_at",), False, None),
        "uq_routine_publishing_run_running": (("status",), True, "running"),
    },
    "routine_attempt_boundaries": {
        ATTEMPT_INDEX: (("attempt_id",), False, None),
        "ix_routine_attempt_boundaries_publication_id": (("publication_id",), False, None),
        "ix_routine_attempt_boundaries_routine_dispatch_permit_id": (("routine_dispatch_permit_id",), False, None),
        "ix_routine_attempt_boundaries_provider_mutation_started_at": (("provider_mutation_started_at",), False, None),
    },
}


def _fail(message: str) -> None:
    raise RuntimeError(f"0018 adoption refused: {message}")


def _default_semantic(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    while len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        text = text[1:-1].strip()
    if "::" in text:
        text = text.split("::", 1)[0].strip()
    if text in {"now()", "current_timestamp", "current_timestamp()"}:
        return "now"
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        text = text[1:-1]
    return text


def _type_semantic(type_: Any) -> tuple[str, Any]:
    if isinstance(type_, sa.JSON):
        return ("json", None)
    if isinstance(type_, sa.DateTime):
        return ("datetime", bool(type_.timezone))
    if isinstance(type_, sa.String):
        return ("string", type_.length)
    if isinstance(type_, sa.Integer):
        return ("integer", None)
    return (type(type_).__name__.lower(), None)


def _index_predicate(index: dict[str, Any]) -> str:
    options = index.get("dialect_options") or {}
    value = options.get("postgresql_where")
    return "" if value is None else str(value).lower()


def _validate_columns(inspector: Any, table: str, mode: str) -> None:
    expected = EXPECTED_COLUMNS[table]
    actual_list = inspector.get_columns(table)
    actual = {column["name"]: column for column in actual_list}
    if set(actual) != set(expected):
        _fail(f"{table} column set differs")
    for name, (kind, detail, nullable, final_default, adoption_default) in expected.items():
        column = actual[name]
        if _type_semantic(column["type"]) != (kind, detail):
            _fail(f"{table}.{name} type differs")
        if bool(column.get("nullable")) != nullable:
            _fail(f"{table}.{name} nullability differs")
        if column.get("identity") is not None or column.get("computed") is not None:
            _fail(f"{table}.{name} unexpectedly generated")
        expected_default = final_default if mode == "final" else adoption_default
        if _default_semantic(column.get("default")) != expected_default:
            _fail(f"{table}.{name} server default differs")


def _validate_pk(inspector: Any, table: str) -> None:
    pk = inspector.get_pk_constraint(table) or {}
    if tuple(pk.get("constrained_columns") or ()) != ("id",):
        _fail(f"{table} primary key differs")


def _validate_fks(inspector: Any, table: str) -> None:
    actual = set()
    for fk in inspector.get_foreign_keys(table):
        options = fk.get("options") or {}
        actual.add(
            (
                tuple(fk.get("constrained_columns") or ()),
                fk.get("referred_table"),
                tuple(fk.get("referred_columns") or ()),
                str(options.get("ondelete") or "").upper(),
            )
        )
    if actual != EXPECTED_FKS[table]:
        _fail(f"{table} foreign keys differ")


def _validate_checks(inspector: Any, table: str) -> None:
    checks = {item.get("name"): str(item.get("sqltext") or "").lower() for item in inspector.get_check_constraints(table)}
    expected = EXPECTED_CHECKS[table]
    if set(checks) != set(expected):
        _fail(f"{table} check constraints differ")
    for name, tokens in expected.items():
        sql = checks[name]
        if any(token.lower() not in sql for token in tokens):
            _fail(f"{table} check constraint {name} differs")


def _explicit_indexes(inspector: Any, table: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index in inspector.get_indexes(table):
        name = index.get("name")
        # PostgreSQL can expose the backing index of a UNIQUE constraint. It is
        # not one of the explicit indexes managed by this migration.
        if index.get("duplicates_constraint"):
            continue
        if name == ATTEMPT_UNIQUE_CONSTRAINT:
            continue
        result[name] = index
    return result


def _validate_indexes(inspector: Any, table: str, mode: str) -> None:
    actual = _explicit_indexes(inspector, table)
    expected = EXPECTED_INDEXES[table]
    if set(actual) != set(expected):
        _fail(f"{table} index set differs")
    for name, (columns, final_unique, predicate_token) in expected.items():
        index = actual[name]
        expected_unique = final_unique
        if mode == "adoption" and table == "routine_attempt_boundaries" and name == ATTEMPT_INDEX:
            expected_unique = True
        if tuple(index.get("column_names") or ()) != columns:
            _fail(f"{table} index {name} columns differ")
        if bool(index.get("unique")) != expected_unique:
            _fail(f"{table} index {name} uniqueness differs")
        predicate = _index_predicate(index)
        if predicate_token is None:
            if predicate:
                _fail(f"{table} index {name} unexpectedly partial")
        elif "status" not in predicate or predicate_token not in predicate:
            _fail(f"{table} index {name} predicate differs")


def _validate_unique_constraints(inspector: Any, mode: str) -> None:
    uniques = inspector.get_unique_constraints("routine_attempt_boundaries")
    relevant = [
        item for item in uniques
        if tuple(item.get("column_names") or item.get("constrained_columns") or ()) == ("attempt_id",)
    ]
    if mode == "adoption":
        if relevant:
            _fail("attempt_id unique constraint already exists in unexpected adoption state")
        return
    if len(relevant) != 1:
        _fail("canonical attempt_id unique constraint missing or duplicated")
    if relevant[0].get("name") != ATTEMPT_UNIQUE_CONSTRAINT:
        _fail("canonical attempt_id unique constraint name differs")


def _validate_postgresql_contract(bind: Any, mode: str) -> None:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    present = set(TARGET_TABLES) & tables
    if present != set(TARGET_TABLES):
        _fail("routine table presence is partial")
    for table in TARGET_TABLES:
        _validate_columns(inspector, table, mode)
        _validate_pk(inspector, table)
        _validate_fks(inspector, table)
        _validate_checks(inspector, table)
        _validate_indexes(inspector, table, mode)
    _validate_unique_constraints(inspector, mode)


def _has_duplicate_attempt_ids(bind: Any) -> bool:
    statement = sa.text(
        "SELECT EXISTS ("
        "SELECT 1 FROM routine_attempt_boundaries "
        "GROUP BY attempt_id HAVING count(*) > 1"
        ")"
    )
    return bool(bind.execute(statement).scalar())


def _create_fresh_schema() -> None:
    op.create_table(
        "routine_dispatch_permits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("pin_publications.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("dispatch_provider", sa.String(40), nullable=False, server_default="buffer"),
        sa.Column("approval_id", sa.String(36), sa.ForeignKey("pin_approvals.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("pinterest_board_record_id", sa.String(36), sa.ForeignKey("pinterest_boards.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("publication_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("scheduled_for_snapshot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_policy_version", sa.String(80), nullable=False),
        sa.Column("quality_snapshot", sa.JSON(), nullable=False),
        sa.Column("duplicate_snapshot", sa.JSON(), nullable=False),
        sa.Column("readiness_snapshot", sa.JSON(), nullable=False),
        sa.Column("authorized_by", sa.String(255), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", sa.String(255)),
        sa.Column("revoke_reason", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("dispatch_provider = 'buffer'", name="ck_routine_dispatch_permit_provider"),
        sa.CheckConstraint("status IN ('ACTIVE','CONSUMED','REVOKED','EXPIRED')", name="ck_routine_dispatch_permit_status"),
    )
    op.create_index("ix_routine_dispatch_permits_publication_id", "routine_dispatch_permits", ["publication_id"])
    op.create_index("ix_routine_dispatch_permit_expires_at", "routine_dispatch_permits", ["expires_at"])
    op.create_index(
        "uq_routine_dispatch_permit_active", "routine_dispatch_permits", ["publication_id"], unique=True,
        sqlite_where=sa.text("status = 'ACTIVE'"), postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "routine_publishing_control",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="PAUSED"),
        sa.Column("pause_reason", sa.String(255)),
        sa.Column("paused_at", sa.DateTime(timezone=True)),
        sa.Column("paused_by", sa.String(255)),
        sa.Column("last_unknown_publication_id", sa.String(36)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("state IN ('PAUSED','DRY_RUN','LIVE')", name="ck_routine_publishing_control_state"),
    )

    op.create_table(
        "routine_publishing_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False, server_default="RUNNING"),
        sa.Column("scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligible", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unknown", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(100)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("status IN ('RUNNING','SUCCEEDED','FAILED','BLOCKED')", name="ck_routine_publishing_run_status"),
    )
    op.create_index("ix_routine_publishing_run_started_at", "routine_publishing_runs", ["started_at"])
    op.create_index(
        "uq_routine_publishing_run_running", "routine_publishing_runs", ["status"], unique=True,
        sqlite_where=sa.text("status = 'RUNNING'"), postgresql_where=sa.text("status = 'RUNNING'"),
    )

    op.create_table(
        "routine_attempt_boundaries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("attempt_id", sa.String(36), sa.ForeignKey("publication_attempts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("pin_publications.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("routine_dispatch_permit_id", sa.String(36), sa.ForeignKey("routine_dispatch_permits.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_mutation_started_at", sa.DateTime(timezone=True)),
        sa.Column("safe_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("attempt_id", name=ATTEMPT_UNIQUE_CONSTRAINT),
    )
    op.create_index(ATTEMPT_INDEX, "routine_attempt_boundaries", ["attempt_id"], unique=False)
    op.create_index("ix_routine_attempt_boundaries_publication_id", "routine_attempt_boundaries", ["publication_id"])
    op.create_index("ix_routine_attempt_boundaries_routine_dispatch_permit_id", "routine_attempt_boundaries", ["routine_dispatch_permit_id"])
    op.create_index("ix_routine_attempt_boundaries_provider_mutation_started_at", "routine_attempt_boundaries", ["provider_mutation_started_at"])


def _adopt_audited_postgresql_schema(bind: Any) -> None:
    _validate_postgresql_contract(bind, "adoption")
    if _has_duplicate_attempt_ids(bind):
        _fail("duplicate attempt_id values exist")

    op.alter_column("routine_dispatch_permits", "dispatch_provider", server_default=sa.text("'buffer'"))
    op.alter_column("routine_dispatch_permits", "status", server_default=sa.text("'ACTIVE'"))
    op.alter_column("routine_publishing_control", "state", server_default=sa.text("'PAUSED'"))
    op.alter_column("routine_publishing_runs", "status", server_default=sa.text("'RUNNING'"))
    for column in ("scanned", "eligible", "skipped", "claimed", "dispatched", "published", "failed", "unknown"):
        op.alter_column("routine_publishing_runs", column, server_default=sa.text("0"))

    # Keep uniqueness continuously enforced: establish the canonical UNIQUE
    # constraint first, then replace the audited unique named index with the
    # canonical non-unique lookup index.
    op.create_unique_constraint(ATTEMPT_UNIQUE_CONSTRAINT, "routine_attempt_boundaries", ["attempt_id"])
    op.drop_index(ATTEMPT_INDEX, table_name="routine_attempt_boundaries")
    op.create_index(ATTEMPT_INDEX, "routine_attempt_boundaries", ["attempt_id"], unique=False)

    _validate_postgresql_contract(bind, "final")


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    present = set(TARGET_TABLES) & tables

    if not present:
        _create_fresh_schema()
        if bind.dialect.name == "postgresql":
            _validate_postgresql_contract(bind, "final")
        return

    if present != set(TARGET_TABLES):
        _fail("only some routine-publishing tables exist")

    if bind.dialect.name != "postgresql":
        _fail("pre-created schema adoption is supported only for PostgreSQL")

    _adopt_audited_postgresql_schema(bind)


def downgrade():
    op.drop_table("routine_attempt_boundaries")
    op.drop_table("routine_publishing_runs")
    op.drop_table("routine_publishing_control")
    op.drop_table("routine_dispatch_permits")

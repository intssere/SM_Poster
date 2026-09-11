"""Durable server-owned Buffer pilot activation."""
from alembic import op
import sqlalchemy as sa
import re

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def _column_contract(bind, table, contracts):
    cols = {c["name"]: c for c in sa.inspect(bind).get_columns(table)}
    timestamp_columns = {"activated_at", "expires_at", "consumed_at", "revoked_at"}
    for name, (length, nullable, default_required) in contracts.items():
        c = cols.get(name)
        if c is None or c["nullable"] is not nullable:
            raise RuntimeError("0017 schema contract mismatch: " + name)
        if length is not None and (
            not isinstance(c["type"], sa.String)
            or getattr(c["type"], "length", None) != length
        ):
            raise RuntimeError("0017 type contract mismatch: " + name)
        if name in timestamp_columns and not isinstance(c["type"], sa.DateTime):
            raise RuntimeError("0017 timestamp contract mismatch: " + name)
        if default_required:
            default = re.sub(r"\s+", "", str(c.get("default") or "").lower())
            default = re.sub(r"::(?:text|timestamp(?:with(?:out)?timezone)?)$", "", default)
            if default not in {"now()", "current_timestamp"}:
                raise RuntimeError("0017 default contract mismatch: " + name)


def _fk_contract(bind, table, expected):
    actual = sa.inspect(bind).get_foreign_keys(table)
    found = {(tuple(x["constrained_columns"]), x["referred_table"],
              tuple(x["referred_columns"]), (x.get("options") or {}).get("ondelete", "").upper())
             for x in actual}
    for item in expected:
        if item not in found:
            raise RuntimeError("0017 foreign-key contract mismatch")


def _index_contract(bind, table, name, columns, *, unique=False, predicate=None):
    row = next((x for x in sa.inspect(bind).get_indexes(table) if x["name"] == name), None)
    if not row or row.get("column_names") != columns or bool(row.get("unique")) != unique:
        raise RuntimeError("0017 index contract mismatch: " + name)
    if predicate is not None:
        opts = row.get("dialect_options") or {}
        candidate = opts.get("postgresql_where")
        if candidate is None:
            candidate = opts.get("sqlite_where")
        where = "" if candidate is None else str(candidate)
        normalized = where.lower().replace('"', "")
        normalized = re.sub(r"::(?:text|character varying|varchar)", "", normalized)
        normalized = re.sub(r"[\s()]", "", normalized)
        if normalized != "status='active'":
            raise RuntimeError("0017 partial-index predicate mismatch")


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    expected_columns = {
        "id", "approval_id", "publication_id", "pinterest_board_record_id",
        "publication_fingerprint", "request_fingerprint", "actor", "activated_at",
        "status", "expires_at", "consumed_at", "revoked_at", "revoked_by",
        "revoke_reason",
    }
    activation_table_exists = inspector.has_table("buffer_pilot_activations")
    if activation_table_exists:
        actual_columns = {
            column["name"]
            for column in inspector.get_columns("buffer_pilot_activations")
        }
        if actual_columns != expected_columns:
            raise RuntimeError(
                "Existing buffer_pilot_activations schema does not match migration 0017"
            )
        _column_contract(bind, "buffer_pilot_activations", {
            "id": (36, False, False), "approval_id": (36, False, False),
            "publication_id": (36, False, False), "pinterest_board_record_id": (36, False, False),
            "publication_fingerprint": (64, False, False), "request_fingerprint": (64, False, False),
            "actor": (255, False, False), "activated_at": (None, False, True),
            "status": (20, False, False), "expires_at": (None, True, False),
            "consumed_at": (None, True, False), "revoked_at": (None, True, False),
            "revoked_by": (255, True, False), "revoke_reason": (255, True, False),
        })
        pk = sa.inspect(bind).get_pk_constraint("buffer_pilot_activations")
        if pk.get("constrained_columns") != ["id"]:
            raise RuntimeError("0017 primary-key contract mismatch")
        _fk_contract(bind, "buffer_pilot_activations", [
            (("approval_id",), "pin_approvals", ("id",), "RESTRICT"),
            (("publication_id",), "pin_publications", ("id",), "RESTRICT"),
            (("pinterest_board_record_id",), "pinterest_boards", ("id",), "RESTRICT"),
        ])
        checks = [str(x.get("sqltext", "")).upper().replace(" ", "") for x in sa.inspect(bind).get_check_constraints("buffer_pilot_activations")]
        if not any("STATUSIN('ACTIVE','CONSUMED','REVOKED')" in x for x in checks):
            raise RuntimeError("0017 status-check contract mismatch")
    else:
        op.create_table(
            "buffer_pilot_activations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("approval_id", sa.String(36), nullable=False),
            sa.Column("publication_id", sa.String(36), nullable=False),
            sa.Column("pinterest_board_record_id", sa.String(36), nullable=False),
            sa.Column("publication_fingerprint", sa.String(64), nullable=False),
            sa.Column("request_fingerprint", sa.String(64), nullable=False),
            sa.Column("actor", sa.String(255), nullable=False),
            sa.Column("activated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column("consumed_at", sa.DateTime(timezone=True)),
            sa.Column("revoked_at", sa.DateTime(timezone=True)),
            sa.Column("revoked_by", sa.String(255)),
            sa.Column("revoke_reason", sa.String(255)),
            sa.CheckConstraint("status IN ('ACTIVE', 'CONSUMED', 'REVOKED')", name="ck_buffer_pilot_activation_status"),
            sa.ForeignKeyConstraint(["approval_id"], ["pin_approvals.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["publication_id"], ["pin_publications.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["pinterest_board_record_id"], ["pinterest_boards.id"], ondelete="RESTRICT"),
        )

    inspector = sa.inspect(bind)
    activation_indexes = {
        index["name"] for index in inspector.get_indexes("buffer_pilot_activations")
    }
    for name, columns in (
        ("ix_buffer_pilot_activations_approval_id", ["approval_id"]),
        ("ix_buffer_pilot_activations_publication_id", ["publication_id"]),
        ("ix_buffer_pilot_activations_pinterest_board_record_id", ["pinterest_board_record_id"]),
    ):
        if name not in activation_indexes:
            op.create_index(name, "buffer_pilot_activations", columns)
        else:
            _index_contract(bind, "buffer_pilot_activations", name, columns)
    if "uq_buffer_pilot_activation_active" not in activation_indexes:
        op.create_index(
            "uq_buffer_pilot_activation_active",
            "buffer_pilot_activations",
            ["status"],
            unique=True,
            sqlite_where=sa.text("status = 'ACTIVE'"),
            postgresql_where=sa.text("status = 'ACTIVE'"),
        )
    else:
        _index_contract(bind, "buffer_pilot_activations", "uq_buffer_pilot_activation_active",
                        ["status"], unique=True, predicate=True)

    attempt_columns = {
        column["name"] for column in inspector.get_columns("publication_attempts")
    }
    if "buffer_pilot_activation_id" not in attempt_columns:
        with op.batch_alter_table("publication_attempts") as batch:
            batch.add_column(sa.Column("buffer_pilot_activation_id", sa.String(36)))
            batch.create_foreign_key(
                "fk_publication_attempts_buffer_pilot_activation",
                "buffer_pilot_activations",
                ["buffer_pilot_activation_id"],
                ["id"],
                ondelete="RESTRICT",
            )
    else:
        _column_contract(bind, "publication_attempts", {"buffer_pilot_activation_id": (36, True, False)})
        _fk_contract(bind, "publication_attempts", [
            (("buffer_pilot_activation_id",), "buffer_pilot_activations", ("id",), "RESTRICT")
        ])
    attempt_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("publication_attempts")
    }
    if "ix_publication_attempts_buffer_pilot_activation_id" not in attempt_indexes:
        op.create_index(
            "ix_publication_attempts_buffer_pilot_activation_id",
            "publication_attempts",
            ["buffer_pilot_activation_id"],
        )
    else:
        _index_contract(bind, "publication_attempts",
                        "ix_publication_attempts_buffer_pilot_activation_id",
                        ["buffer_pilot_activation_id"])


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT COUNT(*) FROM buffer_pilot_activations")):
        raise RuntimeError("BUFFER_PILOT_ACTIVATION_HISTORY_PREVENTS_DOWNGRADE")
    if connection.scalar(sa.text(
        "SELECT COUNT(*) FROM publication_attempts WHERE buffer_pilot_activation_id IS NOT NULL"
    )):
        raise RuntimeError("BUFFER_PILOT_ATTEMPT_HISTORY_PREVENTS_DOWNGRADE")
    op.drop_index("ix_publication_attempts_buffer_pilot_activation_id", table_name="publication_attempts")
    with op.batch_alter_table("publication_attempts") as batch:
        batch.drop_constraint("fk_publication_attempts_buffer_pilot_activation", type_="foreignkey")
        batch.drop_column("buffer_pilot_activation_id")
    op.drop_index("uq_buffer_pilot_activation_active", table_name="buffer_pilot_activations")
    op.drop_index("ix_buffer_pilot_activations_pinterest_board_record_id", table_name="buffer_pilot_activations")
    op.drop_index("ix_buffer_pilot_activations_publication_id", table_name="buffer_pilot_activations")
    op.drop_index("ix_buffer_pilot_activations_approval_id", table_name="buffer_pilot_activations")
    op.drop_table("buffer_pilot_activations")
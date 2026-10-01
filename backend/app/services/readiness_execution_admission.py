"""Durable PostgreSQL-only admission for management readiness execution.

This is deliberately isolated from application ORM metadata and business rows.
An inserted admission is committed before the caller may spawn any work.
Its trigger protections cover ordinary DML; database owners/superusers retain
PostgreSQL's inherent ability to alter DDL or disable triggers and are trusted.
"""
from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert

from app.db.migration_adoption import (
    SchemaAdoptionRefused,
    verify_frozen_schema_at_head,
)
from app.services.readiness_execution_contract import (
    OPERATION,
    ReadinessBinding,
    ReadinessError,
)


metadata = sa.MetaData()
_HEX_GIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
management_readiness_admissions = sa.Table(
    "management_readiness_admissions",
    metadata,
    sa.Column("operation", sa.String(64), nullable=False),
    sa.Column("release_commit_sha", sa.String(64), nullable=False),
    sa.Column("release_tree_sha", sa.String(64), nullable=False),
    sa.Column("descriptor_sha256", sa.String(64), nullable=False),
    sa.Column("grant_id", sa.String(255), nullable=False),
    sa.Column("actor_hash", sa.String(64), nullable=False),
    sa.Column(
        "consumed_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Column(
        "outcome",
        sa.String(16),
        nullable=False,
        server_default=sa.text("'ADMITTED'"),
    ),
    sa.Column("exit_code", sa.Integer(), nullable=True),
    # Python None must mean SQL NULL, not JSON literal null: unknown outcomes
    # without a receipt are legal, non-object JSON evidence is not.
    sa.Column("receipt", JSONB(none_as_null=True), nullable=True),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint(
        "operation",
        "release_commit_sha",
        "release_tree_sha",
        name="pk_management_readiness_admissions",
    ),
    sa.UniqueConstraint("grant_id", name="uq_management_readiness_admissions_grant"),
    sa.CheckConstraint(
        "operation = 'object_storage_readiness_v1'",
        name="ck_management_readiness_admissions_operation",
    ),
    sa.CheckConstraint(
        "descriptor_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_management_readiness_admissions_descriptor_hash",
    ),
    sa.CheckConstraint(
        "actor_hash ~ '^[0-9a-f]{64}$'",
        name="ck_management_readiness_admissions_actor_hash",
    ),
    sa.CheckConstraint(
        "outcome = 'ADMITTED' OR outcome = 'PASS' OR "
        "outcome = 'FAILED' OR outcome = 'UNKNOWN'",
        name="ck_management_readiness_admissions_outcome",
    ),
    sa.CheckConstraint(
        "("
        "outcome = 'ADMITTED' AND exit_code IS NULL "
        "AND receipt IS NULL AND finished_at IS NULL"
        ") OR ("
        "outcome = 'PASS' AND exit_code = 0 AND receipt IS NOT NULL "
        "AND jsonb_typeof(receipt) = 'object' "
        "AND receipt ->> 'final_status' IS NOT DISTINCT FROM 'PASS' "
        "AND finished_at IS NOT NULL"
        ") OR ("
        "outcome = 'FAILED' AND exit_code IS NOT NULL "
        "AND exit_code IN (1, 2) AND receipt IS NOT NULL "
        "AND jsonb_typeof(receipt) = 'object' AND ("
        "(exit_code = 1 AND receipt ->> 'final_status' "
        "IS NOT DISTINCT FROM 'FAILED') OR "
        "(exit_code = 2 AND receipt ->> 'final_status' "
        "IS NOT DISTINCT FROM 'BLOCKED')"
        ") AND finished_at IS NOT NULL"
        ") OR ("
        "outcome = 'UNKNOWN' AND finished_at IS NOT NULL AND ("
        "receipt IS NULL OR (jsonb_typeof(receipt) = 'object' "
        "AND exit_code IS NOT NULL AND ("
        "(exit_code = 0 AND receipt ->> 'final_status' "
        "IS NOT DISTINCT FROM 'PASS') OR "
        "(exit_code = 1 AND receipt ->> 'final_status' "
        "IS NOT DISTINCT FROM 'FAILED') OR "
        "(exit_code = 2 AND receipt ->> 'final_status' "
        "IS NOT DISTINCT FROM 'BLOCKED')"
        ")))"
        ")",
        name="ck_management_readiness_admissions_outcome_evidence",
    ),
    schema="public",
)


class PostgresReadinessAdmission:
    """Persist one immutable grant consumption per release scope."""

    def __init__(self, engine: Any):
        self.engine = engine

    @staticmethod
    def _validate_binding(binding: ReadinessBinding) -> None:
        if not isinstance(binding, ReadinessBinding):
            raise ReadinessError("COORDINATOR_UNAVAILABLE")
        try:
            valid = (
                binding.scope[0] == OPERATION
                and all(
                    isinstance(value, str) and _HEX_GIT_SHA.fullmatch(value)
                    for value in (
                        binding.canonical_commit_sha,
                        binding.canonical_tree_sha,
                        binding.release_commit_sha,
                        binding.release_tree_sha,
                    )
                )
                and all(
                    isinstance(value, str) and _HEX_SHA256.fullmatch(value)
                    for value in (binding.overlay_sha256, binding.probe_sha256)
                )
                and isinstance(binding.topology, str)
                and bool(binding.topology)
                and len(binding.topology) <= 128
                and bool(_HEX_SHA256.fullmatch(binding.digest))
            )
        except Exception:
            valid = False
        if not valid:
            raise ReadinessError("COORDINATOR_UNAVAILABLE")

    @staticmethod
    def _validate_input(
        binding: ReadinessBinding,
        grant_id: str,
        actor_hash: str,
    ) -> None:
        PostgresReadinessAdmission._validate_binding(binding)
        if not isinstance(grant_id, str) or not grant_id or len(grant_id) > 255:
            raise ReadinessError("COORDINATOR_UNAVAILABLE")
        if (
            not isinstance(actor_hash, str)
            or not _HEX_SHA256.fullmatch(actor_hash)
        ):
            raise ReadinessError("COORDINATOR_UNAVAILABLE")

    @staticmethod
    def _bound_transaction(connection: Any) -> None:
        if getattr(connection.dialect, "name", None) != "postgresql":
            raise SchemaAdoptionRefused("readiness admission requires PostgreSQL")
        connection.execute(sa.text("SET LOCAL search_path TO public, pg_catalog"))
        connection.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
        connection.execute(sa.text("SET LOCAL statement_timeout = '8s'"))
        connection.execute(sa.text(
            'LOCK TABLE "public"."alembic_version" IN SHARE MODE'
        ))
        connection.execute(sa.text(
            'LOCK TABLE "public"."management_readiness_admissions" '
            "IN ROW EXCLUSIVE MODE"
        ))
        # Verify the complete frozen head, including the management-only table,
        # while DDL cannot alter the coordinator contract.
        verify_frozen_schema_at_head(connection, revision="0032")

    def consume(
        self,
        binding: ReadinessBinding,
        grant_id: str,
        actor_hash: str,
        *,
        expires_at: int | None = None,
    ) -> bool:
        """Commit a new admission before returning true; never retry ambiguity."""
        self._validate_input(binding, grant_id, actor_hash)
        if expires_at is not None and type(expires_at) is not int:
            raise ReadinessError("COORDINATOR_UNAVAILABLE")
        try:
            with self.engine.connect() as connection:
                with connection.begin():
                    self._bound_transaction(connection)
                    if expires_at is not None:
                        database_epoch = connection.scalar(
                            sa.select(sa.extract(
                                "epoch", sa.func.clock_timestamp()
                            ))
                        )
                        if database_epoch >= expires_at:
                            raise ReadinessError(
                                "EXECUTION_AUTHORIZATION_EXPIRED", 403
                            )
                    statement = (
                        pg_insert(management_readiness_admissions)
                        .values(
                            operation=binding.scope[0],
                            release_commit_sha=binding.scope[1],
                            release_tree_sha=binding.scope[2],
                            descriptor_sha256=binding.digest,
                            grant_id=grant_id,
                            actor_hash=actor_hash,
                            outcome="ADMITTED",
                        )
                        .on_conflict_do_nothing()
                        .returning(management_readiness_admissions.c.grant_id)
                    )
                    inserted = connection.execute(statement).scalar_one_or_none()
            # Engine.begin() has committed successfully at this point. A commit
            # exception is treated as ambiguous and is never retried.
            return inserted is not None
        except ReadinessError:
            raise
        except Exception:
            raise ReadinessError("COORDINATOR_UNAVAILABLE") from None

    def finish(
        self,
        binding: ReadinessBinding,
        outcome: str,
        exit_code: int | None,
        receipt: dict | None,
    ) -> None:
        """Persist one terminal outcome, allowing only an exact idempotent repeat."""
        try:
            if (
                not isinstance(binding, ReadinessBinding)
                or outcome not in {"PASS", "FAILED", "UNKNOWN"}
                or (exit_code is not None and type(exit_code) is not int)
                or (receipt is not None and not isinstance(receipt, dict))
            ):
                raise ValueError("invalid readiness outcome")
            self._validate_binding(binding)
            safe_receipt = None
            if receipt is not None:
                from app.services.readiness_execution_runner import validate_receipt

                safe_receipt = validate_receipt(receipt, exit_code)
            if outcome == "PASS" and (
                exit_code != 0
                or safe_receipt is None
                or safe_receipt["final_status"] != "PASS"
            ):
                raise ValueError("PASS requires a validated PASS receipt")
            if outcome == "FAILED" and (
                safe_receipt is None
                or safe_receipt["final_status"] not in {"FAILED", "BLOCKED"}
            ):
                raise ValueError("FAILED requires a validated failed receipt")
            with self.engine.connect() as connection:
                with connection.begin():
                    self._bound_transaction(connection)
                    table = management_readiness_admissions
                    scope = (
                        (table.c.operation == binding.scope[0])
                        & (table.c.release_commit_sha == binding.scope[1])
                        & (table.c.release_tree_sha == binding.scope[2])
                        & (table.c.descriptor_sha256 == binding.digest)
                    )
                    result = connection.execute(
                        table.update()
                        .where(scope, table.c.outcome == "ADMITTED")
                        .values(
                            outcome=outcome,
                            exit_code=exit_code,
                            receipt=safe_receipt,
                            finished_at=sa.func.now(),
                        )
                        .returning(table.c.outcome)
                    )
                    updated = result.scalar_one_or_none()
                    if updated is None:
                        existing = connection.execute(
                            sa.select(
                                table.c.outcome,
                                table.c.exit_code,
                                table.c.receipt,
                            ).where(scope)
                        ).mappings().one_or_none()
                        if existing is None:
                            raise ValueError("readiness admission missing")
                        if (
                            existing["outcome"] != outcome
                            or existing["exit_code"] != exit_code
                            or existing["receipt"] != safe_receipt
                        ):
                            raise ValueError("readiness outcome already terminal")
        except Exception:
            raise ReadinessError("OUTCOME_PERSISTENCE_FAILED") from None

    def lookup(self, binding: ReadinessBinding) -> dict | None:
        """Return only persisted consumed admission state and its terminal evidence."""
        try:
            if not isinstance(binding, ReadinessBinding):
                raise ValueError("invalid readiness binding")
            self._validate_binding(binding)
            with self.engine.connect() as connection:
                with connection.begin():
                    self._bound_transaction(connection)
                    table = management_readiness_admissions
                    row = connection.execute(
                        sa.select(
                            table.c.descriptor_sha256,
                            table.c.outcome,
                            table.c.exit_code,
                            table.c.receipt,
                        ).where(
                            table.c.operation == binding.scope[0],
                            table.c.release_commit_sha == binding.scope[1],
                            table.c.release_tree_sha == binding.scope[2],
                        )
                    ).mappings().one_or_none()
                    if row is None:
                        return None
                    if row["descriptor_sha256"] != binding.digest:
                        raise ReadinessError("ADMISSION_BINDING_MISMATCH")
                    return {
                        "admission_state": "CONSUMED",
                        "outcome": (
                            "UNKNOWN" if row["outcome"] == "ADMITTED" else row["outcome"]
                        ),
                        "exit_code": row["exit_code"],
                        "receipt": row["receipt"],
                    }
        except ReadinessError:
            raise
        except Exception:
            raise ReadinessError("COORDINATOR_UNAVAILABLE") from None
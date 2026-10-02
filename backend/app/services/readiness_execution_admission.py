"""PostgreSQL insert-only readiness coordinator, isolated from business rows.

Consumption commits before work. Relational uniqueness is not protection
against unrestricted SQL deletion or rewriting; no trigger-equivalence claim.
"""
from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.migration_adoption import (
    SchemaAdoptionRefused,
    verify_frozen_schema_at_head,
)
from app.db.readiness_schema_0033 import tables, validate_terminal
from app.services.readiness_execution_contract import (
    OPERATION,
    ReadinessBinding,
    ReadinessError,
)


metadata = sa.MetaData()
_HEX_GIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
management_readiness_admissions, management_readiness_outcomes = tables(metadata)


class PostgresReadinessAdmission:
    """Persist one grant consumption per release scope through insert-only DML."""

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
        connection.execute(sa.text(
            'LOCK TABLE "public"."management_readiness_outcomes" '
            "IN ROW EXCLUSIVE MODE"
        ))
        verify_frozen_schema_at_head(connection, revision="0033")

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
            safe_receipt = validate_terminal(outcome, exit_code, receipt)
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
                    if connection.execute(
                        sa.select(table.c.grant_id).where(scope)
                    ).scalar_one_or_none() is None:
                        raise ValueError("readiness admission missing or mismatched")
                    terminal = management_readiness_outcomes
                    result = connection.execute(
                        pg_insert(terminal)
                        .values(
                            operation=binding.scope[0],
                            release_commit_sha=binding.scope[1],
                            release_tree_sha=binding.scope[2],
                            outcome=outcome,
                            exit_code=exit_code,
                            receipt=safe_receipt,
                        )
                        .on_conflict_do_nothing(index_elements=[
                            terminal.c.operation, terminal.c.release_commit_sha,
                            terminal.c.release_tree_sha,
                        ])
                        .returning(terminal.c.outcome)
                    )
                    updated = result.scalar_one_or_none()
                    if updated is None:
                        existing = connection.execute(
                            sa.select(
                                terminal.c.outcome,
                                terminal.c.exit_code,
                                terminal.c.receipt,
                            ).where(
                                terminal.c.operation == binding.scope[0],
                                terminal.c.release_commit_sha == binding.scope[1],
                                terminal.c.release_tree_sha == binding.scope[2],
                            )
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
                    terminal = management_readiness_outcomes
                    row = connection.execute(
                        sa.select(
                            table.c.descriptor_sha256,
                            terminal.c.outcome,
                            terminal.c.exit_code,
                            terminal.c.receipt,
                        ).select_from(table.outerjoin(terminal, sa.and_(
                            table.c.operation == terminal.c.operation,
                            table.c.release_commit_sha == terminal.c.release_commit_sha,
                            table.c.release_tree_sha == terminal.c.release_tree_sha,
                        ))).where(
                            table.c.operation == binding.scope[0],
                            table.c.release_commit_sha == binding.scope[1],
                            table.c.release_tree_sha == binding.scope[2],
                        )
                    ).mappings().one_or_none()
                    if row is None:
                        return None
                    if row["descriptor_sha256"] != binding.digest:
                        raise ReadinessError("ADMISSION_BINDING_MISMATCH")
                    if row["outcome"] is not None:
                        validate_terminal(row["outcome"], row["exit_code"], row["receipt"])
                    return {
                        "admission_state": "CONSUMED",
                        "outcome": (
                            "UNKNOWN" if row["outcome"] is None else row["outcome"]
                        ),
                        "exit_code": row["exit_code"],
                        "receipt": row["receipt"],
                    }
        except ReadinessError:
            raise
        except Exception:
            raise ReadinessError("COORDINATOR_UNAVAILABLE") from None
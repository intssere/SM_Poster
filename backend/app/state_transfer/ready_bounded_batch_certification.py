"""Strictly read-only certification of the single prepared five-Pin READY batch."""
from __future__ import annotations

import hashlib
import json
import logging
import re

import sqlalchemy as sa

from .catalog import digest, validate_catalog
from .one_shot_migration import _env, _require, _url
from .production_media_certification import gate_snapshot
from .transfer import transaction


DATABASE_ENV = "DATABASE_URL"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
NONTERMINAL = ("OPEN", "PREPARING", "READY", "RUNNING")
AUTONOMOUS_ACTOR = "autonomous-policy-v1"
IDENTITY = (
    "slot", "item_id", "product_id", "board_id", "external_board_id",
    "item_fingerprint", "publication_id", "permit_id",
    "publication_fingerprint", "request_fingerprint",
)


class ReadyCertificationRefusal(RuntimeError):
    def __init__(self, code: str, stage: str, field: str):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.field = field


def _check(condition, code: str, stage: str, field: str):
    if not condition:
        raise ReadyCertificationRefusal(code, stage, field)


def _hex64(value, *, field="fingerprint", stage="IDENTITY"):
    _check(
        isinstance(value, str) and bool(HEX64.fullmatch(value)),
        "READY_INVALID_HEX64",
        stage,
        field,
    )
    return value


def _text(value, maximum=255, *, field="text", stage="IDENTITY"):
    _check(
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and not any(ord(ch) < 32 for ch in value),
        "READY_INVALID_TEXT",
        stage,
        field,
    )
    return value


def _manifest_hash(rows):
    payload = [{key: row[key] for key in IDENTITY} for row in rows]
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def database_snapshot(engine, report):
    """Exactly one PostgreSQL REPEATABLE READ / READ ONLY transaction."""
    with transaction(engine, readonly=True) as connection:
        report["database_transactions"] += 1
        _check(
            connection.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on",
            "READY_TRANSACTION_NOT_READ_ONLY", "TRANSACTION", "transaction_read_only",
        )
        _check(
            connection.exec_driver_sql("SHOW transaction_isolation").scalar_one() == "repeatable read",
            "READY_TRANSACTION_ISOLATION_MISMATCH", "TRANSACTION", "transaction_isolation",
        )

        revisions = connection.exec_driver_sql(
            "SELECT version_num FROM public.alembic_version ORDER BY version_num"
        ).scalars().all()
        _check(revisions == ["0034"], "READY_SCHEMA_REVISION_MISMATCH", "SCHEMA", "alembic_version")
        validate_catalog(connection, "0034")

        controls = connection.exec_driver_sql(
            "SELECT state FROM public.routine_publishing_control ORDER BY id LIMIT 2"
        ).scalars().all()
        _check(controls == ["PAUSED"], "READY_ROUTINE_CONTROL_MISMATCH", "CONTROL", "routine_state")

        unknown = connection.exec_driver_sql(
            "SELECT count(*) FROM public.pin_publications "
            "WHERE status::text='PUBLISH_UNKNOWN'"
        ).scalar_one()
        _check(int(unknown) == 0, "READY_PUBLISH_UNKNOWN_PRESENT", "CONTROL", "publish_unknown_count")

        batch_rows = connection.exec_driver_sql(
            "SELECT id,target_count,attempts_reserved,state,admission_closed,"
            "manifest_sha256,owner,lease_until,reason,created_at "
            "FROM public.routine_autonomous_batches "
            "WHERE state IN ('OPEN','PREPARING','READY','RUNNING') "
            "ORDER BY created_at,id LIMIT 2"
        ).mappings().all()
        report["nonterminal_batch_count_class"] = (
            "ZERO" if len(batch_rows) == 0
            else "ONE" if len(batch_rows) == 1
            else "MULTIPLE"
        )
        _check(len(batch_rows) == 1, "READY_NONTERMINAL_BATCH_COUNT_MISMATCH", "BATCH", "batch_count")
        batch = dict(batch_rows[0])
        _text(batch["id"], 36, field="batch_id", stage="BATCH")
        for passed, code, field in (
            (batch["target_count"] == 5, "READY_BATCH_TARGET_COUNT_MISMATCH", "target_count"),
            (batch["attempts_reserved"] == 0, "READY_BATCH_ATTEMPTS_RESERVED_NONZERO", "attempts_reserved"),
            (batch["state"] == "READY", "READY_BATCH_STATE_MISMATCH", "state"),
            (batch["admission_closed"] is False, "READY_BATCH_ADMISSION_CLOSED", "admission_closed"),
            (batch["owner"] is None, "READY_BATCH_OWNER_PRESENT", "owner"),
            (batch["lease_until"] is None, "READY_BATCH_LEASE_PRESENT", "lease_until"),
        ):
            _check(passed, code, "BATCH", field)
        _hex64(batch["manifest_sha256"], field="manifest_sha256", stage="BATCH")

        rows = connection.exec_driver_sql(
            """
            SELECT
              e.slot,e.item_id,e.product_id,e.board_id,e.external_board_id,
              e.item_fingerprint,e.publication_id,e.permit_id,
              e.publication_fingerprint,e.request_fingerprint,
              e.reserved_at,e.attempt_id,e.outcome,
              i.plan_id,i.status AS item_status,i.publication_id AS item_publication_id,
              p.plan_fingerprint,p.status AS plan_status,p.month_start,p.month_end,
              pub.status::text AS publication_status,pub.scheduled_for,
              pub.publication_fingerprint AS publication_db_fingerprint,
              pub.pinterest_board_record_id AS publication_board_id,
              pub.pinterest_board_id_snapshot,pub.pinterest_connection_id,
              pub.approval_id,pub.creative_id,pub.attempt_started_at,
              pub.pinterest_pin_id,pub.published_at,
              permit.status AS permit_status,permit.dispatch_provider,
              permit.publication_id AS permit_publication_id,
              permit.approval_id AS permit_approval_id,
              permit.pinterest_board_record_id AS permit_board_id,
              permit.publication_fingerprint AS permit_publication_fingerprint,
              permit.request_fingerprint AS permit_request_fingerprint,
              permit.scheduled_for_snapshot,permit.authorized_by,
              permit.authorized_at,permit.expires_at,permit.consumed_at,
              permit.revoked_at,permit.revoked_by,permit.revoke_reason,
              approval.decision AS approval_decision,
              approval.decided_by AS approval_decided_by,
              approval.creative_id AS approval_creative_id,
              approval.draft_id AS approval_draft_id,
              creative.render_status,creative.sha256 AS creative_sha256,
              creative.size_bytes AS creative_size_bytes,
              creative.rendered_url AS creative_rendered_url,
              board.external_board_id AS board_external_id,
              board.connection_id,board.is_active AS board_active,
              board.is_eligible AS board_eligible,
              conn.status AS connection_status
            FROM public.routine_autonomous_batch_entries e
            JOIN public.pinterest_portfolio_plan_items i ON i.id=e.item_id
            JOIN public.pinterest_portfolio_plans p ON p.id=i.plan_id
            JOIN public.pin_publications pub ON pub.id=e.publication_id
            JOIN public.routine_dispatch_permits permit ON permit.id=e.permit_id
            JOIN public.pin_approvals approval ON approval.id=pub.approval_id
            JOIN public.pin_creatives creative ON creative.id=pub.creative_id
            JOIN public.pinterest_boards board ON board.id=e.board_id
            JOIN public.pinterest_connections conn ON conn.id=board.connection_id
            WHERE e.batch_id=%s
            ORDER BY e.slot
            """,
            (batch["id"],),
        ).mappings().all()
        rows = [dict(row) for row in rows]
        _check(len(rows) == 5, "READY_BATCH_ENTRY_COUNT_MISMATCH", "MANIFEST", "entry_count")
        _check(
            [row["slot"] for row in rows] == list(range(5)),
            "READY_BATCH_SLOT_ORDER_MISMATCH", "MANIFEST", "slots",
        )
        _check(
            _manifest_hash(rows) == batch["manifest_sha256"],
            "READY_BATCH_MANIFEST_HASH_MISMATCH", "MANIFEST", "manifest_sha256",
        )

        now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        current_date = now.date()
        month_start = current_date.replace(day=1)
        plan_ids = {row["plan_id"] for row in rows}
        plan_fingerprints = {row["plan_fingerprint"] for row in rows}
        _check(
            len(plan_ids) == 1 and len(plan_fingerprints) == 1,
            "READY_PLAN_IDENTITY_MISMATCH", "PLAN", "plan_identity",
        )
        plan_id = _text(next(iter(plan_ids)), 36, field="plan_id", stage="PLAN")
        plan_fingerprint = _hex64(
            next(iter(plan_fingerprints)), field="plan_fingerprint", stage="PLAN",
        )

        entries = []
        for row in rows:
            for key in (
                "item_fingerprint", "publication_fingerprint", "request_fingerprint",
                "publication_db_fingerprint", "permit_publication_fingerprint",
                "permit_request_fingerprint", "creative_sha256",
            ):
                _hex64(row[key], field=key, stage="ENTRY")
            for key, maximum in (
                ("item_id", 36), ("product_id", 36), ("board_id", 36),
                ("external_board_id", 255), ("publication_id", 36),
                ("permit_id", 36), ("creative_id", 36),
            ):
                _text(row[key], maximum, field=key, stage="ENTRY")

            checks = (
                (row["reserved_at"] is None, "READY_ENTRY_ALREADY_RESERVED", "reserved_at"),
                (row["attempt_id"] is None, "READY_ENTRY_ATTEMPT_ID_PRESENT", "attempt_id"),
                (row["outcome"] is None, "READY_ENTRY_OUTCOME_PRESENT", "outcome"),
                (row["item_status"] == "SCHEDULED", "READY_PLAN_ITEM_STATUS_MISMATCH", "item_status"),
                (row["item_publication_id"] == row["publication_id"], "READY_PLAN_ITEM_PUBLICATION_MISMATCH", "item_publication_id"),
                (row["plan_status"] == "ACTIVE", "READY_PLAN_NOT_ACTIVE", "plan_status"),
                (row["month_start"] == month_start, "READY_PLAN_MONTH_START_MISMATCH", "month_start"),
                (row["month_end"] >= current_date, "READY_PLAN_MONTH_EXPIRED", "month_end"),
                (row["publication_status"] == "SCHEDULED", "READY_PUBLICATION_STATUS_MISMATCH", "publication_status"),
                (row["scheduled_for"] is not None, "READY_PUBLICATION_SCHEDULE_MISSING", "scheduled_for"),
                (row["scheduled_for"] is not None and row["scheduled_for"] > now, "READY_PUBLICATION_NOT_FUTURE", "scheduled_for"),
                (row["publication_db_fingerprint"] == row["publication_fingerprint"], "READY_PUBLICATION_FINGERPRINT_MISMATCH", "publication_fingerprint"),
                (row["publication_board_id"] == row["board_id"], "READY_PUBLICATION_BOARD_MISMATCH", "publication_board_id"),
                (row["pinterest_board_id_snapshot"] == row["external_board_id"], "READY_PUBLICATION_EXTERNAL_BOARD_MISMATCH", "pinterest_board_id_snapshot"),
                (row["attempt_started_at"] is None, "READY_PUBLICATION_ATTEMPT_STARTED", "attempt_started_at"),
                (row["pinterest_pin_id"] is None, "READY_PUBLICATION_PROVIDER_ID_PRESENT", "pinterest_pin_id"),
                (row["published_at"] is None, "READY_PUBLICATION_PUBLISHED_AT_PRESENT", "published_at"),
                (row["permit_status"] == "ACTIVE", "READY_PERMIT_STATUS_MISMATCH", "permit_status"),
                (row["dispatch_provider"] == "buffer", "READY_PERMIT_PROVIDER_MISMATCH", "dispatch_provider"),
                (row["permit_publication_id"] == row["publication_id"], "READY_PERMIT_PUBLICATION_MISMATCH", "permit_publication_id"),
                (row["permit_approval_id"] == row["approval_id"], "READY_PERMIT_APPROVAL_MISMATCH", "permit_approval_id"),
                (row["permit_board_id"] == row["board_id"], "READY_PERMIT_BOARD_MISMATCH", "permit_board_id"),
                (row["permit_publication_fingerprint"] == row["publication_fingerprint"], "READY_PERMIT_PUBLICATION_FINGERPRINT_MISMATCH", "permit_publication_fingerprint"),
                (row["permit_request_fingerprint"] == row["request_fingerprint"], "READY_PERMIT_REQUEST_FINGERPRINT_MISMATCH", "permit_request_fingerprint"),
                (row["scheduled_for_snapshot"] == row["scheduled_for"], "READY_PERMIT_SCHEDULE_MISMATCH", "scheduled_for_snapshot"),
                (row["authorized_by"] == AUTONOMOUS_ACTOR, "READY_PERMIT_ACTOR_MISMATCH", "authorized_by"),
                (row["expires_at"] is not None and row["expires_at"] > now, "READY_PERMIT_EXPIRED", "expires_at"),
                (row["consumed_at"] is None, "READY_PERMIT_CONSUMED", "consumed_at"),
                (row["revoked_at"] is None, "READY_PERMIT_REVOKED", "revoked_at"),
                (row["revoked_by"] is None, "READY_PERMIT_REVOKED", "revoked_by"),
                (row["revoke_reason"] is None, "READY_PERMIT_REVOKED", "revoke_reason"),
                (row["approval_decision"] == "APPROVED", "READY_APPROVAL_DECISION_MISMATCH", "approval_decision"),
                (row["approval_decided_by"] == AUTONOMOUS_ACTOR, "READY_APPROVAL_ACTOR_MISMATCH", "approval_decided_by"),
                (row["approval_creative_id"] == row["creative_id"], "READY_APPROVAL_CREATIVE_MISMATCH", "approval_creative_id"),
                (row["render_status"] == "RENDERED", "READY_CREATIVE_RENDER_STATUS_MISMATCH", "render_status"),
                (row["creative_size_bytes"] is not None and int(row["creative_size_bytes"]) > 0, "READY_CREATIVE_SIZE_INVALID", "creative_size_bytes"),
                (bool(row["creative_rendered_url"]), "READY_CREATIVE_URL_MISSING", "creative_rendered_url"),
                (row["board_external_id"] == row["external_board_id"], "READY_BOARD_EXTERNAL_ID_MISMATCH", "board_external_id"),
                (row["board_active"] is True, "READY_BOARD_INACTIVE", "board_active"),
                (row["board_eligible"] is True, "READY_BOARD_INELIGIBLE", "board_eligible"),
                (row["connection_status"] == "CONNECTED", "READY_CONNECTION_NOT_CONNECTED", "connection_status"),
                (row["pinterest_connection_id"] == row["connection_id"], "READY_CONNECTION_ID_MISMATCH", "pinterest_connection_id"),
            )
            for passed, code, field in checks:
                _check(passed, code, "ENTRY", field)
            entries.append({
                "slot": int(row["slot"]),
                "item_id": row["item_id"],
                "product_id": row["product_id"],
                "pinterest_board_record_id": row["board_id"],
                "external_board_id": row["external_board_id"],
                "item_fingerprint": row["item_fingerprint"],
                "publication_id": row["publication_id"],
                "permit_id": row["permit_id"],
                "publication_fingerprint": row["publication_fingerprint"],
                "request_fingerprint": row["request_fingerprint"],
                "scheduled_for": row["scheduled_for"].isoformat(),
                "permit_expires_at": row["expires_at"].isoformat(),
                "creative_id": row["creative_id"],
                "creative_sha256": row["creative_sha256"],
                "creative_size_bytes": int(row["creative_size_bytes"]),
                "creative_rendered_url": row["creative_rendered_url"],
            })

        for table, join_column in (
            ("publication_attempts", "publication_id"),
            ("routine_attempt_boundaries", "publication_id"),
            ("routine_scheduled_quota_reservations", "publication_id"),
        ):
            count = connection.exec_driver_sql(
                f"SELECT count(*) FROM public.{table} x "
                "JOIN public.routine_autonomous_batch_entries e "
                f"ON e.publication_id=x.{join_column} "
                "WHERE e.batch_id=%s",
                (batch["id"],),
            ).scalar_one()
            _check(
                int(count) == 0,
                "READY_ATTEMPT_EVIDENCE_PRESENT",
                "ACCOUNTING",
                table,
            )

        dossier = {
            "contract": "FIVE_PIN_READY_BATCH_CERTIFICATION_V1",
            "database_revision": "0034",
            "routine_state": "PAUSED",
            "batch_id": batch["id"],
            "batch_manifest_sha256": batch["manifest_sha256"],
            "batch_state": "READY",
            "target_count": 5,
            "attempts_reserved": 0,
            "admission_closed": False,
            "plan_id": plan_id,
            "plan_fingerprint": plan_fingerprint,
            "candidate_count": 5,
            "entries": entries,
        }
        dossier["ready_batch_fingerprint"] = digest(dossier)
        return dossier


def run():
    report = {
        "success": False,
        "mode": "READ_ONLY_FIVE_PIN_READY_BATCH_CERTIFICATION",
        "terminal_stage": "GATES",
        "database_transactions": 0,
        "database_writes": 0,
        "object_storage_reads": 0,
        "object_storage_writes": 0,
        "provider_calls": 0,
        "provider_reads": 0,
        "provider_writes": 0,
        "buffer_calls": 0,
        "pinterest_calls": 0,
        "oauth_calls": 0,
        "ai_calls": 0,
        "automatic_retries": 0,
        "scheduler_activations": 0,
        "worker_activations": 0,
        "autonomy_activations": 0,
        "provider_attempts_reserved": 0,
        "publishing_admission": "NOT_GRANTED",
        "ready_batch_certification": "NOT_GRANTED",
        "batch_id": None,
        "batch_manifest_sha256": None,
        "ready_batch_fingerprint": None,
        "entries": [],
        "refusal_code": None,
        "refusal_stage": None,
        "refusal_field": None,
        "nonterminal_batch_count_class": None,
    }
    previous = logging.root.manager.disable
    engine = None
    logging.disable(logging.CRITICAL)
    try:
        gates = gate_snapshot()
        report["gate_fingerprint"] = digest(gates)

        report["terminal_stage"] = "READ_ONLY_DATABASE"
        engine = sa.create_engine(
            _url(_env(DATABASE_ENV)),
            echo=False,
            hide_parameters=True,
            connect_args={"connect_timeout": 10},
        )
        report.update(database_snapshot(engine, report))
        engine.dispose()
        engine = None

        report["ready_batch_certification"] = "PASS"
        report["terminal_stage"] = "COMPLETE"
        report["success"] = True
    except ReadyCertificationRefusal as exc:
        report.update(
            success=False,
            ready_batch_certification="NOT_GRANTED",
            refusal_code=exc.code,
            refusal_stage=exc.stage,
            refusal_field=exc.field,
        )
    except Exception:
        report.update(
            success=False,
            ready_batch_certification="NOT_GRANTED",
            refusal_code="READY_CERTIFICATION_UNEXPECTED",
            refusal_stage=report.get("terminal_stage"),
            refusal_field=None,
        )
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                report.update(
                    success=False,
                    terminal_stage="DATABASE_CLOSE",
                    ready_batch_certification="NOT_GRANTED",
                    refusal_code="READY_DATABASE_CLOSE_FAILED",
                    refusal_stage="DATABASE_CLOSE",
                    refusal_field=None,
                )
        logging.disable(previous)
    return report

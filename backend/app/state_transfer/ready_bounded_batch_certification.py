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


def _hex64(value):
    _require(isinstance(value, str) and bool(HEX64.fullmatch(value)))
    return value


def _text(value, maximum=255):
    _require(
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and not any(ord(ch) < 32 for ch in value)
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
        _require(connection.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on")
        _require(connection.exec_driver_sql("SHOW transaction_isolation").scalar_one() == "repeatable read")

        revisions = connection.exec_driver_sql(
            "SELECT version_num FROM public.alembic_version ORDER BY version_num"
        ).scalars().all()
        _require(revisions == ["0034"])
        validate_catalog(connection, "0034")

        controls = connection.exec_driver_sql(
            "SELECT state FROM public.routine_publishing_control ORDER BY id LIMIT 2"
        ).scalars().all()
        _require(controls == ["PAUSED"])

        unknown = connection.exec_driver_sql(
            "SELECT count(*) FROM public.pin_publications "
            "WHERE status::text='PUBLISH_UNKNOWN'"
        ).scalar_one()
        _require(int(unknown) == 0)

        batch_rows = connection.exec_driver_sql(
            "SELECT id,target_count,attempts_reserved,state,admission_closed,"
            "manifest_sha256,owner,lease_until,reason,created_at "
            "FROM public.routine_autonomous_batches "
            "WHERE state IN ('OPEN','PREPARING','READY','RUNNING') "
            "ORDER BY created_at,id LIMIT 2"
        ).mappings().all()
        _require(len(batch_rows) == 1)
        batch = dict(batch_rows[0])
        _text(batch["id"], 36)
        _require(
            batch["target_count"] == 5
            and batch["attempts_reserved"] == 0
            and batch["state"] == "READY"
            and batch["admission_closed"] is False
            and batch["owner"] is None
            and batch["lease_until"] is None
        )
        _hex64(batch["manifest_sha256"])

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
        _require(len(rows) == 5)
        _require([row["slot"] for row in rows] == list(range(5)))
        _require(_manifest_hash(rows) == batch["manifest_sha256"])

        now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        current_date = now.date()
        month_start = current_date.replace(day=1)
        plan_ids = {row["plan_id"] for row in rows}
        plan_fingerprints = {row["plan_fingerprint"] for row in rows}
        _require(len(plan_ids) == 1 and len(plan_fingerprints) == 1)
        plan_id = _text(next(iter(plan_ids)), 36)
        plan_fingerprint = _hex64(next(iter(plan_fingerprints)))

        entries = []
        for row in rows:
            for key in (
                "item_fingerprint", "publication_fingerprint", "request_fingerprint",
                "publication_db_fingerprint", "permit_publication_fingerprint",
                "permit_request_fingerprint", "creative_sha256",
            ):
                _hex64(row[key])
            for key, maximum in (
                ("item_id", 36), ("product_id", 36), ("board_id", 36),
                ("external_board_id", 255), ("publication_id", 36),
                ("permit_id", 36), ("creative_id", 36),
            ):
                _text(row[key], maximum)

            _require(
                row["reserved_at"] is None
                and row["attempt_id"] is None
                and row["outcome"] is None
                and row["item_status"] == "SCHEDULED"
                and row["item_publication_id"] == row["publication_id"]
                and row["plan_status"] == "ACTIVE"
                and row["month_start"] == month_start
                and row["month_end"] >= current_date
                and row["publication_status"] == "SCHEDULED"
                and row["scheduled_for"] is not None
                and row["scheduled_for"] > now
                and row["publication_db_fingerprint"] == row["publication_fingerprint"]
                and row["publication_board_id"] == row["board_id"]
                and row["pinterest_board_id_snapshot"] == row["external_board_id"]
                and row["attempt_started_at"] is None
                and row["pinterest_pin_id"] is None
                and row["published_at"] is None
                and row["permit_status"] == "ACTIVE"
                and row["dispatch_provider"] == "buffer"
                and row["permit_publication_id"] == row["publication_id"]
                and row["permit_approval_id"] == row["approval_id"]
                and row["permit_board_id"] == row["board_id"]
                and row["permit_publication_fingerprint"] == row["publication_fingerprint"]
                and row["permit_request_fingerprint"] == row["request_fingerprint"]
                and row["scheduled_for_snapshot"] == row["scheduled_for"]
                and row["authorized_by"] == AUTONOMOUS_ACTOR
                and row["expires_at"] > now
                and row["consumed_at"] is None
                and row["revoked_at"] is None
                and row["revoked_by"] is None
                and row["revoke_reason"] is None
                and row["approval_decision"] == "APPROVED"
                and row["approval_decided_by"] == AUTONOMOUS_ACTOR
                and row["approval_creative_id"] == row["creative_id"]
                and row["render_status"] == "RENDERED"
                and row["creative_size_bytes"] is not None
                and int(row["creative_size_bytes"]) > 0
                and row["creative_rendered_url"]
                and row["board_external_id"] == row["external_board_id"]
                and row["board_active"] is True
                and row["board_eligible"] is True
                and row["connection_status"] == "CONNECTED"
                and row["pinterest_connection_id"] == row["connection_id"]
            )
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
            _require(int(count) == 0)

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
    except Exception:
        report["success"] = False
        report["ready_batch_certification"] = "NOT_GRANTED"
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                report.update(
                    success=False,
                    terminal_stage="DATABASE_CLOSE",
                    ready_batch_certification="NOT_GRANTED",
                )
        logging.disable(previous)
    return report

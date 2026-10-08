"""Strictly read-only five-Pin bounded-pilot preflight certification.

No publication, creative, approval, permit, batch, provider, AI, object-storage,
scheduler, worker, or autonomy execution path is reachable from this module.
It certifies the exact five candidates that the later bounded preparation
service would select from the current ACTIVE portfolio plan.
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.domain import Board, PinterestBoard, PinterestConnection, Product, PinterestPortfolioPlanItem
from app.services.pinterest_board_strategy import board_strategy
from app.services.routine_bounded_preparation import _execution_ready_items, _execution_settings
from app.services.pinterest_autonomous_execution import execution_readiness
from app.services.pinterest_autonomous_generation import autonomous_generation_preparation_readiness
from app.db.bounded_batch_schema_0034 import entries
from .catalog import digest, validate_catalog
from .one_shot_migration import _env, _require, _url
from .production_media_certification import gate_snapshot
from .transfer import transaction


DATABASE_ENV = "DATABASE_URL"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
NONTERMINAL_BATCH_STATES = ("OPEN", "PREPARING", "READY", "RUNNING")
ROUTE_SETTINGS = SimpleNamespace(
    pinterest_board_write_scope_enabled=False,
    pinterest_board_provisioning_enabled=False,
)


def _hex64(value):
    _require(isinstance(value, str) and bool(HEX64.fullmatch(value)))
    return value


def _text(value, *, maximum=255):
    _require(
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and not any(ord(ch) < 32 for ch in value)
    )
    return value


def _route_candidate(db, plan, row):
    """Resolve exactly through the production board strategy; never provision."""
    intent_board = db.get(Board, row["local_board_id"])
    product = db.get(Product, row["product_id"])
    _require(
        intent_board is not None
        and intent_board.active is True
        and intent_board.store_id == plan["store_id"]
        and intent_board.slug == row["board_key_snapshot"]
        and product is not None
        and product.store_id == plan["store_id"]
    )

    route = board_strategy(
        db,
        canonical_key=row["board_key_snapshot"],
        settings=ROUTE_SETTINGS,
    )
    _require(
        route.get("status") == "ROUTE_EXISTING"
        and not route.get("blockers")
        and route.get("selected_board_id")
        and route.get("selected_external_board_id")
    )

    provider_board = db.get(PinterestBoard, route["selected_board_id"])
    connection = (
        db.get(PinterestConnection, provider_board.connection_id)
        if provider_board is not None else None
    )
    _require(
        provider_board is not None
        and connection is not None
        and provider_board.is_active is True
        and provider_board.is_eligible is True
        and connection.status == "CONNECTED"
        and provider_board.external_board_id == route["selected_external_board_id"]
    )

    metadata = row["selection_metadata"]
    _require(isinstance(metadata, dict))
    candidate_fingerprint = _hex64(metadata.get("candidate_fingerprint"))
    item_fingerprint = _hex64(row["item_fingerprint"])

    identity = {
        "item_id": _text(row["id"], maximum=36),
        "item_fingerprint": item_fingerprint,
        "candidate_fingerprint": candidate_fingerprint,
        "product_id": _text(row["product_id"], maximum=36),
        "local_board_id": _text(row["local_board_id"], maximum=36),
        "pinterest_board_record_id": _text(provider_board.id, maximum=36),
        "external_board_id": _text(provider_board.external_board_id),
        "content_angle_id": _text(row["content_angle_id"], maximum=36),
        "planned_date": row["planned_date"].isoformat(),
        "slot_index": int(row["slot_index"]),
    }
    identity["candidate_identity_fingerprint"] = digest(identity)
    return identity


def _stage(report, code):
    # Fixed constants only; never expose SQL, exception messages or identities.
    report["refusal_code"] = code


def _blocked_candidate_summary(db, plan_id, now, settings):
    """Aggregate only deterministic readiness categories on the same read-only snapshot."""
    statement = sa.select(PinterestPortfolioPlanItem).where(
        PinterestPortfolioPlanItem.plan_id == plan_id,
        PinterestPortfolioPlanItem.is_reserve.is_(False),
        PinterestPortfolioPlanItem.status == "PLANNED",
        PinterestPortfolioPlanItem.publication_id.is_(None),
        PinterestPortfolioPlanItem.planned_date >= now.date(),
        ~PinterestPortfolioPlanItem.id.in_(sa.select(entries.c.item_id)),
    ).order_by(
        PinterestPortfolioPlanItem.planned_date,
        PinterestPortfolioPlanItem.slot_index,
        PinterestPortfolioPlanItem.id,
    )
    counts = {
        "eligible_planned": 0,
        "execution_blocked": 0,
        "generation_blocked": 0,
        "missing_persisted_template": 0,
        "evaluation_error": 0,
        "generation_blocker_groups": {
            "seo": 0, "source_image": 0, "creative_layout": 0,
            "board_or_angle": 0, "duplicate_or_history": 0,
            "product": 0, "template": 0, "other": 0,
        },
    }
    internal = _execution_settings(settings)
    for item in db.scalars(statement).all():
        counts["eligible_planned"] += 1
        try:
            execution = execution_readiness(db, item.id, settings=internal, now=now)
            generation = autonomous_generation_preparation_readiness(
                db, item.id, settings=internal,
            )
            if execution.get("ready") is not True:
                counts["execution_blocked"] += 1
            if generation.get("ready") is not True:
                counts["generation_blocked"] += 1
                blockers = generation.get("blockers") or []
                if "CREATIVE_TEMPLATE_NOT_PERSISTED" in blockers:
                    counts["missing_persisted_template"] += 1
                # A single item can have multiple blocker groups; never log
                # raw error strings, candidate identities or private content.
                categories = set()
                for blocker in blockers:
                    if not isinstance(blocker, str):
                        categories.add("other")
                    elif "SEO" in blocker or "KEYWORD" in blocker:
                        categories.add("seo")
                    elif "IMAGE" in blocker or "MEDIA" in blocker:
                        categories.add("source_image")
                    elif "COPY" in blocker or "LAYOUT" in blocker or "TEXT" in blocker:
                        categories.add("creative_layout")
                    elif "BOARD" in blocker or "ANGLE" in blocker:
                        categories.add("board_or_angle")
                    elif "DUPLICATE" in blocker or "HISTORY" in blocker or "CONCEPT" in blocker:
                        categories.add("duplicate_or_history")
                    elif "PRODUCT" in blocker or "INVENTORY" in blocker:
                        categories.add("product")
                    elif "TEMPLATE" in blocker:
                        categories.add("template")
                    else:
                        categories.add("other")
                if not categories:
                    categories.add("other")
                for category in categories:
                    counts["generation_blocker_groups"][category] += 1
        except Exception:
            counts["evaluation_error"] += 1
    return counts


def database_snapshot(engine, report):
    """Exactly one PostgreSQL REPEATABLE READ / READ ONLY transaction."""
    with transaction(engine, readonly=True) as connection:
        report["database_transactions"] += 1
        _require(connection.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on")
        _require(connection.exec_driver_sql("SHOW transaction_isolation").scalar_one() == "repeatable read")

        _stage(report, "PREFLIGHT_SCHEMA_REVISION_OR_CATALOG")
        revisions = connection.exec_driver_sql(
            "SELECT version_num FROM public.alembic_version ORDER BY version_num"
        ).scalars().all()
        _require(revisions == ["0034"])
        validate_catalog(connection, "0034")

        _stage(report, "PREFLIGHT_ROUTINE_NOT_PAUSED")
        controls = connection.exec_driver_sql(
            "SELECT state FROM public.routine_publishing_control ORDER BY id LIMIT 2"
        ).scalars().all()
        _require(controls == ["PAUSED"])

        _stage(report, "PREFLIGHT_PUBLISH_UNKNOWN_PRESENT")
        publish_unknown_count = connection.exec_driver_sql(
            "SELECT count(*) FROM public.pin_publications "
            "WHERE status::text='PUBLISH_UNKNOWN'"
        ).scalar_one()
        _require(publish_unknown_count == 0)

        _stage(report, "PREFLIGHT_NONTERMINAL_BATCH_PRESENT")
        conflicting_batch_count = connection.exec_driver_sql(
            "SELECT count(*) FROM public.routine_autonomous_batches "
            "WHERE state IN ('OPEN','PREPARING','READY','RUNNING')"
        ).scalar_one()
        _require(conflicting_batch_count == 0)

        _stage(report, "PREFLIGHT_ACTIVE_PLAN_INVALID")
        current_date = connection.exec_driver_sql("SELECT CURRENT_DATE").scalar_one()
        month_start = current_date.replace(day=1)
        plans = connection.exec_driver_sql(
            "SELECT id,store_id,month_start,month_end,plan_fingerprint,status "
            "FROM public.pinterest_portfolio_plans "
            "WHERE status='ACTIVE' AND month_start=%s AND month_end >= %s "
            "ORDER BY id LIMIT 2",
            (month_start, current_date),
        ).mappings().all()
        _require(len(plans) == 1)
        plan = dict(plans[0])
        _text(plan["id"], maximum=36)
        _text(plan["store_id"], maximum=36)
        _hex64(plan["plan_fingerprint"])

        # Use the exact autonomous execution-readiness contract before freezing
        # candidate identity. Fetch one extra ready item only to prove that a
        # larger ready pool cannot expand the certified five-item manifest.
        current_timestamp = connection.exec_driver_sql("SELECT CURRENT_TIMESTAMP").scalar_one()
        db = Session(
            bind=connection,
            autoflush=False,
            expire_on_commit=False,
            join_transaction_mode="rollback_only",
        )
        try:
            _stage(report, "PREFLIGHT_READY_CANDIDATES_INSUFFICIENT")
            ready_items = _execution_ready_items(
                db,
                plan["id"],
                settings=get_settings(),
                now=current_timestamp,
                limit=6,
                lock=False,
            )
            if len(ready_items) < 5:
                report["readiness_blocker_counts"] = _blocked_candidate_summary(
                    db, plan["id"], current_timestamp, get_settings(),
                )
            _require(len(ready_items) >= 5)
            selected = ready_items[:5]
            selected_rows = [
                {
                    "id": item.id,
                    "slot_index": item.slot_index,
                    "planned_date": item.planned_date,
                    "product_id": item.product_id,
                    "local_board_id": item.local_board_id,
                    "board_key_snapshot": item.board_key_snapshot,
                    "content_angle_id": item.content_angle_id,
                    "item_fingerprint": item.item_fingerprint,
                    "selection_metadata": item.selection_metadata,
                }
                for item in selected
            ]
            _stage(report, "PREFLIGHT_CANDIDATE_ROUTING_INVALID")
            candidates = [_route_candidate(db, plan, row) for row in selected_rows]
        finally:
            db.close()

        _stage(report, "PREFLIGHT_CANDIDATE_IDENTITIES_INVALID")
        _require([candidate["slot_index"] for candidate in candidates] ==
                 [int(item.slot_index) for item in selected])
        _require(len({c["item_id"] for c in candidates}) == 5)
        _require(len({c["item_fingerprint"] for c in candidates}) == 5)
        _require(len({c["candidate_fingerprint"] for c in candidates}) == 5)
        _require(len({c["candidate_identity_fingerprint"] for c in candidates}) == 5)
        _require(len({
            (
                c["product_id"],
                c["local_board_id"],
                c["content_angle_id"],
                c["pinterest_board_record_id"],
                c["external_board_id"],
            )
            for c in candidates
        }) == 5)

        preflight_payload = {
            "contract": "FIVE_PIN_BOUNDED_PREFLIGHT_V1",
            "database_revision": "0034",
            "month_start": month_start.isoformat(),
            "current_date": current_date.isoformat(),
            "plan_id": plan["id"],
            "plan_fingerprint": plan["plan_fingerprint"],
            "candidates": candidates,
        }
        preflight_fingerprint = digest(preflight_payload)
        return {
            "database_revision": "0034",
            "schema_canonicality": "PASS",
            "routine_state": "PAUSED",
            "publish_unknown_count": 0,
            "conflicting_nonterminal_batch_count": 0,
            "month_start": month_start.isoformat(),
            "current_date": current_date.isoformat(),
            "plan_id": plan["id"],
            "plan_fingerprint": plan["plan_fingerprint"],
            "candidate_count": 5,
            "candidate_pool_has_more": len(ready_items) > 5,
            "candidates": candidates,
            "preflight_fingerprint": preflight_fingerprint,
        }


def run():
    report = {
        "success": False,
        "mode": "READ_ONLY_FIVE_PIN_BOUNDED_PREFLIGHT",
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
        "publication_creations": 0,
        "creative_creations": 0,
        "approval_creations": 0,
        "permit_creations": 0,
        "batch_creations": 0,
        "scheduler_activations": 0,
        "worker_activations": 0,
        "autonomy_activations": 0,
        "publishing_admission": "NOT_GRANTED",
        "bounded_preflight_certification": "NOT_GRANTED",
        "schema_canonicality": "NOT_GRANTED",
        "database_revision": None,
        "routine_state": None,
        "publish_unknown_count": None,
        "conflicting_nonterminal_batch_count": None,
        "candidate_count": 0,
        "candidate_pool_has_more": None,
        "gate_fingerprint": None,
        "plan_id": None,
        "plan_fingerprint": None,
        "preflight_fingerprint": None,
        "candidates": [],
        "refusal_code": "PREFLIGHT_GATES_INVALID",
        "readiness_blocker_counts": None,
    }
    previous = logging.root.manager.disable
    engine = None
    logging.disable(logging.CRITICAL)
    try:
        gates = gate_snapshot()
        report["gate_fingerprint"] = digest(gates)

        report["terminal_stage"] = "READ_ONLY_DATABASE"
        _stage(report, "PREFLIGHT_DATABASE_UNAVAILABLE")
        engine = sa.create_engine(
            _url(_env(DATABASE_ENV)),
            echo=False,
            hide_parameters=True,
            connect_args={"connect_timeout": 10},
        )
        report.update(database_snapshot(engine, report))
        engine.dispose()
        engine = None

        report["refusal_code"] = None
        report["bounded_preflight_certification"] = "PASS"
        report["terminal_stage"] = "COMPLETE"
        report["success"] = True
    except Exception:
        report["success"] = False
        report["bounded_preflight_certification"] = "NOT_GRANTED"
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                report.update(success=False, terminal_stage="DATABASE_CLOSE")
        logging.disable(previous)
    return report

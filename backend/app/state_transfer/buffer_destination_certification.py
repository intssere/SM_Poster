"""Strictly read-only Buffer destination certification.

This module is deliberately separate from publication preparation and live
dispatch. It performs one PostgreSQL READ ONLY / REPEATABLE READ transaction
plus exactly two Buffer read queries (organizations, then channels). It never
constructs or executes a Buffer mutation and never calls Pinterest/OAuth.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from types import SimpleNamespace

import sqlalchemy as sa

from app.integrations.buffer.gateway import BufferGateway
from .catalog import digest, validate_catalog
from .one_shot_migration import _env, _require, _url
from .production_media_certification import CLOSED_FALSE_GATES, SAFE_SCALARS, gate_snapshot
from .transfer import transaction


DATABASE_ENV = "DATABASE_URL"
BUFFER_ENVS = {
    "api_key": "BUFFER_API_KEY",
    "organization_id": "BUFFER_ORGANIZATION_ID",
    "channel_id": "BUFFER_PINTEREST_CHANNEL_ID",
}
ALLOWED_BUFFER_API_BASES = {"https://api.buffer.com", "https://api.buffer.com/"}
BUFFER_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")


def _identifier(value: str) -> str:
    _require(isinstance(value, str) and bool(BUFFER_IDENTIFIER_RE.fullmatch(value)))
    return value


def buffer_configuration():
    """Load the minimum Buffer configuration without deriving credential metadata."""
    api_key = _env(BUFFER_ENVS["api_key"])
    _require(bool(api_key.strip()) and not any(c.isspace() for c in api_key))
    organization_id = _identifier(_env(BUFFER_ENVS["organization_id"]))
    channel_id = _identifier(_env(BUFFER_ENVS["channel_id"]))
    api_base = os.environ.get("BUFFER_API_BASE", "https://api.buffer.com")
    _require(api_base in ALLOWED_BUFFER_API_BASES)

    settings = SimpleNamespace(
        buffer_api_key=api_key,
        buffer_api_base=api_base,
        buffer_organization_id=organization_id,
        buffer_pinterest_channel_id=channel_id,
        publishing_enabled=False,
        buffer_publishing_enabled=False,
    )
    safe = {
        "api_base": api_base.rstrip("/"),
        "organization_id": organization_id,
        "channel_id": channel_id,
        "credential_configured": True,
    }
    return settings, safe


def database_snapshot(engine, report):
    """Use exactly one read-only/repeatable-read transaction for local routing evidence."""
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

        connection_ids = connection.exec_driver_sql(
            "SELECT id FROM public.pinterest_connections "
            "WHERE status='CONNECTED' ORDER BY id LIMIT 2"
        ).scalars().all()
        _require(len(connection_ids) == 1)

        board_ids = connection.exec_driver_sql(
            "SELECT external_board_id FROM public.pinterest_boards "
            "WHERE connection_id=%s AND is_active IS TRUE AND is_eligible IS TRUE "
            "ORDER BY external_board_id",
            (connection_ids[0],),
        ).scalars().all()
        _require(bool(board_ids) and len(board_ids) == len(set(board_ids)))
        for board_id in board_ids:
            _identifier(board_id)

        database_fingerprint = digest({
            "revision": "0034",
            "routine_state": "PAUSED",
            "connected_connection_count": 1,
            "eligible_board_ids": board_ids,
        })
        return {
            "database_revision": "0034",
            "schema_canonicality": "PASS",
            "routine_state": "PAUSED",
            "connected_connection_count": 1,
            "local_eligible_board_count": len(board_ids),
            "local_board_ids": list(board_ids),
            "database_fingerprint": database_fingerprint,
        }


async def verify_buffer_destination(settings, safe_config, local_board_ids, report, gateway_factory=None):
    """Perform exactly two Buffer read operations; no mutation path is reachable here."""
    gateway_factory = gateway_factory or BufferGateway
    gateway = gateway_factory(settings)

    report["provider_calls"] += 1
    report["provider_reads"] += 1
    organizations = await gateway.organizations()
    organization_matches = [
        row for row in organizations if row.get("id") == safe_config["organization_id"]
    ]
    _require(len(organization_matches) == 1)

    report["provider_calls"] += 1
    report["provider_reads"] += 1
    channels = await gateway.channels(safe_config["organization_id"])
    channel_matches = [
        row for row in channels if row.get("id") == safe_config["channel_id"]
    ]
    _require(len(channel_matches) == 1)
    channel = channel_matches[0]
    _require(
        channel.get("service") == "pinterest"
        and channel.get("isDisconnected") is False
        and channel.get("isLocked") is False
    )

    provider_board_ids = [row.get("serviceId") for row in channel.get("boards", [])]
    _require(
        all(isinstance(value, str) and BUFFER_IDENTIFIER_RE.fullmatch(value)
            for value in provider_board_ids)
        and len(provider_board_ids) == len(set(provider_board_ids))
    )
    local_set = set(local_board_ids)
    provider_set = set(provider_board_ids)
    matched = sorted(local_set & provider_set)
    missing = sorted(local_set - provider_set)
    _require(not missing and len(matched) == len(local_board_ids))

    return {
        "organization_verified": True,
        "channel_verified": True,
        "channel_service": "pinterest",
        "channel_available": True,
        "provider_organization_count": len(organizations),
        "provider_channel_count": len(channels),
        "provider_board_count": len(provider_board_ids),
        "matched_local_board_count": len(matched),
        "missing_local_board_count": 0,
        "destination_fingerprint": digest({
            "organization_id": safe_config["organization_id"],
            "channel_id": safe_config["channel_id"],
            "matched_board_ids": matched,
        }),
    }


def run(*, gateway_factory=None):
    report = {
        "success": False,
        "mode": "READ_ONLY_BUFFER_DESTINATION_CERTIFICATION",
        "terminal_stage": "GATES",
        "database_transactions": 0,
        "database_writes": 0,
        "provider_calls": 0,
        "provider_reads": 0,
        "provider_writes": 0,
        "automatic_retries": 0,
        "publishing_admission": "NOT_GRANTED",
        "buffer_destination_certification": "NOT_GRANTED",
        "schema_canonicality": "NOT_GRANTED",
        "database_revision": None,
        "routine_state": None,
        "connected_connection_count": 0,
        "local_eligible_board_count": 0,
        "provider_organization_count": 0,
        "provider_channel_count": 0,
        "provider_board_count": 0,
        "matched_local_board_count": 0,
        "missing_local_board_count": None,
        "organization_verified": False,
        "channel_verified": False,
        "channel_available": False,
        "gate_fingerprint": None,
        "database_fingerprint": None,
        "buffer_configuration_fingerprint": None,
        "destination_fingerprint": None,
        "certification_fingerprint": None,
    }
    previous = logging.root.manager.disable
    engine = None
    logging.disable(logging.CRITICAL)
    try:
        gates = gate_snapshot()
        report["gate_fingerprint"] = digest(gates)

        report["terminal_stage"] = "CONFIGURATION"
        settings, safe_config = buffer_configuration()
        report["buffer_configuration_fingerprint"] = digest({
            "api_base": safe_config["api_base"],
            "organization_id": safe_config["organization_id"],
            "channel_id": safe_config["channel_id"],
            "credential_configured": True,
        })

        engine = sa.create_engine(
            _url(_env(DATABASE_ENV)),
            echo=False,
            hide_parameters=True,
            connect_args={"connect_timeout": 10},
        )
        report["terminal_stage"] = "READ_ONLY_DATABASE"
        database = database_snapshot(engine, report)
        local_board_ids = database.pop("local_board_ids")
        report.update(database)
        engine.dispose()
        engine = None

        report["terminal_stage"] = "BUFFER_READS"
        provider = asyncio.run(
            verify_buffer_destination(
                settings,
                safe_config,
                local_board_ids,
                report,
                gateway_factory=gateway_factory,
            )
        )
        report.update(provider)

        report["certification_fingerprint"] = digest({
            "gates": report["gate_fingerprint"],
            "database": report["database_fingerprint"],
            "configuration": report["buffer_configuration_fingerprint"],
            "destination": report["destination_fingerprint"],
        })
        report["buffer_destination_certification"] = "PASS"
        report["terminal_stage"] = "COMPLETE"
        report["success"] = True
    except Exception:
        report["success"] = False
        report["buffer_destination_certification"] = "NOT_GRANTED"
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                report.update(success=False, terminal_stage="DATABASE_CLOSE")
        logging.disable(previous)
    return report

"""Strictly read-only production media + closed-state certification.

This module is deliberately separate from readiness-management execution. It
never creates admissions/evidence, never mutates object storage, and never
loads Pinterest/Buffer/provider clients.
"""
from __future__ import annotations

from collections import Counter
import logging
import os

import sqlalchemy as sa

from app.services.media_storage import StorageMissing, StorageUnavailable
from app.services.s3_media_storage import S3Config
from .catalog import digest, validate_catalog
from .media_migration import bindings, verified
from .one_shot_migration import _env, _require, _url
from .policy import TARGET_ONLY
from .transfer import closed_state, count, transaction


DATABASE_ENV = "DATABASE_URL"
STORAGE_ENVS = {
    "endpoint": "OBJECT_STORAGE_ENDPOINT",
    "bucket": "OBJECT_STORAGE_BUCKET",
    "access_key": "OBJECT_STORAGE_ACCESS_KEY",
    "secret_key": "OBJECT_STORAGE_SECRET_KEY",
    "region": "OBJECT_STORAGE_REGION",
    "path_style": "OBJECT_STORAGE_PATH_STYLE",
}

CLOSED_FALSE_GATES = (
    "PUBLISHING_ENABLED",
    "BUFFER_PUBLISHING_ENABLED",
    "BUFFER_SINGLE_PIN_PILOT_ENABLED",
    "PINTEREST_SINGLE_PIN_PILOT_ENABLED",
    "PINTEREST_WRITE_SCOPE_ENABLED",
    "PINTEREST_BOARD_WRITE_SCOPE_ENABLED",
    "ROUTINE_PINTEREST_SCHEDULER_ENABLED",
    "ROUTINE_PINTEREST_WORKER_ENABLED",
    "ROUTINE_BUFFER_DISPATCH_ENABLED",
    "ROUTINE_AUTONOMOUS_AUTHORIZATION_ENABLED",
    "PINTEREST_PORTFOLIO_PLANNER_ENABLED",
    "PINTEREST_PORTFOLIO_ACTIVATION_ENABLED",
    "PINTEREST_OPTIMIZER_ENABLED",
    "PINTEREST_OPTIMIZER_APPLY_ENABLED",
    "PINTEREST_SEO_BRIEF_PERSISTENCE_ENABLED",
    "PINTEREST_ANALYTICS_INGESTION_ENABLED",
    "PINTEREST_LEARNING_SNAPSHOT_PERSISTENCE_ENABLED",
    "PINTEREST_AUTONOMOUS_GENERATION_ENABLED",
    "PINTEREST_AUTONOMOUS_EXECUTION_ENABLED",
    "PINTEREST_AUTONOMOUS_BOARD_ENSURE_ENABLED",
    "PINTEREST_BOARD_PROVISIONING_ENABLED",
    "ROUTINE_SCHEDULED_AUTONOMY_ENABLED",
    "ROUTINE_SCHEDULED_LIVE_ADMISSION_ENABLED",
    "ROUTINE_BOUNDED_BATCH_ENABLED",
    "OBJECT_STORAGE_READINESS_MANAGEMENT_ENABLED",
    "OBJECT_STORAGE_READINESS_PROBE_ENABLED",
    "ROUTINE_SCHEDULER_CANARY_ENABLED",
)
SAFE_SCALARS = {
    "ROUTINE_PINTEREST_DRY_RUN": "true",
    "ROUTINE_PINTEREST_BATCH_SIZE": "1",
    "ROUTINE_PINTEREST_DAILY_WRITE_LIMIT": "1",
    "AI_PROVIDER": "none",
}


def gate_snapshot():
    """Read only reviewed non-secret execution controls and require closed state."""
    result = {}
    for name in CLOSED_FALSE_GATES:
        value = os.environ.get(name)
        _require(value == "false")
        result[name] = False
    for name, expected in SAFE_SCALARS.items():
        value = os.environ.get(name)
        _require(value == expected)
        if expected in {"true", "false"}:
            result[name] = expected == "true"
        elif expected.isdigit():
            result[name] = int(expected)
        else:
            result[name] = expected
    return result


def storage_config():
    style = _env(STORAGE_ENVS["path_style"])
    _require(style in {"true", "false"})
    config = S3Config(
        endpoint=_env(STORAGE_ENVS["endpoint"]),
        bucket=_env(STORAGE_ENVS["bucket"]),
        access_key=_env(STORAGE_ENVS["access_key"]),
        secret_key=_env(STORAGE_ENVS["secret_key"]),
        region=_env(STORAGE_ENVS["region"]),
        path_style=style == "true",
    )
    _require(config.endpoint.startswith("https://"))
    return config


def database_snapshot(engine, report):
    """Use exactly one repeatable-read/read-only production transaction."""
    with transaction(engine, readonly=True) as c:
        report["database_transactions"] += 1
        _require(c.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on")
        _require(c.exec_driver_sql("SHOW transaction_isolation").scalar_one() == "repeatable read")

        revisions = c.exec_driver_sql(
            "SELECT version_num FROM public.alembic_version ORDER BY version_num"
        ).scalars().all()
        _require(revisions == ["0034"])
        validate_catalog(c, "0034")
        publications = closed_state(c)

        controls = c.exec_driver_sql(
            "SELECT state FROM public.routine_publishing_control ORDER BY id LIMIT 2"
        ).scalars().all()
        _require(controls == ["PAUSED"])

        target_only_counts = {name: count(c, name) for name in TARGET_ONLY}
        _require(target_only_counts["routine_autonomous_batches"] == 0)
        _require(target_only_counts["routine_autonomous_batch_entries"] == 0)

        rows = c.exec_driver_sql(
            "SELECT id,sha256,size_bytes,render_status FROM public.pin_creatives "
            "ORDER BY id LIMIT 18"
        ).mappings().all()
        authoritative = bindings(rows)

        binding_fingerprint = digest(authoritative)
        database_fingerprint = digest({
            "revision": "0034",
            "routine_state": "PAUSED",
            "publication_status_counts": publications,
            "target_only_counts": target_only_counts,
            "binding_fingerprint": binding_fingerprint,
        })
        return {
            "database_revision": "0034",
            "schema_canonicality": "PASS",
            "closed_state": "PASS",
            "routine_state": "PAUSED",
            "publication_status_counts": publications,
            "target_only_counts": target_only_counts,
            "bindings": authoritative,
            "binding_fingerprint": binding_fingerprint,
            "database_fingerprint": database_fingerprint,
        }


def verify_objects(config, authoritative, report, target_factory=None):
    """Exact-key GET only. There is intentionally no write/list/delete path."""
    if target_factory is None:
        from .migration_storage import S3ExactTarget
        target_factory = S3ExactTarget

    objects = [
        {
            "creative_id": item["creative_id"],
            "key": item["key"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "target_status": "NOT_RUN",
        }
        for item in authoritative
    ]
    report["objects"] = objects
    target = None
    try:
        target = target_factory(config, authoritative)
        for item in objects:
            report["object_reads"] += 1
            try:
                payload = target.get(item["key"], item["size_bytes"])
            except StorageMissing:
                item["target_status"] = "MISSING"
                continue
            except StorageUnavailable:
                item["target_status"] = "UNAVAILABLE"
                continue
            except Exception:
                item["target_status"] = "UNAVAILABLE"
                continue

            try:
                verified(payload, item)
            except Exception:
                item["target_status"] = "CONFLICT"
            else:
                item["target_status"] = "VERIFIED_EXISTING"

        _require(
            len(objects) == 17
            and all(item["target_status"] == "VERIFIED_EXISTING" for item in objects)
        )
    finally:
        if target is not None:
            try:
                target.close()
            except Exception:
                report["terminal_stage"] = "STORAGE_CLOSE"
                _require(False)


def run(*, target_factory=None):
    report = {
        "success": False,
        "mode": "READ_ONLY_CERTIFICATION",
        "terminal_stage": "GATES",
        "database_transactions": 0,
        "database_writes": 0,
        "object_reads": 0,
        "object_writes": 0,
        "provider_calls": 0,
        "automatic_retries": 0,
        "publishing_admission": "NOT_GRANTED",
        "media_certification": "NOT_GRANTED",
        "schema_canonicality": "NOT_GRANTED",
        "closed_state": "NOT_GRANTED",
        "database_revision": None,
        "routine_state": None,
        "authoritative_binding_count": 0,
        "gate_values": {},
        "objects": [],
        "target_counts": {},
        "binding_fingerprint": None,
        "database_fingerprint": None,
        "gate_fingerprint": None,
        "target_fingerprint": None,
        "certification_fingerprint": None,
    }
    previous = logging.root.manager.disable
    engine = None
    logging.disable(logging.CRITICAL)
    try:
        gates = gate_snapshot()
        report["gate_values"] = gates
        report["gate_fingerprint"] = digest(gates)

        report["terminal_stage"] = "CONFIGURATION"
        config = storage_config()
        report["storage_configuration_fingerprint"] = digest({
            "endpoint": config.endpoint,
            "bucket": config.bucket,
            "region": config.region,
            "path_style": config.path_style,
        })

        engine = sa.create_engine(
            _url(_env(DATABASE_ENV)),
            echo=False,
            hide_parameters=True,
            connect_args={"connect_timeout": 10},
        )
        report["terminal_stage"] = "READ_ONLY_DATABASE"
        db = database_snapshot(engine, report)
        report.update({k: v for k, v in db.items() if k != "bindings"})
        authoritative = db["bindings"]
        report["authoritative_binding_count"] = len(authoritative)
        engine.dispose()
        engine = None

        report["terminal_stage"] = "OBJECT_READS"
        verify_objects(config, authoritative, report, target_factory=target_factory)

        report["target_counts"] = dict(Counter(
            item["target_status"] for item in report["objects"]
        ))
        report["target_fingerprint"] = digest([
            {
                "key": item["key"],
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
                "status": "VERIFIED",
            }
            for item in report["objects"]
        ])
        report["certification_fingerprint"] = digest({
            "database": report["database_fingerprint"],
            "gates": report["gate_fingerprint"],
            "target": report["target_fingerprint"],
            "storage_configuration": report["storage_configuration_fingerprint"],
        })
        report["media_certification"] = "PASS"
        report["terminal_stage"] = "COMPLETE"
        report["success"] = True
    except Exception:
        report["success"] = False
        report["media_certification"] = "NOT_GRANTED"
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                report.update(success=False, terminal_stage="DATABASE_CLOSE")
        if report["objects"] and not report["target_counts"]:
            report["target_counts"] = dict(Counter(
                item["target_status"] for item in report["objects"]
            ))
        logging.disable(previous)
    return report

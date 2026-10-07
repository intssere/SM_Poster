"""Durable, sanitized READY-batch receipt storage and authenticated retrieval."""
from __future__ import annotations

import hashlib
import json
import os
import re
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.domain import AuditLog


ACTION = "bounded_pilot_ready_receipt_v1"
ACTOR = "bounded-ready-receipt-v1"
ENTITY_TYPE = "routine_autonomous_batch"
RECEIPT_VERSION = "FIVE_PIN_READY_BATCH_DURABLE_RECEIPT_V1"
RECEIPT_NAMESPACE = UUID("c44b25ab-4d9f-4af6-b65b-60f2d2db85e7")
LOG_PREFIX = "BOUNDED_READY_RECEIPT_JSON "
LOG_CHUNK_PREFIX = "BOUNDED_READY_RECEIPT_CHUNK "
LOG_NOT_FOUND = "BOUNDED_READY_RECEIPT_NOT_FOUND"
LOG_REFUSED = "BOUNDED_READY_RECEIPT_REFUSED"
LOG_CHUNK_BYTES = 512


def railway_production_receipt_logging_enabled() -> bool:
    return bool(os.environ.get("RAILWAY_ENVIRONMENT_ID")) and (
        os.environ.get("RAILWAY_ENVIRONMENT_NAME") == "production"
    )
HEX64 = re.compile(r"[0-9a-f]{64}\Z")

_COUNTER_FIELDS = (
    "database_writes",
    "object_storage_reads",
    "object_storage_writes",
    "provider_calls",
    "provider_reads",
    "provider_writes",
    "buffer_calls",
    "pinterest_calls",
    "oauth_calls",
    "ai_calls",
    "automatic_retries",
    "scheduler_activations",
    "worker_activations",
    "autonomy_activations",
    "provider_attempts_reserved",
)
_ENTRY_FIELDS = (
    "slot",
    "item_id",
    "product_id",
    "pinterest_board_record_id",
    "external_board_id",
    "item_fingerprint",
    "publication_id",
    "permit_id",
    "publication_fingerprint",
    "request_fingerprint",
    "scheduled_for",
    "permit_expires_at",
    "creative_id",
    "creative_sha256",
    "creative_size_bytes",
    "creative_rendered_url",
)


class ReadyReceiptError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str):
    raise ReadyReceiptError(code)


def _hex64(value):
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        _fail("READY_RECEIPT_INVALID")
    return value


def _text(value, maximum=255):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(ch) < 32 for ch in value)
    ):
        _fail("READY_RECEIPT_INVALID")
    return value


def _canonical_sha(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


def _sanitize_entry(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(_ENTRY_FIELDS):
        _fail("READY_RECEIPT_INVALID")
    if isinstance(value["slot"], bool) or not isinstance(value["slot"], int) or value["slot"] < 0:
        _fail("READY_RECEIPT_INVALID")
    for key in ("item_fingerprint", "publication_fingerprint", "request_fingerprint", "creative_sha256"):
        _hex64(value[key])
    for key, maximum in (
        ("item_id", 36),
        ("product_id", 36),
        ("pinterest_board_record_id", 36),
        ("external_board_id", 255),
        ("publication_id", 36),
        ("permit_id", 36),
        ("creative_id", 36),
        ("scheduled_for", 64),
        ("permit_expires_at", 64),
        ("creative_rendered_url", 2048),
    ):
        _text(value[key], maximum)
    if (
        isinstance(value["creative_size_bytes"], bool)
        or not isinstance(value["creative_size_bytes"], int)
        or value["creative_size_bytes"] <= 0
    ):
        _fail("READY_RECEIPT_INVALID")
    return {key: value[key] for key in _ENTRY_FIELDS}


def receipt_from_certification(report: dict) -> dict:
    if (
        not isinstance(report, dict)
        or report.get("success") is not True
        or report.get("mode") != "READ_ONLY_FIVE_PIN_READY_BATCH_CERTIFICATION"
        or report.get("terminal_stage") != "COMPLETE"
        or report.get("ready_batch_certification") != "PASS"
        or report.get("publishing_admission") != "NOT_GRANTED"
        or report.get("contract") != "FIVE_PIN_READY_BATCH_CERTIFICATION_V1"
        or report.get("database_revision") != "0034"
        or report.get("routine_state") != "PAUSED"
        or report.get("batch_state") != "READY"
        or report.get("target_count") != 5
        or report.get("attempts_reserved") != 0
        or report.get("admission_closed") is not False
        or report.get("candidate_count") != 5
        or report.get("database_transactions") != 1
    ):
        _fail("READY_CERTIFICATION_NOT_PASS")

    for key in _COUNTER_FIELDS:
        if report.get(key) != 0:
            _fail("READY_CERTIFICATION_NOT_PROVIDER_FREE")

    entries = report.get("entries")
    if not isinstance(entries, list) or len(entries) != 5:
        _fail("READY_RECEIPT_INVALID")
    safe_entries = [_sanitize_entry(dict(entry)) for entry in entries]
    if [entry["slot"] for entry in safe_entries] != list(range(5)):
        _fail("READY_RECEIPT_INVALID")

    payload = {
        "receipt_version": RECEIPT_VERSION,
        "certification_contract": report["contract"],
        "database_revision": report["database_revision"],
        "routine_state": report["routine_state"],
        "batch_id": _text(report.get("batch_id"), 36),
        "batch_manifest_sha256": _hex64(report.get("batch_manifest_sha256")),
        "ready_batch_fingerprint": _hex64(report.get("ready_batch_fingerprint")),
        "batch_state": "READY",
        "target_count": 5,
        "attempts_reserved": 0,
        "admission_closed": False,
        "plan_id": _text(report.get("plan_id"), 36),
        "plan_fingerprint": _hex64(report.get("plan_fingerprint")),
        "candidate_count": 5,
        "database_transactions": 1,
        **{key: 0 for key in _COUNTER_FIELDS},
        "publishing_admission": "NOT_GRANTED",
        "entries": safe_entries,
    }
    payload["receipt_sha256"] = _canonical_sha(payload)
    return payload


def validate_stored_receipt(value: dict) -> dict:
    if not isinstance(value, dict):
        _fail("STORED_READY_RECEIPT_INVALID")
    expected = {
        "receipt_version",
        "certification_contract",
        "database_revision",
        "routine_state",
        "batch_id",
        "batch_manifest_sha256",
        "ready_batch_fingerprint",
        "batch_state",
        "target_count",
        "attempts_reserved",
        "admission_closed",
        "plan_id",
        "plan_fingerprint",
        "candidate_count",
        "database_transactions",
        *_COUNTER_FIELDS,
        "publishing_admission",
        "entries",
        "receipt_sha256",
    }
    if set(value) != expected:
        _fail("STORED_READY_RECEIPT_INVALID")
    supplied_sha = value["receipt_sha256"]
    if not isinstance(supplied_sha, str):
        _fail("STORED_READY_RECEIPT_INVALID")
    payload = {key: value[key] for key in value if key != "receipt_sha256"}
    if _canonical_sha(payload) != supplied_sha:
        _fail("STORED_READY_RECEIPT_INVALID")

    # Re-run all semantic validation by projecting back into the certification shape.
    synthetic = {
        "success": True,
        "mode": "READ_ONLY_FIVE_PIN_READY_BATCH_CERTIFICATION",
        "terminal_stage": "COMPLETE",
        "ready_batch_certification": "PASS",
        "publishing_admission": value["publishing_admission"],
        "contract": value["certification_contract"],
        "database_revision": value["database_revision"],
        "routine_state": value["routine_state"],
        "batch_id": value["batch_id"],
        "batch_manifest_sha256": value["batch_manifest_sha256"],
        "ready_batch_fingerprint": value["ready_batch_fingerprint"],
        "batch_state": value["batch_state"],
        "target_count": value["target_count"],
        "attempts_reserved": value["attempts_reserved"],
        "admission_closed": value["admission_closed"],
        "plan_id": value["plan_id"],
        "plan_fingerprint": value["plan_fingerprint"],
        "candidate_count": value["candidate_count"],
        "database_transactions": value["database_transactions"],
        **{key: value[key] for key in _COUNTER_FIELDS},
        "entries": value["entries"],
    }
    rebuilt = receipt_from_certification(synthetic)
    if rebuilt != value:
        _fail("STORED_READY_RECEIPT_INVALID")
    return rebuilt


def receipt_id(ready_batch_fingerprint: str) -> str:
    return str(uuid5(RECEIPT_NAMESPACE, _hex64(ready_batch_fingerprint)))


def persist_receipt(db: Session, certification_report: dict) -> dict:
    receipt = receipt_from_certification(certification_report)
    row_id = receipt_id(receipt["ready_batch_fingerprint"])

    rows = list(db.scalars(
        sa.select(AuditLog).where(
            AuditLog.action == ACTION,
            AuditLog.entity_type == ENTITY_TYPE,
            AuditLog.entity_id == receipt["batch_id"],
        ).order_by(AuditLog.created_at, AuditLog.id)
    ).all())
    if rows:
        if len(rows) != 1:
            _fail("READY_RECEIPT_CONFLICT")
        existing = rows[0]
        try:
            stored = validate_stored_receipt(dict(existing.metadata_json or {}))
        except ReadyReceiptError:
            _fail("READY_RECEIPT_CONFLICT")
        if (
            existing.id != row_id
            or existing.actor != ACTOR
            or existing.correlation_id != receipt["ready_batch_fingerprint"]
            or stored != receipt
        ):
            _fail("READY_RECEIPT_CONFLICT")
        return {"created": False, "receipt_id": row_id, "receipt": stored}

    row = AuditLog(
        id=row_id,
        actor=ACTOR,
        action=ACTION,
        entity_type=ENTITY_TYPE,
        entity_id=receipt["batch_id"],
        correlation_id=receipt["ready_batch_fingerprint"],
        metadata_json=receipt,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(AuditLog, row_id)
        if existing is None:
            raise ReadyReceiptError("READY_RECEIPT_WRITE_FAILED") from None
        try:
            stored = validate_stored_receipt(dict(existing.metadata_json or {}))
        except ReadyReceiptError:
            raise ReadyReceiptError("READY_RECEIPT_CONFLICT") from None
        if (
            existing.actor != ACTOR
            or existing.action != ACTION
            or existing.entity_type != ENTITY_TYPE
            or existing.entity_id != receipt["batch_id"]
            or existing.correlation_id != receipt["ready_batch_fingerprint"]
            or stored != receipt
        ):
            raise ReadyReceiptError("READY_RECEIPT_CONFLICT")
        return {"created": False, "receipt_id": row_id, "receipt": stored}
    except Exception:
        db.rollback()
        raise ReadyReceiptError("READY_RECEIPT_WRITE_FAILED") from None
    return {"created": True, "receipt_id": row_id, "receipt": receipt}


def latest_receipt(db: Session) -> dict | None:
    rows = list(db.scalars(
        sa.select(AuditLog).where(
            AuditLog.action == ACTION,
            AuditLog.entity_type == ENTITY_TYPE,
        ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(2)
    ).all())
    if not rows:
        return None
    receipt = validate_stored_receipt(dict(rows[0].metadata_json or {}))
    if (
        rows[0].actor != ACTOR
        or rows[0].entity_id != receipt["batch_id"]
        or rows[0].correlation_id != receipt["ready_batch_fingerprint"]
        or rows[0].id != receipt_id(receipt["ready_batch_fingerprint"])
    ):
        _fail("STORED_READY_RECEIPT_INVALID")
    if len(rows) == 2 and rows[1].entity_id == rows[0].entity_id:
        _fail("STORED_READY_RECEIPT_INVALID")
    return {"receipt_id": rows[0].id, "receipt": receipt}


def _runtime_line(line: str, *, writer=None) -> None:
    if writer is None:
        print(line, flush=True)
    else:
        writer(line)


def emit_latest_receipt_to_runtime_log(*, session_factory=None, writer=None) -> bool:
    """Emit validated durable receipt as bounded stdout chunks.

    Railway documents stdout/stderr as the canonical retained log channel.
    Chunking prevents one oversized receipt line from becoming the transport
    boundary, while a whole-payload SHA permits deterministic reconstruction.
    """
    if session_factory is None:
        from app.db.session import SessionLocal
        session_factory = SessionLocal
    db = session_factory()
    try:
        result = latest_receipt(db)
        if result is None:
            _runtime_line(LOG_NOT_FOUND, writer=writer)
            return False

        serialized = json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        payload_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        chunks = [
            serialized[index:index + LOG_CHUNK_BYTES]
            for index in range(0, len(serialized), LOG_CHUNK_BYTES)
        ]
        if not chunks:
            _fail("STORED_READY_RECEIPT_INVALID")

        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            envelope = json.dumps(
                {
                    "index": index,
                    "total": total,
                    "payload_sha256": payload_sha256,
                    "data": chunk,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            _runtime_line(f"{LOG_CHUNK_PREFIX}{envelope}", writer=writer)
        return True
    except Exception:
        _runtime_line(LOG_REFUSED, writer=writer)
        return False
    finally:
        db.close()

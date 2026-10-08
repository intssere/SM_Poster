"""Railway runtime evidence for the live five-Pin READY certification."""
from __future__ import annotations

import hashlib
import json

LIVE_CERT_CHUNK_PREFIX = "BOUNDED_READY_LIVE_CERT_CHUNK "
LIVE_CERT_CHUNK_CHARS = 512


def _line(value: str, *, writer=None) -> None:
    if writer is None:
        print(value, flush=True)
    else:
        writer(value)


def _emit_payload(payload: dict, *, writer=None) -> None:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    payload_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    chunks = [
        serialized[index:index + LIVE_CERT_CHUNK_CHARS]
        for index in range(0, len(serialized), LIVE_CERT_CHUNK_CHARS)
    ] or [""]
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
            allow_nan=False,
        )
        _line(f"{LIVE_CERT_CHUNK_PREFIX}{envelope}", writer=writer)


def emit_live_ready_certification_to_runtime_log(*, certification_runner=None, writer=None) -> bool:
    """Run the strict provider-free READY certifier and emit its sanitized result."""
    if certification_runner is None:
        from app.state_transfer.ready_bounded_batch_certification import run
        certification_runner = run

    try:
        report = certification_runner()
        if not isinstance(report, dict):
            report = {
                "success": False,
                "mode": "READ_ONLY_FIVE_PIN_READY_BATCH_CERTIFICATION",
                "terminal_stage": "INVALID_REPORT",
                "ready_batch_certification": "NOT_GRANTED",
                "publishing_admission": "NOT_GRANTED",
            }
        _emit_payload(report, writer=writer)
        return bool(
            report.get("success") is True
            and report.get("ready_batch_certification") == "PASS"
            and report.get("publishing_admission") == "NOT_GRANTED"
        )
    except Exception:
        _emit_payload(
            {
                "success": False,
                "mode": "READ_ONLY_FIVE_PIN_READY_BATCH_CERTIFICATION",
                "terminal_stage": "RUNTIME_EVIDENCE_UNEXPECTED",
                "ready_batch_certification": "NOT_GRANTED",
                "publishing_admission": "NOT_GRANTED",
            },
            writer=writer,
        )
        return False


PREFLIGHT_CERT_CHUNK_PREFIX = "BOUNDED_PREFLIGHT_LIVE_CERT_CHUNK "


def emit_live_preflight_certification_to_runtime_log(*, certification_runner=None, writer=None) -> bool:
    """Report a fresh bounded preflight without leaking candidate identities.

    The existing certifier owns the single read-only PostgreSQL transaction,
    selection/readiness checks and fail-closed behavior. This transport never
    grants publishing admission, creates a batch, or persists a receipt.
    """
    if certification_runner is None:
        from app.state_transfer.bounded_pilot_preflight import run
        certification_runner = run

    safe = {
        "success": False,
        "mode": "READ_ONLY_FIVE_PIN_BOUNDED_PREFLIGHT",
        "bounded_preflight_certification": "NOT_GRANTED",
        "publishing_admission": "NOT_GRANTED",
        "terminal_stage": "RUNTIME_EVIDENCE_UNEXPECTED",
    }
    try:
        report = certification_runner()
        if isinstance(report, dict):
            # Allowlist only: no product IDs, plan IDs, external board IDs,
            # tokens, media, URLs or arbitrary exception messages enter logs.
            for key in (
                "refusal_code",
                "success", "bounded_preflight_certification", "publishing_admission",
                "terminal_stage", "database_revision", "schema_canonicality",
                "routine_state", "publish_unknown_count",
                "conflicting_nonterminal_batch_count", "candidate_count",
                "candidate_pool_has_more", "database_transactions",
                "database_writes", "provider_calls", "provider_reads",
                "provider_writes", "buffer_calls", "pinterest_calls",
                "oauth_calls", "ai_calls", "object_storage_reads",
                "object_storage_writes", "publication_creations",
                "creative_creations", "approval_creations", "permit_creations",
                "batch_creations", "scheduler_activations",
                "worker_activations", "autonomy_activations",
            ):
                value = report.get(key)
                if key == "refusal_code":
                    if value is None or value in {
                        "PREFLIGHT_GATES_INVALID", "PREFLIGHT_DATABASE_UNAVAILABLE",
                        "PREFLIGHT_SCHEMA_REVISION_OR_CATALOG", "PREFLIGHT_ROUTINE_NOT_PAUSED",
                        "PREFLIGHT_PUBLISH_UNKNOWN_PRESENT", "PREFLIGHT_NONTERMINAL_BATCH_PRESENT",
                        "PREFLIGHT_ACTIVE_PLAN_INVALID", "PREFLIGHT_READY_CANDIDATES_INSUFFICIENT",
                        "PREFLIGHT_CANDIDATE_ROUTING_INVALID", "PREFLIGHT_CANDIDATE_IDENTITIES_INVALID",
                    }:
                        safe[key] = value
                elif key == "terminal_stage":
                    if value in ("GATES", "READ_ONLY_DATABASE", "COMPLETE", "DATABASE_CLOSE"):
                        safe[key] = value
                elif key == "database_revision":
                    if value in (None, "0034"):
                        safe[key] = value
                elif key == "schema_canonicality":
                    if value in (None, "PASS", "NOT_GRANTED"):
                        safe[key] = value
                elif key == "routine_state":
                    if value in (None, "PAUSED"):
                        safe[key] = value
                elif key == "bounded_preflight_certification":
                    if value in ("PASS", "NOT_GRANTED"):
                        safe[key] = value
                elif key == "publishing_admission":
                    if value == "NOT_GRANTED":
                        safe[key] = value
                elif key == "success" or key == "candidate_pool_has_more":
                    if type(value) is bool:
                        safe[key] = value
                elif type(value) is int and value >= 0:
                    safe[key] = value
        counts = report.get("readiness_blocker_counts") if isinstance(report, dict) else None
        allowed_count_keys = {
            "eligible_planned", "execution_blocked", "generation_blocked",
            "missing_persisted_template", "evaluation_error",
        }
        if (
            safe.get("refusal_code") == "PREFLIGHT_READY_CANDIDATES_INSUFFICIENT"
            and isinstance(counts, dict) and set(counts) == allowed_count_keys
            and all(type(v) is int and 0 <= v <= 100000 for v in counts.values())
        ):
            safe["readiness_blocker_counts"] = {
                key: counts[key] for key in sorted(allowed_count_keys)
            }
        success = (
            safe["success"] is True
            and safe["bounded_preflight_certification"] == "PASS"
            and safe["publishing_admission"] == "NOT_GRANTED"
            and safe.get("candidate_count") == 5
            and safe.get("database_revision") == "0034"
            and safe.get("terminal_stage") == "COMPLETE"
            and safe.get("schema_canonicality") == "PASS"
            and safe.get("routine_state") == "PAUSED"
            and safe.get("publish_unknown_count") == 0
            and safe.get("conflicting_nonterminal_batch_count") == 0
            and safe.get("database_transactions") == 1
            and all(safe.get(key) == 0 for key in (
                "database_writes", "provider_calls", "provider_reads",
                "provider_writes", "buffer_calls", "pinterest_calls",
                "oauth_calls", "ai_calls", "object_storage_reads",
                "object_storage_writes", "publication_creations",
                "creative_creations", "approval_creations",
                "permit_creations", "batch_creations",
                "scheduler_activations", "worker_activations",
                "autonomy_activations",
            ))
        )
        if not success:
            safe.update(success=False, bounded_preflight_certification="NOT_GRANTED")
    except Exception:
        success = False
    serialized = json.dumps(safe, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    chunks = [serialized[i:i + LIVE_CERT_CHUNK_CHARS] for i in range(0, len(serialized), LIVE_CERT_CHUNK_CHARS)]
    for index, chunk in enumerate(chunks, start=1):
        envelope = json.dumps({
            "index": index, "total": len(chunks),
            "payload_sha256": payload_sha256, "data": chunk,
        }, sort_keys=True, separators=(",", ":"), allow_nan=False)
        _line(PREFLIGHT_CERT_CHUNK_PREFIX + envelope, writer=writer)
    return success

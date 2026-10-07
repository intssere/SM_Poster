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

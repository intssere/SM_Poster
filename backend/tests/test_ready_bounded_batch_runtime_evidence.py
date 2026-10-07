"""Task 61.61: live READY startup certification evidence regressions."""
from __future__ import annotations

import hashlib
import inspect
import json

from app.services import ready_bounded_batch_runtime_evidence as evidence


def _report(*, success=True):
    result = {
        "success": success,
        "mode": "READ_ONLY_FIVE_PIN_READY_BATCH_CERTIFICATION",
        "terminal_stage": "COMPLETE" if success else "READ_ONLY_DATABASE",
        "database_transactions": 1 if success else 0,
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
        "ready_batch_certification": "PASS" if success else "NOT_GRANTED",
        "batch_id": "batch-ready" if success else None,
        "batch_manifest_sha256": "1" * 64 if success else None,
        "ready_batch_fingerprint": "2" * 64 if success else None,
        "entries": [],
    }
    if success:
        result.update({
            "contract": "FIVE_PIN_READY_BATCH_CERTIFICATION_V1",
            "database_revision": "0034",
            "routine_state": "PAUSED",
            "batch_state": "READY",
            "target_count": 5,
            "attempts_reserved": 0,
            "admission_closed": False,
            "plan_id": "plan-ready",
            "plan_fingerprint": "3" * 64,
            "candidate_count": 5,
            "entries": [
                {
                    "slot": i,
                    "item_id": f"item-{i}",
                    "product_id": f"product-{i}",
                    "pinterest_board_record_id": f"board-{i}",
                    "external_board_id": f"external-{i}",
                    "item_fingerprint": f"{100+i:064x}",
                    "publication_id": f"pub-{i}",
                    "permit_id": f"permit-{i}",
                    "publication_fingerprint": f"{200+i:064x}",
                    "request_fingerprint": f"{300+i:064x}",
                    "scheduled_for": f"2026-10-{20+i:02d}T12:00:00+00:00",
                    "permit_expires_at": f"2026-10-{21+i:02d}T12:00:00+00:00",
                    "creative_id": f"creative-{i}",
                    "creative_sha256": f"{400+i:064x}",
                    "creative_size_bytes": 1000 + i,
                    "creative_rendered_url": f"/api/pins/creatives/creative-{i}/image",
                }
                for i in range(5)
            ],
        })
    return result


def _reconstruct(lines):
    assert lines
    assert all(line.startswith(evidence.LIVE_CERT_CHUNK_PREFIX) for line in lines)
    envelopes = [
        json.loads(line[len(evidence.LIVE_CERT_CHUNK_PREFIX):])
        for line in lines
    ]
    assert [entry["index"] for entry in envelopes] == list(range(1, len(envelopes) + 1))
    assert {entry["total"] for entry in envelopes} == {len(envelopes)}
    assert len({entry["payload_sha256"] for entry in envelopes}) == 1
    assert all(len(entry["data"]) <= evidence.LIVE_CERT_CHUNK_CHARS for entry in envelopes)
    serialized = "".join(entry["data"] for entry in envelopes)
    assert hashlib.sha256(serialized.encode("utf-8")).hexdigest() == envelopes[0]["payload_sha256"]
    return json.loads(serialized)


def test_live_ready_runtime_evidence_emits_exact_pass_report():
    expected = _report(success=True)
    lines = []
    assert evidence.emit_live_ready_certification_to_runtime_log(
        certification_runner=lambda: expected,
        writer=lines.append,
    ) is True
    assert _reconstruct(lines) == expected


def test_live_ready_runtime_evidence_emits_exact_sanitized_refusal():
    expected = _report(success=False)
    lines = []
    assert evidence.emit_live_ready_certification_to_runtime_log(
        certification_runner=lambda: expected,
        writer=lines.append,
    ) is False
    assert _reconstruct(lines) == expected


def test_live_ready_runtime_evidence_sanitizes_unexpected_exception():
    secret = "PRIVATE_DATABASE_URL_SENTINEL"

    def boom():
        raise RuntimeError(secret)

    lines = []
    assert evidence.emit_live_ready_certification_to_runtime_log(
        certification_runner=boom,
        writer=lines.append,
    ) is False
    parsed = _reconstruct(lines)
    assert secret not in json.dumps(parsed)
    assert parsed == {
        "success": False,
        "mode": "READ_ONLY_FIVE_PIN_READY_BATCH_CERTIFICATION",
        "terminal_stage": "RUNTIME_EVIDENCE_UNEXPECTED",
        "ready_batch_certification": "NOT_GRANTED",
        "publishing_admission": "NOT_GRANTED",
    }


def test_live_ready_runtime_evidence_defaults_to_stdout(capsys):
    expected = _report(success=False)
    assert evidence.emit_live_ready_certification_to_runtime_log(
        certification_runner=lambda: expected,
    ) is False
    output = capsys.readouterr()
    assert output.err == ""
    lines = [line for line in output.out.splitlines() if line]
    assert _reconstruct(lines) == expected


def test_runtime_evidence_source_has_no_provider_or_mutation_paths():
    source = inspect.getsource(evidence)
    forbidden = (
        "BufferGateway",
        "PinterestClient",
        "create_pinterest_post",
        "run_bounded_batch_once",
        "reserve_attempt",
        "persist_receipt",
        "SessionLocal",
        "db.add",
        "db.commit",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
    )
    for token in forbidden:
        assert token not in source
    assert "ready_bounded_batch_certification import run" in source


def test_main_lifespan_emits_live_certification_only_inside_railway_gate():
    import app.main as main

    source = inspect.getsource(main.lifespan)
    assert "if railway_production_receipt_logging_enabled()" in source
    assert "emit_live_ready_certification_to_runtime_log()" in source

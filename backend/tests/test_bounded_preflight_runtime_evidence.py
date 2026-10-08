"""Task 61.71: sanitized provider-free startup preflight evidence."""
from __future__ import annotations

import hashlib
import json

from app.services.ready_bounded_batch_runtime_evidence import (
    PREFLIGHT_CERT_CHUNK_PREFIX,
    emit_live_preflight_certification_to_runtime_log,
)


def _decode(lines):
    chunks = [json.loads(line.removeprefix(PREFLIGHT_CERT_CHUNK_PREFIX)) for line in lines]
    assert chunks
    assert [chunk["index"] for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert all(chunk["total"] == len(chunks) for chunk in chunks)
    serialized = "".join(chunk["data"] for chunk in chunks)
    assert all(chunk["payload_sha256"] == hashlib.sha256(serialized.encode()).hexdigest() for chunk in chunks)
    return json.loads(serialized)


def test_success_is_summary_only_without_candidate_or_secret_leakage():
    lines = []
    private = "PRIVATE_TOKEN_AND_PRODUCT_TITLE"
    report = {
        "success": True,
        "bounded_preflight_certification": "PASS",
        "publishing_admission": "NOT_GRANTED",
        "terminal_stage": "COMPLETE",
        "database_revision": "0034",
        "schema_canonicality": "PASS",
        "routine_state": "PAUSED",
        "candidate_count": 5,
        "database_transactions": 1,
        "database_writes": 0,
        "provider_calls": 0,
        "publish_unknown_count": 0,
        "conflicting_nonterminal_batch_count": 0,
        **{key: 0 for key in (
            "provider_reads", "provider_writes", "buffer_calls", "pinterest_calls",
            "oauth_calls", "ai_calls", "object_storage_reads", "object_storage_writes",
            "publication_creations", "creative_creations", "approval_creations",
            "permit_creations", "batch_creations", "scheduler_activations",
            "worker_activations", "autonomy_activations",
        )},
        "candidates": [{"product_id": private, "external_board_id": private}],
        "plan_id": private,
        "plan_fingerprint": private,
        "preflight_fingerprint": private,
    }
    assert emit_live_preflight_certification_to_runtime_log(
        certification_runner=lambda: report, writer=lines.append
    ) is True
    rendered = "\n".join(lines)
    assert private not in rendered
    result = _decode(lines)
    assert result["success"] is True
    assert result["candidate_count"] == 5
    assert result["publishing_admission"] == "NOT_GRANTED"
    assert "candidates" not in result
    assert "plan_id" not in result


def test_failure_remains_closed_and_does_not_echo_private_exception():
    lines = []
    def fail():
        raise RuntimeError("PRIVATE_DATABASE_URL_OR_ACCESS_TOKEN")

    assert emit_live_preflight_certification_to_runtime_log(
        certification_runner=fail, writer=lines.append
    ) is False
    assert "PRIVATE_DATABASE_URL_OR_ACCESS_TOKEN" not in "\n".join(lines)
    result = _decode(lines)
    assert result["success"] is False
    assert result["bounded_preflight_certification"] == "NOT_GRANTED"
    assert result["publishing_admission"] == "NOT_GRANTED"


def test_incomplete_report_cannot_claim_preflight_pass():
    lines = []
    assert emit_live_preflight_certification_to_runtime_log(
        certification_runner=lambda: {
            "success": True, "bounded_preflight_certification": "PASS",
            "publishing_admission": "NOT_GRANTED", "candidate_count": 4,
            "database_revision": "0034",
        }, writer=lines.append,
    ) is False
    assert _decode(lines)["bounded_preflight_certification"] == "NOT_GRANTED"


def test_allowlisted_refusal_and_aggregate_counts_are_rendered():
    lines = []
    counts = {
        "eligible_planned": 12,
        "execution_blocked": 2,
        "generation_blocked": 10,
        "missing_persisted_template": 8,
        "evaluation_error": 0,
        "generation_blocker_groups": {"seo": 10, "source_image": 0, "creative_layout": 0, "board_or_angle": 0, "duplicate_or_history": 0, "product": 0, "template": 0, "other": 0},
    }
    assert emit_live_preflight_certification_to_runtime_log(
        certification_runner=lambda: {
            "success": False,
            "bounded_preflight_certification": "NOT_GRANTED",
            "publishing_admission": "NOT_GRANTED",
            "terminal_stage": "READ_ONLY_DATABASE",
            "refusal_code": "PREFLIGHT_READY_CANDIDATES_INSUFFICIENT",
            "readiness_blocker_counts": counts,
            "candidate_count": 0,
        },
        writer=lines.append,
    ) is False
    report = _decode(lines)
    assert report["refusal_code"] == "PREFLIGHT_READY_CANDIDATES_INSUFFICIENT"
    assert report["readiness_blocker_counts"] == counts
    assert report["publishing_admission"] == "NOT_GRANTED"


def test_unknown_refusal_and_private_aggregate_are_not_logged():
    lines = []
    private = "PRIVATE_CATALOG_AND_TOKEN"
    assert emit_live_preflight_certification_to_runtime_log(
        certification_runner=lambda: {
            "success": False, "refusal_code": private,
            "readiness_blocker_counts": {"product_name": private},
            "publishing_admission": "NOT_GRANTED",
        }, writer=lines.append,
    ) is False
    assert private not in "\n".join(lines)
    result = _decode(lines)
    assert "refusal_code" not in result
    assert "readiness_blocker_counts" not in result

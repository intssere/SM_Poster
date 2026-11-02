"""Strict read-only READY certificate binding to a validated one-shot intent.

This pure module does not run a database query or grant publication admission.
A separately governed runner must invoke the existing read-only certificate
against the actual PostgreSQL database and pass its complete report here.
"""
from __future__ import annotations

from collections.abc import Mapping

from .one_shot_preparation_intent import OneShotPreparationIntent


class ReadyIntentBindingRefusal(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require_ready_certificate(intent: OneShotPreparationIntent, report: Mapping) -> bool:
    """Accept only independently certified, matching five-pin READY evidence."""
    def require(ok, code):
        if not ok:
            raise ReadyIntentBindingRefusal(code)

    require(type(intent) is OneShotPreparationIntent, "ONE_SHOT_VALIDATED_INTENT_REQUIRED")
    require(isinstance(report, Mapping), "ONE_SHOT_READY_CERTIFICATE_REQUIRED")
    require(
        report.get("success") is True
        and report.get("mode") == "READ_ONLY_FIVE_PIN_READY_BATCH_CERTIFICATION"
        and report.get("ready_batch_certification") == "PASS"
        and report.get("terminal_stage") == "COMPLETE",
        "ONE_SHOT_READY_CERTIFICATION_NOT_PASS",
    )
    require(
        report.get("database_revision") == "0035"
        and report.get("publishing_admission") == "NOT_GRANTED"
        and report.get("routine_state") == "PAUSED"
        and report.get("batch_state") == "READY",
        "ONE_SHOT_READY_CLOSED_STATE_REQUIRED",
    )
    for field in (
        "database_writes", "provider_calls", "provider_reads", "provider_writes",
        "buffer_calls", "pinterest_calls", "oauth_calls", "ai_calls",
        "automatic_retries", "scheduler_activations", "worker_activations",
        "autonomy_activations", "provider_attempts_reserved",
    ):
        require(type(report.get(field)) is int and report[field] == 0,
                "ONE_SHOT_READY_SIDE_EFFECT_EVIDENCE_INVALID")
    require(
        report.get("batch_id") == intent.batch_id
        and report.get("plan_id") == intent.plan_id
        and report.get("plan_fingerprint") == intent.plan_fingerprint,
        "ONE_SHOT_READY_IDENTITY_MISMATCH",
    )
    entries = report.get("entries")
    require(
        isinstance(entries, list) and len(entries) == 5
        and report.get("candidate_count") == 5
        and report.get("target_count") == 5,
        "ONE_SHOT_READY_FIVE_ENTRIES_REQUIRED",
    )
    require(
        all(isinstance(entry, Mapping) and type(entry.get("slot")) is int
            and entry.get("slot") == i and isinstance(entry.get("item_id"), str)
            for i, entry in enumerate(entries))
        and tuple(entry["item_id"] for entry in entries) == intent.candidate_item_ids
        and len(set(intent.candidate_item_ids)) == 5,
        "ONE_SHOT_READY_CANDIDATE_MISMATCH",
    )
    require(
        report.get("attempts_reserved") == 0
        and report.get("admission_closed") is False
        and isinstance(report.get("ready_batch_fingerprint"), str)
        and len(report["ready_batch_fingerprint"]) == 64
        and all(c in "0123456789abcdef" for c in report["ready_batch_fingerprint"]),
        "ONE_SHOT_READY_MANIFEST_INVALID",
    )
    return True

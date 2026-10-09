"""Pure fail-closed validation for a single fresh five-pin preflight receipt.

No database access, provider I/O, settings mutation, or batch side effects.
This module is only an admission prerequisite; it cannot execute preparation.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

CONTRACT = "FIVE_PIN_BOUNDED_PREFLIGHT_V1"
HEX64 = re.compile(r"[0-9a-f]{64}\\Z")
FIELDS = frozenset({
    "contract", "database_revision", "month_start", "current_date",
    "plan_id", "plan_fingerprint", "candidates", "preflight_fingerprint",
})
IDENTITY_FIELDS = frozenset({
    "item_id", "item_fingerprint", "candidate_fingerprint",
    "product_id", "local_board_id", "pinterest_board_record_id",
    "external_board_id", "content_angle_id", "planned_date",
    "slot_index", "candidate_identity_fingerprint",
})


class PreflightAdmissionRefusal(ValueError):
    """Static refusal code, never private receipt contents."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise PreflightAdmissionRefusal(code)


def _sha256(value: Mapping) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _hex(value: object) -> bool:
    return isinstance(value, str) and bool(HEX64.fullmatch(value))


def _safe_text(value: object, limit: int = 255) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= limit
            and not any(ord(char) < 32 for char in value))


def validate_preflight_for_one_shot(report: Mapping, *, today: str) -> dict:
    """Return the exact existing-service receipt, only on strict PASS.

    Caller must obtain report from an uncached fresh run in the SAME invocation.
    Caller must still atomically reserve an operation, revalidate under DB locks,
    and independently certify READY; none of those steps occur here.
    """
    _require(isinstance(report, Mapping), "ONE_SHOT_PREFLIGHT_INVALID")
    for key, value in {
        "success": True,
        "mode": "READ_ONLY_FIVE_PIN_BOUNDED_PREFLIGHT",
        "terminal_stage": "COMPLETE",
        "bounded_preflight_certification": "PASS",
        "schema_canonicality": "PASS",
        "database_revision": "0034",
        "routine_state": "PAUSED",
        "publish_unknown_count": 0,
        "conflicting_nonterminal_batch_count": 0,
        "candidate_count": 5,
        "publishing_admission": "NOT_GRANTED",
        "database_writes": 0,
        "provider_calls": 0,
        "buffer_calls": 0,
        "pinterest_calls": 0,
        "oauth_calls": 0,
        "ai_calls": 0,
        "object_storage_writes": 0,
        "batch_creations": 0,
        "permit_creations": 0,
        "publication_creations": 0,
        "scheduler_activations": 0,
        "worker_activations": 0,
        "autonomy_activations": 0,
        "refusal_code": None,
    }.items():
        _require(type(report.get(key)) is type(value) and report[key] == value,
                 "ONE_SHOT_PREFLIGHT_NOT_ADMITTED")

    _require(_safe_text(today, 10) and len(today) == 10,
             "ONE_SHOT_DATE_INVALID")
    _require(report.get("current_date") == today
             and report.get("month_start") == today[:7] + "-01",
             "ONE_SHOT_PREFLIGHT_STALE")
    _require(_safe_text(report.get("plan_id"), 36)
             and _hex(report.get("plan_fingerprint")),
             "ONE_SHOT_PLAN_INVALID")
    candidates = report.get("candidates")
    _require(isinstance(candidates, list) and len(candidates) == 5,
             "ONE_SHOT_CANDIDATE_COUNT_INVALID")
    for candidate in candidates:
        _require(isinstance(candidate, dict)
                 and set(candidate) == IDENTITY_FIELDS,
                 "ONE_SHOT_CANDIDATE_IDENTITY_INVALID")
        for key in ("item_id", "product_id", "local_board_id",
                    "pinterest_board_record_id", "content_angle_id"):
            _require(_safe_text(candidate[key], 36),
                     "ONE_SHOT_CANDIDATE_IDENTITY_INVALID")
        _require(_safe_text(candidate["external_board_id"]),
                 "ONE_SHOT_CANDIDATE_IDENTITY_INVALID")
        for key in ("item_fingerprint", "candidate_fingerprint",
                    "candidate_identity_fingerprint"):
            _require(_hex(candidate[key]), "ONE_SHOT_CANDIDATE_FINGERPRINT_INVALID")
        _require(type(candidate["slot_index"]) is int
                 and candidate["slot_index"] >= 0
                 and _safe_text(candidate["planned_date"], 10),
                 "ONE_SHOT_CANDIDATE_IDENTITY_INVALID")
        identity = {k: v for k, v in candidate.items()
                    if k != "candidate_identity_fingerprint"}
        _require(_sha256(identity) == candidate["candidate_identity_fingerprint"],
                 "ONE_SHOT_CANDIDATE_FINGERPRINT_MISMATCH")
    for key in ("item_id", "item_fingerprint", "candidate_fingerprint",
                "candidate_identity_fingerprint"):
        _require(len({c[key] for c in candidates}) == 5,
                 "ONE_SHOT_CANDIDATE_DUPLICATE")
    receipt = {
        "contract": CONTRACT,
        "database_revision": "0034",
        "month_start": report["month_start"],
        "current_date": report["current_date"],
        "plan_id": report["plan_id"],
        "plan_fingerprint": report["plan_fingerprint"],
        "candidates": candidates,
    }
    fingerprint = _sha256(receipt)
    _require(_hex(report.get("preflight_fingerprint"))
             and fingerprint == report["preflight_fingerprint"],
             "ONE_SHOT_PREFLIGHT_FINGERPRINT_MISMATCH")
    return {**receipt, "preflight_fingerprint": fingerprint}

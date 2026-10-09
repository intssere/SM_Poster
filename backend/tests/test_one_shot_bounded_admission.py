"""Pure, provider-free regression coverage for Task 61.76A admission."""
import hashlib
import json
from copy import deepcopy

import pytest

from app.state_transfer.one_shot_bounded_admission import (
    PreflightAdmissionRefusal, validate_preflight_for_one_shot,
)


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def sample():
    candidates = []
    for i in range(5):
        item = {
            "item_id": f"item-{i}",
            "item_fingerprint": f"{i+1:064x}",
            "candidate_fingerprint": f"{i+11:064x}",
            "product_id": f"product-{i}",
            "local_board_id": f"local-{i}",
            "pinterest_board_record_id": f"board-{i}",
            "external_board_id": f"external-{i}",
            "content_angle_id": f"angle-{i}",
            "planned_date": "2026-10-12",
            "slot_index": i,
        }
        item["candidate_identity_fingerprint"] = digest(item)
        candidates.append(item)
    receipt = {
        "contract": "FIVE_PIN_BOUNDED_PREFLIGHT_V1",
        "database_revision": "0034",
        "month_start": "2026-10-01",
        "current_date": "2026-10-09",
        "plan_id": "plan-1",
        "plan_fingerprint": "a" * 64,
        "candidates": candidates,
    }
    return {
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
        **receipt,
        "preflight_fingerprint": digest(receipt),
    }


def test_accepts_exact_five_pin_receipt_without_mutating_report():
    source = sample()
    original = deepcopy(source)
    result = validate_preflight_for_one_shot(source, today="2026-10-09")
    assert source == original
    assert result["preflight_fingerprint"] == source["preflight_fingerprint"]
    assert len(result["candidates"]) == 5


@pytest.mark.parametrize("change", [
    lambda r: r.update(success=False),
    lambda r: r.update(database_revision="0033"),
    lambda r: r.update(routine_state="RUNNING"),
    lambda r: r.update(publish_unknown_count=1),
    lambda r: r.update(conflicting_nonterminal_batch_count=1),
    lambda r: r.update(publishing_admission="GRANTED"),
    lambda r: r.update(provider_calls=1),
    lambda r: r.update(database_writes=1),
    lambda r: r.update(scheduler_activations=1),
    lambda r: r.update(candidate_count=6),
    lambda r: r["candidates"].pop(),
    lambda r: r["candidates"][1].update(item_id=r["candidates"][0]["item_id"]),
    lambda r: r["candidates"][0].update(product_id="substituted"),
    lambda r: r.update(preflight_fingerprint="b" * 64),
    lambda r: r.update(current_date="2026-10-08"),
    lambda r: r.update(refusal_code="PREFLIGHT_READY_CANDIDATES_INSUFFICIENT"),
])
def test_refuses_bad_receipt(change):
    data = sample()
    change(data)
    with pytest.raises(PreflightAdmissionRefusal):
        validate_preflight_for_one_shot(data, today="2026-10-09")


def test_refuses_python_bool_for_zero_counter():
    data = sample()
    data["provider_calls"] = False
    with pytest.raises(PreflightAdmissionRefusal):
        validate_preflight_for_one_shot(data, today="2026-10-09")

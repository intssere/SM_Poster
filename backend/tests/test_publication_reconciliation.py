import pytest
from types import SimpleNamespace

from app.services.publication_reconciliation import ReconciliationError, _pin, reconcile


@pytest.mark.parametrize("value", ["", " ", "a b", "https://x", "a/b", "a!", "x\n", "x" * 256])
def test_provider_pin_ids_reject_unsafe_values(value):
    with pytest.raises(ReconciliationError, match="INVALID_PROVIDER_PIN_ID"):
        _pin(value)


def test_provider_pin_ids_accept_token_style_values():
    assert _pin("Pin-01_v2.example:abc") == "Pin-01_v2.example:abc"


def test_reconcile_rejects_confirmation_and_unsupported_action():
    with pytest.raises(ReconciliationError, match="CONFIRMATION_REQUIRED"):
        reconcile(SimpleNamespace(), "missing", actor="admin", action="PROVIDER_PIN_CONFIRMED", confirmed=False)
    with pytest.raises(ReconciliationError, match="RECONCILIATION_REQUIRES_PUBLISH_UNKNOWN"):
        reconcile(SimpleNamespace(get=lambda *_: None), "missing", actor="admin", action="NOPE", confirmed=True)


def test_reconcile_requires_publish_unknown():
    db = SimpleNamespace(get=lambda *_: SimpleNamespace(status="SCHEDULED"))
    with pytest.raises(ReconciliationError, match="RECONCILIATION_REQUIRES_PUBLISH_UNKNOWN"):
        reconcile(db, "p", actor="admin", action="CANCELLED_UNKNOWN", confirmed=True, reason="stop")

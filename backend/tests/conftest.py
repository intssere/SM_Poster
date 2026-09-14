import pytest

from app.models.domain import PinApproval


@pytest.fixture(autouse=True)
def _normalize_buffer_phase2_original_provenance(request, monkeypatch):
    """Keep the historical phase-2 fixture aligned with valid approved-original provenance.

    test_buffer_phase2 predates ContentRevision persistence and synthesizes a non-null
    revision identity without creating that revision. Reconciliation now correctly
    rejects that impossible record. Patch only that test module's helper so its
    intended original-draft fixture is explicit and structurally valid.
    """
    module = request.module
    if module.__name__ != "test_buffer_phase2" or not hasattr(module, "_ready_publication"):
        return

    original = module._ready_publication

    def valid_ready_publication(db, *args, **kwargs):
        publication = original(db, *args, **kwargs)
        if kwargs.get("dispatch_provider") == "buffer":
            approval = db.get(PinApproval, publication.approval_id)
            approval.revision_id = None
            approval.approved_version_id = "original"
            publication.revision_id = None
            db.commit()
        return publication

    monkeypatch.setattr(module, "_ready_publication", valid_ready_publication)

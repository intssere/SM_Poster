"""PostgreSQL fixture/recovery coverage for Task #61.14 media durability.

All promotion targets are in-memory backends. The shared configured fixture
starts a disposable Unix-socket-only PostgreSQL cluster; this module never
constructs the attached Object Storage SDK client.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.domain import (
    AuditLog,
    PinApproval,
    PinConcept,
    PinCreative,
    PinDraft,
    PinPublication,
    PinterestAutonomousExecutionRun,
    PinterestAutonomousGenerationRun,
    PinterestPortfolioPlanItem,
    PublicationStatus,
)
from app.models.routine_publishing import RoutineDispatchPermit
from app.services import local_canary_admission
from app.services import pinterest_local_canary_fixture as fixture
from app.services import pinterest_local_canary_media as canary_media
from app.services.media_storage import (
    PNGMediaStorage,
    StorageMissing,
    StorageUnavailable,
)

from test_pinterest_local_canary_fixture_postgres import (
    db_session,
    postgres_url,
    ITEM_ID,
    NOW,
    SOURCE_ID,
    _rows,
    _source_bytes,
    configured as configured_task616a,
)


class FixtureMemoryBackend:
    """A strict process-local Object Storage double with observable writes."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.put_count = 0
        self.read_count = 0
        self.unavailable = False
        self.corrupt_reads = False

    def put(self, key: str, data: bytes) -> None:
        if self.unavailable:
            raise StorageUnavailable("isolated fixture storage is unavailable")
        self.put_count += 1
        self.objects[key] = data

    def get(self, key: str) -> bytes:
        if self.unavailable:
            raise StorageUnavailable("isolated fixture storage is unavailable")
        self.read_count += 1
        if key not in self.objects:
            raise StorageMissing("isolated fixture object is missing")
        contents = self.objects[key]
        if self.corrupt_reads:
            return contents + b"corrupt"
        return contents

    def exists(self, key: str) -> bool:
        if self.unavailable:
            raise StorageUnavailable("isolated fixture storage is unavailable")
        return key in self.objects


@pytest.fixture
def durable_context(configured_task616a, monkeypatch):
    db, development_settings, root = configured_task616a
    # Exposed policy is an explicit test input. Keep every publishing,
    # scheduler, autonomy, and provider gate in the original fixture closed.
    development_settings.app_env = "production"
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: development_settings
    )
    backend = FixtureMemoryBackend()
    storage = PNGMediaStorage(
        "creative",
        backend=backend,
        settings=SimpleNamespace(is_exposed=True),
    )

    # The admission code is called by schedule() after the creative has been
    # marked RENDERED. Bind that read to this test's storage and explicit
    # exposed policy; it must never discover workspace credentials.
    real_read = local_canary_admission.read_verified_promoted

    def read_with_test_storage(creative, *, digest=None, **kwargs):
        return real_read(
            creative,
            digest=digest,
            storage=storage,
            settings=development_settings,
            **kwargs,
        )

    monkeypatch.setattr(
        local_canary_admission,
        "read_verified_promoted",
        read_with_test_storage,
    )
    return db, development_settings, root, storage, backend


def _prepare_durable(durable_context):
    db, settings, root, storage, _ = durable_context
    return fixture.prepare_local_canary_fixture(
        db,
        ITEM_ID,
        source_image_id=SOURCE_ID,
        source_bytes=_source_bytes(),
        local_media_root=root,
        actor="local-test",
        media_storage=storage,
        settings=settings,
        now=NOW,
    )


def _reconcile_durable(durable_context, *, root=None):
    db, settings, original_root, storage, _ = durable_context
    return fixture.reconcile_local_canary_fixture(
        db,
        ITEM_ID,
        local_media_root=original_root if root is None else root,
        media_storage=storage,
        settings=settings,
        now=NOW,
    )


def _fixture_row_counts(db):
    """Counts for this disposable schema's fixture-created semantic rows."""
    return {
        "concepts": db.query(PinConcept).count(),
        "drafts": db.query(PinDraft).count(),
        "creatives": db.query(PinCreative).count(),
        "approvals": db.query(PinApproval).count(),
        "publications": db.query(PinPublication).count(),
        "generation_runs": db.query(PinterestAutonomousGenerationRun).count(),
        "execution_runs": db.query(PinterestAutonomousExecutionRun).count(),
        "permits": db.query(RoutineDispatchPermit).count(),
        "audit_rows": db.query(AuditLog).count(),
    }


def _make_pending_fixture(durable_context, monkeypatch):
    db, settings, root, storage, backend = durable_context
    real_promote = fixture.promote_staged_creative

    def stop_before_promotion(*_args, **_kwargs):
        raise canary_media.LocalCanaryMediaError("SIMULATED_INTERRUPTION")

    monkeypatch.setattr(fixture, "promote_staged_creative", stop_before_promotion)
    with pytest.raises(
        fixture.LocalCanaryFixtureError,
        match="PENDING_RECONCILIATION",
    ):
        fixture.prepare_local_canary_fixture(
            db,
            ITEM_ID,
            source_image_id=SOURCE_ID,
            source_bytes=_source_bytes(),
            local_media_root=root,
            actor="local-test",
            media_storage=storage,
            settings=settings,
            now=NOW,
        )
    monkeypatch.setattr(fixture, "promote_staged_creative", real_promote)
    item, execution, generation, publication, creative, approval = _rows(db)
    assert item.status == "PLANNED"
    assert execution.status == generation.status == "STARTED"
    assert publication.status == PublicationStatus.APPROVED
    assert creative.render_status == "STAGED"
    assert approval.decision == "APPROVED"
    assert db.query(RoutineDispatchPermit).count() == 0
    assert backend.put_count == 0
    return creative


def test_postgres_durable_fixture_promotes_readbacks_and_is_idempotent(
    durable_context,
):
    db, _, root, storage, backend = durable_context

    result = _prepare_durable(durable_context)

    item, execution, generation, publication, creative, approval = _rows(db)
    receipt = creative.render_spec["local_canary_stage"]
    assert result["status"] == "SUCCEEDED"
    assert result["provider_called"] is False
    assert result["ai_called"] is False
    assert result["external_requests"] == 0
    assert item.status == "SCHEDULED"
    assert execution.status == generation.status == "SUCCEEDED"
    assert publication.status == PublicationStatus.SCHEDULED
    assert creative.render_status == "RENDERED"
    assert approval.decision == "APPROVED"
    assert receipt["durable_protocol"] == canary_media.DURABLE_CANARY_PROTOCOL
    assert receipt["storage_key"] == storage.key(creative.id, creative.sha256)
    assert receipt["durable_receipt_fingerprint"]
    assert backend.put_count == 1
    stored_png = storage.read(creative.id, creative.sha256)
    assert stored_png.startswith(b"\x89PNG\r\n\x1a\n")
    assert hashlib.sha256(stored_png).hexdigest() == creative.sha256
    assert db.query(RoutineDispatchPermit).count() == 1

    counts = _fixture_row_counts(db)
    duplicate = fixture.prepare_local_canary_fixture(
        db,
        ITEM_ID,
        source_image_id=SOURCE_ID,
        source_bytes=_source_bytes(),
        local_media_root=root,
        actor="local-test",
        media_storage=storage,
        settings=durable_context[1],
        now=NOW,
    )
    assert duplicate["status"] == "SUCCEEDED"
    assert duplicate["execution_run_id"] == result["execution_run_id"]
    assert _fixture_row_counts(db) == counts
    assert backend.put_count == 1


def test_postgres_durable_receipt_reads_after_new_instance_has_no_local_file(
    durable_context, tmp_path,
):
    result = _prepare_durable(durable_context)
    db, settings, original_root, storage, backend = durable_context
    _, _, _, _, creative, _ = _rows(db)
    receipt = creative.render_spec["local_canary_stage"]
    assert result["status"] == "SUCCEEDED"
    assert backend.put_count == 1
    assert (original_root / receipt["final_path"]).exists() is False

    # A different worker/root can read the durable object with no local stage
    # or final artifact. Delete the now-unused originating staging directory.
    shutil.rmtree(original_root, ignore_errors=True)
    new_instance_root = tmp_path / "other-instance-media"
    new_instance_root.mkdir()
    reconciled = fixture.reconcile_local_canary_fixture(
        db,
        ITEM_ID,
        local_media_root=new_instance_root,
        media_storage=storage,
        settings=settings,
        now=NOW,
    )

    assert reconciled["status"] == "SUCCEEDED"
    assert backend.put_count == 1
    assert canary_media.read_verified_durable_creative(
        creative, storage=storage, settings=settings
    ) == storage.read(creative.id, creative.sha256)


@pytest.mark.parametrize("drift", ["origin", "snapshot"])
def test_postgres_succeeded_reconciliation_rejects_public_media_binding_drift(
    durable_context, drift,
):
    db, settings, _, _, backend = durable_context
    result = _prepare_durable(durable_context)
    _, _, _, publication, creative, _ = _rows(db)
    assert result["status"] == "SUCCEEDED"

    original_snapshot = publication.media_url_snapshot
    assert original_snapshot.startswith(settings.public_media_base_url)
    if drift == "origin":
        settings.public_media_base_url = "https://different-media.fixture-cdn.com"
    else:
        publication.media_url_snapshot = (
            "https://different-media.fixture-cdn.com/wrong.png"
        )
        db.commit()
        original_snapshot = publication.media_url_snapshot
    before_rows = _fixture_row_counts(db)
    before_reads = backend.read_count
    before_writes = backend.put_count
    # A changed configured origin or persisted URL must not make the object
    # appear correctly bound, or mutate any successful fixture state.
    with pytest.raises(
        fixture.LocalCanaryFixtureError,
        match="PENDING_RECONCILIATION",
    ) as error:
        _reconcile_durable(durable_context)

    assert isinstance(error.value.__cause__, fixture.LocalCanaryFixtureError)
    assert str(error.value.__cause__) == "LOCAL_CANARY_PUBLIC_MEDIA_MISMATCH"
    db.expire_all()
    _, _, _, current_publication, current_creative, _ = _rows(db)
    assert current_publication.media_url_snapshot == original_snapshot
    assert current_creative.render_status == "RENDERED"
    assert _fixture_row_counts(db) == before_rows
    assert backend.read_count == before_reads
    assert backend.put_count == before_writes == 1


@pytest.mark.parametrize("failure", ["unavailable", "corrupt_readback"])
def test_postgres_storage_failures_keep_fixture_staged_and_without_permit(
    durable_context, monkeypatch, failure,
):
    db, _, root, storage, backend = durable_context
    creative = _make_pending_fixture(durable_context, monkeypatch)

    if failure == "unavailable":
        backend.unavailable = True
    else:
        real_write = storage.write

        def write_then_corrupt(creative_id, contents):
            key = real_write(creative_id, contents)
            backend.objects[key] += b"changed-after-write"
            return key

        monkeypatch.setattr(storage, "write", write_then_corrupt)

    with pytest.raises(
        fixture.LocalCanaryFixtureError,
        match="PENDING_RECONCILIATION",
    ):
        _reconcile_durable(durable_context)

    db.expire_all()
    item, execution, generation, publication, persisted, _ = _rows(db)
    assert item.status == "PLANNED"
    assert execution.status == generation.status == "STARTED"
    assert publication.status == PublicationStatus.APPROVED
    assert persisted.render_status == "STAGED"
    assert persisted.render_spec["local_canary_stage"]["protocol"] == (
        canary_media.LOCAL_CANARY_PROTOCOL
    )
    assert "durable_protocol" not in persisted.render_spec["local_canary_stage"]
    assert db.query(RoutineDispatchPermit).count() == 0
    assert (root / persisted.render_spec["local_canary_stage"]["stage_path"]).is_file()
    assert persisted.id == creative.id


def test_postgres_preexisting_durable_object_recovers_without_duplicate_fixture(
    durable_context, monkeypatch,
):
    db, _, root, storage, backend = durable_context
    _make_pending_fixture(durable_context, monkeypatch)
    _, _, _, _, creative, _ = _rows(db)
    receipt = creative.render_spec["local_canary_stage"]
    stage_path = root / receipt["stage_path"]
    digest = receipt["artifact_sha256"]
    assert stage_path.is_file()

    # An object persisted before a crash is verified, not rewritten.
    key = storage.write(creative.id, stage_path.read_bytes())
    assert key == storage.key(creative.id, digest)
    assert backend.put_count == 1
    counts = _fixture_row_counts(db)

    result = _reconcile_durable(durable_context)

    assert result["status"] == "SUCCEEDED"
    assert backend.put_count == 1
    assert _fixture_row_counts(db) == {
        **counts,
        "permits": counts["permits"] + 1,
        "audit_rows": counts["audit_rows"] + 3,
    }
    assert not stage_path.exists()


def test_postgres_interrupted_finalization_reconciles_same_durable_object(
    durable_context, monkeypatch,
):
    db, _, root, storage, backend = durable_context
    creative = _make_pending_fixture(durable_context, monkeypatch)
    creative_id = creative.id
    real_commit = db.commit
    failed = False

    def interrupt_rendered_commit():
        nonlocal failed
        current = db.get(PinCreative, creative_id)
        if (
            not failed
            and backend.put_count == 1
            and current is not None
            and current.render_status == "RENDERED"
        ):
            failed = True
            raise RuntimeError("SIMULATED_INTERRUPTED_FINALIZATION_COMMIT")
        return real_commit()

    monkeypatch.setattr(db, "commit", interrupt_rendered_commit)
    with pytest.raises(
        fixture.LocalCanaryFixtureError,
        match="PENDING_RECONCILIATION",
    ):
        _reconcile_durable(durable_context)
    monkeypatch.setattr(db, "commit", real_commit)

    db.expire_all()
    item, execution, generation, publication, creative, _ = _rows(db)
    receipt = creative.render_spec["local_canary_stage"]
    assert failed
    assert backend.put_count == 1
    assert receipt["durable_protocol"] == canary_media.DURABLE_CANARY_PROTOCOL
    assert receipt["storage_key"] == storage.key(creative.id, creative.sha256)
    assert creative.render_status == "STAGED"
    assert item.status == "PLANNED"
    assert execution.status == generation.status == "STARTED"
    assert publication.status == PublicationStatus.APPROVED
    assert db.query(RoutineDispatchPermit).count() == 0
    assert not (root / receipt["stage_path"]).exists()

    before = _fixture_row_counts(db)
    result = _reconcile_durable(durable_context)

    assert result["status"] == "SUCCEEDED"
    assert backend.put_count == 1
    assert db.query(PinCreative).count() == before["creatives"]
    assert db.query(PinPublication).count() == before["publications"]
    assert db.query(PinterestAutonomousGenerationRun).count() == before["generation_runs"]
    assert db.query(PinterestAutonomousExecutionRun).count() == before["execution_runs"]
    assert db.query(RoutineDispatchPermit).count() == 1


@pytest.mark.parametrize(
    ("commit_mode", "receipt_committed", "stage_survives"),
    [
        ("before_commit", False, True),
        ("commit_succeeded_response_lost", True, True),
    ],
)
def test_postgres_interrupted_durable_receipt_commit_recovers_one_fixture(
    durable_context, monkeypatch, commit_mode, receipt_committed, stage_survives,
):
    db, _, root, storage, backend = durable_context
    _make_pending_fixture(durable_context, monkeypatch)
    _, _, _, _, initial_creative, _ = _rows(db)
    creative_id = initial_creative.id
    local_receipt = initial_creative.render_spec["local_canary_stage"]
    stage_path = root / local_receipt["stage_path"]
    assert stage_path.is_file()
    before_interruption = _fixture_row_counts(db)
    real_commit = db.commit
    interrupted = False

    def interrupt_durable_receipt_commit():
        nonlocal interrupted
        current = db.get(PinCreative, creative_id)
        receipt = (
            current.render_spec.get("local_canary_stage")
            if current is not None and isinstance(current.render_spec, dict)
            else None
        )
        if (
            not interrupted
            and backend.put_count == 1
            and current is not None
            and current.render_status == "STAGED"
            and isinstance(receipt, dict)
            and receipt.get("durable_protocol") == canary_media.DURABLE_CANARY_PROTOCOL
        ):
            interrupted = True
            if commit_mode == "commit_succeeded_response_lost":
                real_commit()
            raise RuntimeError("SIMULATED_DURABLE_RECEIPT_COMMIT_INTERRUPTION")
        return real_commit()

    monkeypatch.setattr(db, "commit", interrupt_durable_receipt_commit)
    with pytest.raises(
        fixture.LocalCanaryFixtureError,
        match="PENDING_RECONCILIATION",
    ):
        _reconcile_durable(durable_context)
    monkeypatch.setattr(db, "commit", real_commit)

    assert interrupted
    assert backend.put_count == 1
    db.expire_all()
    item, execution, generation, publication, creative, _ = _rows(db)
    persisted_receipt = creative.render_spec["local_canary_stage"]
    assert creative.render_status == "STAGED"
    assert item.status == "PLANNED"
    assert execution.status == generation.status == "STARTED"
    assert publication.status == PublicationStatus.APPROVED
    assert db.query(RoutineDispatchPermit).count() == 0
    assert persisted_receipt["protocol"] == canary_media.LOCAL_CANARY_PROTOCOL
    assert (
        persisted_receipt.get("durable_protocol")
        == (
            canary_media.DURABLE_CANARY_PROTOCOL
            if receipt_committed
            else None
        )
    )
    assert stage_path.is_file() is stage_survives
    assert _fixture_row_counts(db) == before_interruption

    counts_before_retry = _fixture_row_counts(db)
    recovered = _reconcile_durable(durable_context)

    assert recovered["status"] == "SUCCEEDED"
    assert backend.put_count == 1
    assert _fixture_row_counts(db) == {
        **counts_before_retry,
        "permits": counts_before_retry["permits"] + 1,
        "audit_rows": counts_before_retry["audit_rows"] + 3,
    }
    assert db.query(PinCreative).count() == 1
    assert db.query(PinPublication).count() == 1
    assert db.query(PinterestAutonomousGenerationRun).count() == 1
    assert db.query(PinterestAutonomousExecutionRun).count() == 1
    assert db.query(RoutineDispatchPermit).count() == 1


def test_postgres_orphan_cleanup_preserves_committed_stage_then_recovers(
    durable_context, monkeypatch,
):
    db, _, root, storage, backend = durable_context
    _make_pending_fixture(durable_context, monkeypatch)
    _, _, _, _, creative, _ = _rows(db)
    receipt = creative.render_spec["local_canary_stage"]
    referenced_stage = root / receipt["stage_path"]
    assert referenced_stage.is_file()

    orphan = canary_media.stage_creative_png(
        root,
        creative_id="unreferenced-orphan",
        png=_source_bytes(),
        source_image_id=SOURCE_ID,
        source_sha256="a" * 64,
        input_fingerprint="b" * 64,
        provenance={"fixture": "orphan-cleanup-regression"},
    )
    orphan_path = root / orphan.stage_path
    assert orphan_path.is_file()

    removed = fixture.cleanup_local_canary_orphans(db, root)

    assert orphan.stage_path in removed
    assert not orphan_path.exists()
    assert referenced_stage.is_file()
    assert backend.put_count == 0
    assert db.query(RoutineDispatchPermit).count() == 0

    result = _reconcile_durable(durable_context)
    assert result["status"] == "SUCCEEDED"
    assert backend.put_count == 1
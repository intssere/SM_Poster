from types import SimpleNamespace

import pytest

from app.models.domain import PinPublication
from app.services import local_canary_admission


class _CreativeSession:
    def __init__(self, creative):
        self.creative = creative

    def execute(self, statement):
        row = SimpleNamespace(
            id=self.creative.id,
            render_status=self.creative.render_status,
            render_spec=self.creative.render_spec,
            sha256=self.creative.sha256,
            source_image_id=self.creative.source_image_id,
        )
        return SimpleNamespace(one_or_none=lambda: row)


def _publication(creative_id="creative-local"):
    return PinPublication(id="publication-local", creative_id=creative_id)


def _creative(*, status="RENDERED", marker=True):
    render_spec = (
        {"local_canary_protocol": "TASK61_6A_LOCAL_MEDIA_STAGED_V1",
         "local_canary_stage": {"artifact_sha256": "a" * 64}}
        if marker
        else {}
    )
    return SimpleNamespace(
        id="creative-local",
        render_status=status,
        render_spec=render_spec,
        sha256="a" * 64,
        source_image_id="source-local",
    )


def test_staged_local_canary_media_is_blocked_without_reading_promoted_bytes(monkeypatch):
    creative = _creative(status="STAGED")
    monkeypatch.setattr(
        local_canary_admission,
        "read_verified_promoted",
        lambda *args, **kwargs: pytest.fail("staged media must not be read as promoted"),
    )

    assert local_canary_admission.publication_has_pending_local_canary_media(
        _CreativeSession(creative), _publication()
    )


@pytest.mark.parametrize("failure", [FileNotFoundError("missing"), ValueError("corrupt")])
def test_rendered_local_receipt_is_blocked_when_promoted_media_cannot_be_verified(
    monkeypatch, failure
):
    creative = _creative()
    reads = []

    def unavailable(current, *, digest):
        reads.append((current.id, digest))
        raise failure

    monkeypatch.setattr(local_canary_admission, "read_verified_promoted", unavailable)

    assert local_canary_admission.publication_has_pending_local_canary_media(
        _CreativeSession(creative), _publication()
    )
    assert reads == [(creative.id, creative.sha256)]


def test_rendered_local_receipt_is_admitted_only_after_verified_read(monkeypatch):
    creative = _creative()
    reads = []

    def verified(current, *, digest):
        reads.append((current.id, digest))
        return b"verified local artifact"

    monkeypatch.setattr(local_canary_admission, "read_verified_promoted", verified)

    assert not local_canary_admission.publication_has_pending_local_canary_media(
        _CreativeSession(creative), _publication()
    )
    assert reads == [(creative.id, creative.sha256)]


def test_unrelated_rendered_creative_does_not_require_local_artifact(monkeypatch):
    creative = _creative(marker=False)
    monkeypatch.setattr(
        local_canary_admission,
        "read_verified_promoted",
        lambda *args, **kwargs: pytest.fail("ordinary creatives do not use local receipt checks"),
    )

    assert not local_canary_admission.publication_has_pending_local_canary_media(
        _CreativeSession(creative), _publication()
    )
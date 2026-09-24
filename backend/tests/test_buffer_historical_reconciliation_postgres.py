from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.integrations.buffer.gateway import BufferPostSnapshot
from app.models.domain import (
    CreativeTemplate,
    PinCreative,
    PinDraft,
    PinPublication,
    PublicationAttempt,
    PublicationStatus,
)
from app.models.routine_publishing import RoutineDispatchPermit
from app.services.buffer_publication_reconciliation import (
    BufferReconciliationError,
    reconcile_buffer,
)
from app.services.fingerprints import publication_identity_fingerprint
from app.services.publication_scheduler import request_fingerprint_for
from app.services.routine_autonomous_authorization import AUTONOMOUS_ACTOR
from test_pinterest_autonomous_execution import (
    NOW,
    _fake_authorize,
    _fake_generation,
    _fake_seo,
    _seed,
)


POSTGRES_URL = os.getenv("TASK58_POSTGRES_URL")
SETTINGS = SimpleNamespace(
    buffer_organization_id="org",
    buffer_pinterest_channel_id="channel",
)
PIN_ID = "1093811828280487079"
OPERATION_ID = "6ab408353c9d972a44060ca0"


def _admin_url(url: str) -> str:
    return make_url(url).set(database="postgres").render_as_string(
        hide_password=False
    )


@pytest.fixture
def isolated_postgres() -> Iterator[str]:
    if not POSTGRES_URL:
        pytest.skip("TASK58_POSTGRES_URL is required")
    database = f"task5818_{uuid4().hex[:16]}"
    admin = sa.create_engine(
        _admin_url(POSTGRES_URL),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database}"'))
        url = make_url(POSTGRES_URL).set(database=database).render_as_string(
            hide_password=False
        )
        engine = sa.create_engine(url)
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        yield url
    finally:
        with admin.connect() as connection:
            connection.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname=:database AND pid <> pg_backend_pid()"
                ),
                {"database": database},
            )
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database}"'))
        admin.dispose()


class ExactGateway:
    def __init__(self, publication):
        self.publication = publication
        self.calls = []

    async def post(self, operation_id):
        self.calls.append(operation_id)
        return BufferPostSnapshot(
            buffer_post_id=operation_id,
            status="sent",
            channel_id="channel",
            created_at="2026-09-23T17:11:09.782Z",
            due_at=None,
            sent_at="2026-09-23T17:11:10.000Z",
            external_link=f"https://www.pinterest.com/pin/{PIN_ID}",
            channel_service="pinterest",
            text=self.publication.description_snapshot,
            pinterest_board_service_id=self.publication.pinterest_board_id_snapshot,
            pinterest_title=self.publication.title_snapshot,
            pinterest_url=self.publication.utm_url,
            image_url=self.publication.media_url_snapshot,
            image_alt_text=self.publication.alt_text_snapshot,
        )


def _historical_case(url: str):
    engine = sa.create_engine(url)
    Session = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    db = Session()

    seeded = _seed(db, two_same_day=False)
    board = seeded["board"]
    provider_board = seeded["provider_board"]
    connection = seeded["connection"]
    board.pinterest_board_id = provider_board.external_board_id
    db.commit()

    _fake_seo(db, seeded["item1"].id)
    generation = _fake_generation(db, seeded["item1"].id, now=NOW)
    approval = _fake_authorize(db, generation.draft_id, now=NOW)
    draft = db.get(PinDraft, generation.draft_id)
    creative = db.get(PinCreative, generation.creative_id)
    template = db.get(CreativeTemplate, creative.template_id)

    scheduled_for = NOW - timedelta(minutes=1)
    publication = PinPublication(
        id="phase-c-historical-publication",
        draft_id=draft.id,
        revision_id=None,
        creative_id=creative.id,
        approval_id=approval.id,
        source_image_id=creative.source_image_id,
        template_id=template.id,
        template_key=template.key,
        template_version=template.version,
        text_fingerprint=draft.text_fingerprint,
        creative_fingerprint=creative.creative_fingerprint,
        board_id=board.id,
        pinterest_board_id=provider_board.external_board_id,
        pinterest_connection_id=connection.id,
        pinterest_board_record_id=provider_board.id,
        pinterest_board_id_snapshot=provider_board.external_board_id,
        title_snapshot=draft.title,
        description_snapshot=draft.description,
        alt_text_snapshot=draft.alt_text,
        media_url_snapshot=creative.rendered_url,
        destination_url=draft.destination_url,
        utm_url=draft.utm_url,
        publication_fingerprint="pending",
        status=PublicationStatus.PUBLISH_UNKNOWN,
        created_at=NOW,
        scheduled_for=scheduled_for,
        error_code="BUFFER_SENT_LINK_UNVERIFIED",
    )
    publication.publication_fingerprint = publication_identity_fingerprint(
        draft_id=publication.draft_id,
        revision_id=publication.revision_id,
        creative_id=publication.creative_id,
        source_image_id=publication.source_image_id,
        board_id=publication.board_id,
        integration_account_id=publication.integration_account_id,
        destination_url=publication.destination_url,
        utm_url=publication.utm_url,
        pinterest_connection_id=publication.pinterest_connection_id,
        pinterest_board_record_id=publication.pinterest_board_record_id,
        pinterest_board_id_snapshot=publication.pinterest_board_id_snapshot,
    )
    db.add(publication)
    db.flush()

    attempt = PublicationAttempt(
        id="phase-c-historical-attempt",
        publication_id=publication.id,
        attempt_number=1,
        status="UNKNOWN",
        dispatch_provider="buffer",
        request_fingerprint=request_fingerprint_for(publication),
        error_code="BUFFER_SENT_LINK_UNVERIFIED",
        provider_operation_id=OPERATION_ID,
        provider_operation_status="sent",
        provider_external_link=f"https://www.pinterest.com/pin/{PIN_ID}",
        provider_submitted_at=NOW,
        provider_last_observed_at=NOW,
        safe_response_metadata={
            "buffer_organization_id": "org",
            "buffer_channel_id": "channel",
        },
    )
    db.add(attempt)
    db.flush()

    permit = RoutineDispatchPermit(
        id="phase-c-historical-permit",
        publication_id=publication.id,
        dispatch_provider="buffer",
        approval_id=approval.id,
        pinterest_board_record_id=provider_board.id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=attempt.request_fingerprint,
        scheduled_for_snapshot=scheduled_for,
        quality_policy_version="PINTEREST_QUALITY_V1",
        quality_snapshot={},
        duplicate_snapshot={},
        readiness_snapshot={"dispatch_provider": "buffer"},
        authorized_by=AUTONOMOUS_ACTOR,
        authorized_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
        status="CONSUMED",
        consumed_at=NOW - timedelta(minutes=4),
    )
    db.add(permit)
    db.commit()
    return engine, db, publication, attempt, permit


def test_postgres_historical_phase_c_reconciles_without_lineage_rewrite(
    isolated_postgres: str,
) -> None:
    engine, db, publication, attempt, permit = _historical_case(isolated_postgres)
    try:
        publication_fingerprint = publication.publication_fingerprint
        request_fingerprint = attempt.request_fingerprint
        board_id = publication.board_id

        gateway = ExactGateway(publication)
        result = asyncio.run(
            reconcile_buffer(
                db,
                publication.id,
                actor="operator",
                settings=SETTINGS,
                gateway=gateway,
            )
        )
        db.refresh(attempt)
        db.refresh(permit)

        assert gateway.calls == [OPERATION_ID]
        assert result.status == PublicationStatus.PUBLISHED
        assert result.pinterest_pin_id == PIN_ID
        assert result.board_id == board_id
        assert result.publication_fingerprint == publication_fingerprint
        assert attempt.request_fingerprint == request_fingerprint
        assert attempt.status == "SUCCEEDED"
        assert permit.status == "CONSUMED"
        assert db.query(PublicationAttempt).filter_by(
            publication_id=publication.id
        ).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_postgres_historical_phase_c_permit_drift_rejects_before_provider_read(
    isolated_postgres: str,
) -> None:
    engine, db, publication, attempt, permit = _historical_case(isolated_postgres)
    try:
        permit.request_fingerprint = "x" * 64
        db.commit()
        gateway = ExactGateway(publication)

        with pytest.raises(
            BufferReconciliationError,
            match="^BUFFER_POST_SNAPSHOT_MISMATCH$",
        ) as error:
            asyncio.run(
                reconcile_buffer(
                    db,
                    publication.id,
                    actor="operator",
                    settings=SETTINGS,
                    gateway=gateway,
                )
            )

        assert error.value.stage == "pre_provider"
        assert error.value.field == "destination_identity"
        assert gateway.calls == []
        db.refresh(publication)
        db.refresh(attempt)
        assert publication.status == PublicationStatus.PUBLISH_UNKNOWN
        assert attempt.status == "UNKNOWN"
        assert attempt.provider_pin_id is None
    finally:
        db.close()
        engine.dispose()

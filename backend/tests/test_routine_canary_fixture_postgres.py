from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import PinConcept, PinDraft, PinPublication, PublicationStatus, Store, Product, ProductImage, ContentAngle, CreativeTemplate, PinCreative, PinApproval, PinterestConnection, PinterestBoard
from app.models.routine_publishing import RoutineDispatchPermit, RoutinePublishingControl
from app.services import routine_canary_fixture as fixture


POSTGRES_URL = os.getenv("TASK58_POSTGRES_URL")
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _admin_url(url: str) -> str:
    return make_url(url).set(database="postgres").render_as_string(hide_password=False)


@pytest.fixture
def isolated_postgres() -> Iterator[str]:
    if not POSTGRES_URL:
        pytest.skip("TASK58_POSTGRES_URL is required")
    database = f"task594a_{uuid4().hex[:16]}"
    admin = sa.create_engine(_admin_url(POSTGRES_URL), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database}"'))
        url = make_url(POSTGRES_URL).set(database=database).render_as_string(hide_password=False)
        engine = sa.create_engine(url)
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        yield url
    finally:
        with admin.connect() as connection:
            connection.execute(sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=:database AND pid <> pg_backend_pid()"
            ), {"database": database})
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database}"'))
        admin.dispose()


def _settings(url: str) -> Settings:
    return Settings(
        database_url=url,
        publishing_enabled=False,
        buffer_publishing_enabled=False,
        buffer_single_pin_pilot_enabled=False,
        pinterest_single_pin_pilot_enabled=False,
        routine_pinterest_worker_enabled=False,
        routine_buffer_dispatch_enabled=False,
        routine_pinterest_scheduler_enabled=False,
        routine_autonomous_authorization_enabled=False,
        routine_pinterest_dry_run=True,
        routine_pinterest_batch_size=1,
        routine_pinterest_daily_write_limit=1,
        pinterest_autonomous_generation_enabled=False,
        pinterest_autonomous_execution_enabled=False,
        pinterest_autonomous_board_ensure_enabled=False,
        pinterest_write_scope_enabled=False,
        pinterest_board_write_scope_enabled=False,
        pinterest_board_provisioning_enabled=False,
    )


def test_postgres_control_row_lock_and_failed_postcondition_are_atomic(
    isolated_postgres: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine(isolated_postgres)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    try:
        db.add(RoutinePublishingControl(id="default", state="PAUSED", paused_at=NOW, paused_by="test"))
        store = Store(id="pg-store", name="Canary", shop_domain="canary.example")
        product = Product(
            id="pg-product", store_id="pg-store", shopify_product_id="pg-product",
            handle="canary", title="Canary", product_url="https://canary.example/p/canary",
        )
        angle = ContentAngle(id="pg-angle", key="canary", name="Canary")
        concept = PinConcept(
            id="pg-concept", store_id="pg-store", product_id="pg-product",
            content_angle_id="pg-angle", fingerprint="e" * 64,
        )
        draft = PinDraft(
            id="draft", concept_id="pg-concept", version=1, title="Canary",
            description="Canary", alt_text="Canary",
            destination_url="https://diamondshelf.us/p/canary",
            utm_url="https://diamondshelf.us/p/canary?utm_source=pinterest",
            text_fingerprint="b" * 64, status="APPROVED",
        )
        db.add(store)
        db.flush()
        db.add_all([product, angle])
        db.flush()
        db.add(concept)
        db.flush()
        db.add(draft)
        db.flush()
        image = ProductImage(
            id="source", product_id="pg-product",
            source_url="https://cdn.example/canary.jpg", is_primary=True,
        )
        template = CreativeTemplate(
            id="template", key="template", version=1, name="Canary",
        )
        db.add_all([image, template])
        db.flush()
        creative = PinCreative(
            id="creative", draft_id="draft", template_id="template",
            source_image_id="source", creative_fingerprint="c" * 64,
            render_status="RENDERED",
        )
        db.add(creative)
        db.flush()
        approval = PinApproval(
            id="approval", draft_id="draft", creative_id="creative",
            decision="APPROVED", decided_by="test",
        )
        db.add(approval)
        db.flush()
        connection = PinterestConnection(
            id="conn", external_user_id="test-user",
            access_token_ciphertext="test-ciphertext",
            refresh_token_ciphertext="test-refresh-ciphertext",
            status="CONNECTED",
        )
        db.add(connection)
        db.flush()
        board = PinterestBoard(
            id="board", connection_id="conn", external_board_id="external",
            name="Canary Board", is_active=True, is_eligible=True,
        )
        db.add(board)
        db.flush()
        db.add(PinPublication(
            id="pg-canary-source", draft_id="draft", creative_id="creative",
            approval_id="approval", pinterest_connection_id="conn",
            pinterest_board_record_id="board", pinterest_board_id_snapshot="external",
            publication_fingerprint="a" * 64, text_fingerprint="b" * 64,
            creative_fingerprint="c" * 64, status=PublicationStatus.APPROVED,
            title_snapshot="Canary", description_snapshot="Canary",
            alt_text_snapshot="Canary", destination_url="https://diamondshelf.us/p/canary",
            utm_url="https://diamondshelf.us/p/canary?utm_source=pinterest",
            media_url_snapshot="https://cdn.example/canary.jpg",
            source_image_id="source", template_id="template", template_key="template",
            template_version=1,
        ))
        db.commit()

        monkeypatch.setattr(fixture, "_persisted_routing_current", lambda *_: True)
        monkeypatch.setattr(fixture, "routine_readiness_snapshot", lambda *a, **k: {"ready": True, "alerts": []})
        calls = {"n": 0}
        def candidates(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"due_count":0,"active_permit_count":0,"valid_permit_count":0,
                        "routing_current_count":0,"eligible_count":0,
                        "eligible_publication_ids":[],"invalid_reasons":{}}
            return {"due_count":1,"active_permit_count":1,"valid_permit_count":1,
                    "routing_current_count":1,"eligible_count":0,
                    "eligible_publication_ids":[],"invalid_reasons":{"FORCED_POSTCONDITION_FAILURE":1}}
        monkeypatch.setattr(fixture, "routine_activation_candidate_snapshot", candidates)

        def create(_db, publication, *, actor, now, commit):
            row = RoutineDispatchPermit(
                id="pg-permit", publication_id=publication.id, dispatch_provider="buffer",
                approval_id=publication.approval_id,
                pinterest_board_record_id=publication.pinterest_board_record_id,
                publication_fingerprint=publication.publication_fingerprint,
                request_fingerprint="d" * 64, scheduled_for_snapshot=now,
                quality_policy_version="pinterest-quality-v1",
                quality_snapshot={"status":"PASS"},
                duplicate_snapshot={"status":"SAFE_TO_CONTINUE"},
                readiness_snapshot={"dispatch_provider":"buffer"},
                authorized_by=actor, authorized_at=now, expires_at=now,
                status="ACTIVE",
            )
            _db.add(row); _db.flush(); return row
        monkeypatch.setattr(fixture, "create_permit", create)
        monkeypatch.setattr(fixture, "build_routine_offline_evidence", lambda *a, **k: type("E", (), {"external_requests":0})())

        with pytest.raises(fixture.RoutineCanaryFixtureError, match="POSTCONDITION"):
            fixture.prepare_atomic_dry_run_canary_fixture(
                db, publication_id="pg-canary-source", actor="admin",
                settings=_settings(isolated_postgres), now=NOW,
                scheduler_snapshot={
                    "enabled":False,"started":False,"task_running":False,
                    "tick_running":False,"lease_supported":True,"lease_held":False,
                },
            )

        db.expire_all()
        publication = db.get(PinPublication, "pg-canary-source")
        assert publication.status == PublicationStatus.APPROVED
        assert publication.scheduled_for is None
        assert db.query(RoutineDispatchPermit).count() == 0
        assert db.get(RoutinePublishingControl, "default").state == "PAUSED"
    finally:
        db.close()
        engine.dispose()

"""Disposable PostgreSQL tests. No attached DB, credentials or provider I/O."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import threading
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db import bounded_batch_schema_0034 as schema
from app.db.migration_adoption import _table_fingerprints, verify_frozen_schema_at_head
from app.models import domain as d
from app.models.routine_publishing import (
    RoutineAttemptBoundary, RoutineDispatchPermit, RoutinePublishingControl,
    RoutineScheduledQuotaReservation,
)
from app.services import routine_bounded_batch as batch
from app.services.publication_scheduler import request_fingerprint_for
from app.services.routine_autonomous_authorization import AUTONOMOUS_ACTOR
from app.services.routine_buffer_dispatch import claim_for_routine, mark_provider_mutation_boundary


@pytest.fixture
def postgres():
    raw = os.environ.get("TASK61_DISPOSABLE_POSTGRES_URL")
    assert raw, "run through scripts/run_isolated_readiness_tests.py --with-postgres"
    url = make_url(raw)
    assert url.host in {None, "localhost", "127.0.0.1"}
    if url.host is None:
        assert url.query["host"].startswith("/tmp/readiness-ci-postgres-")
    name = "bounded_" + uuid4().hex
    admin = sa.create_engine(url.set(database="template1"), isolation_level="AUTOCOMMIT")
    # The runner freshly migrated this disposable cluster; template cloning
    # avoids running an application import/startup or migration per test.
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE postgres')
    engine = sa.create_engine(url.set(database=name))
    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, sessions
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


def settings(**changes):
    values = dict(
        database_url="sqlite:///:memory:", routine_bounded_batch_enabled=True,
        routine_pinterest_batch_size=5, routine_pinterest_daily_write_limit=5,
        publishing_enabled=True, buffer_publishing_enabled=True,
        routine_pinterest_worker_enabled=True, routine_buffer_dispatch_enabled=True,
        routine_scheduled_live_admission_enabled=True, routine_pinterest_dry_run=False,
    )
    return Settings(**{**values, **changes})


def _add(db, model, **values):
    """Supply harmless required non-FK seed fields; all FK identities explicit."""
    for column in model.__table__.columns:
        if (column.name not in values and not column.nullable and
                column.default is None and column.server_default is None):
            if column.foreign_keys:
                raise AssertionError(f"seed must bind {model.__name__}.{column.name}")
            if isinstance(column.type, sa.String):
                values[column.name] = "x" * (64 if column.type.length == 64 else 1)
            elif isinstance(column.type, (sa.Integer, sa.Numeric)):
                values[column.name] = 1
    obj = model(**values)
    db.add(obj)
    db.flush()
    return obj


def graph(db, count=6):
    now = datetime.now(timezone.utc)
    start = now.date().replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    store = _add(db, d.Store, id="store", name="test", shop_domain="test.invalid")
    connection = _add(db, d.PinterestConnection, id="connection", status="CONNECTED")
    board = _add(db, d.PinterestBoard, id="provider-board", connection_id=connection.id,
                 external_board_id="existing-board", is_active=True, is_eligible=True)
    local = _add(db, d.Board, id="local-board", store_id=store.id, slug="existing", active=True)
    angle = _add(db, d.ContentAngle, id="angle", key="angle")
    template = _add(db, d.CreativeTemplate, id="template")
    plan = _add(db, d.PinterestPortfolioPlan, id="plan", store_id=store.id,
                month_start=start, month_end=end, target_pins=150, status="ACTIVE")
    rows = []
    for i in range(count):
        product = _add(db, d.Product, id=f"product-{i}", store_id=store.id,
                       shopify_product_id=str(i), handle=f"product-{i}", vendor=f"vendor-{i}",
                       product_url="https://catalog.invalid/item")
        image = _add(db, d.ProductImage, id=f"image-{i}", product_id=product.id)
        concept = _add(db, d.PinConcept, id=f"concept-{i}", store_id=store.id,
                       product_id=product.id, content_angle_id=angle.id, board_id=local.id,
                       fingerprint=f"{i:064x}")
        draft = _add(db, d.PinDraft, id=f"draft-{i}", concept_id=concept.id)
        creative = _add(db, d.PinCreative, id=f"creative-{i}", draft_id=draft.id,
                        template_id=template.id, source_image_id=image.id,
                        creative_fingerprint=f"{i + 10:064x}")
        approval = _add(db, d.PinApproval, id=f"approval-{i}", draft_id=draft.id,
                        creative_id=creative.id, decision="APPROVED", decided_by=AUTONOMOUS_ACTOR)
        pub = _add(db, d.PinPublication, id=f"pub-{i}", draft_id=draft.id,
                   creative_id=creative.id, approval_id=approval.id,
                   pinterest_connection_id=connection.id, pinterest_board_record_id=board.id,
                   pinterest_board_id_snapshot=board.external_board_id,
                   publication_fingerprint=f"{i + 20:064x}", status=d.PublicationStatus.SCHEDULED,
                   scheduled_for=now - timedelta(minutes=1) if i < 5 else now + timedelta(days=1))
        item = _add(db, d.PinterestPortfolioPlanItem, id=f"item-{i}", plan_id=plan.id,
                    slot_index=i, product_id=product.id, local_board_id=local.id,
                    board_key_snapshot=local.slug, content_angle_id=angle.id,
                    angle_key_snapshot=angle.key, planned_date=pub.scheduled_for.date(), status="SCHEDULED",
                    selection_score=1, item_fingerprint=f"{i + 30:064x}", publication_id=pub.id)
        permit = _add(db, RoutineDispatchPermit, id=f"permit-{i}", publication_id=pub.id,
                      approval_id=approval.id, pinterest_board_record_id=board.id,
                      publication_fingerprint=pub.publication_fingerprint,
                      request_fingerprint=request_fingerprint_for(pub),
                      scheduled_for_snapshot=pub.scheduled_for, authorized_by=AUTONOMOUS_ACTOR,
                      authorized_at=now - timedelta(minutes=2), expires_at=now + timedelta(days=2))
        rows.append(dict(
            slot=i, item_id=item.id, product_id=product.id, board_id=board.id,
            external_board_id=board.external_board_id, item_fingerprint=item.item_fingerprint,
            publication_id=pub.id, permit_id=permit.id,
            publication_fingerprint=pub.publication_fingerprint,
            request_fingerprint=permit.request_fingerprint,
        ))
    control = db.get(RoutinePublishingControl, "default")
    control.state = "LIVE"
    db.commit()
    return rows


@pytest.fixture
def ready(postgres, monkeypatch):
    engine, sessions = postgres
    with sessions() as db:
        rows = graph(db)
        identity = batch.create_batch(db, settings=settings())
        db.execute(schema.batches.update().where(schema.batches.c.id == identity).values(state="PREPARING"))
        db.execute(schema.entries.insert(), [dict(batch_id=identity, **r) for r in rows[:5]])
        db.commit()
        batch.seal_batch(db, identity, settings=settings())
        owner = batch.acquire_batch(db, identity, settings=settings())
    # Content-policy behavior is covered by existing machine-authorization and
    # permit suites. Controller and quota/claim transactions remain real here.
    from app.services import routine_buffer_dispatch as dispatch
    monkeypatch.setattr(dispatch, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
    return engine, sessions, identity, owner, rows


def claim(db, identity, owner, row):
    return claim_for_routine(
        db, db.get(d.PinPublication, row["publication_id"]),
        db.get(RoutineDispatchPermit, row["permit_id"]), settings=settings(),
        bounded_batch_id=identity, bounded_owner=owner,
    )


def complete(db, identity, row, status=d.PublicationStatus.PUBLISHED):
    pub = db.get(d.PinPublication, row["publication_id"])
    pub.status = status
    if status == d.PublicationStatus.PUBLISHED:
        item = db.get(d.PinterestPortfolioPlanItem, row["item_id"])
        item.status = "PUBLISHED"
    db.commit()
    return batch.observe_batch(db, identity)


def snapshot(db, identity):
    return dict(db.execute(sa.select(schema.batches).where(
        schema.batches.c.id == identity)).mappings().one())


def test_migration_catalog_matches_frozen(postgres):
    with postgres[0].connect() as connection:
        assert _table_fingerprints(connection, schema.TABLES) == schema.FINGERPRINTS


def test_exactly_five_admitted_closed_then_reconciled(ready):
    _, sessions, identity, owner, rows = ready
    with sessions() as db:
        for i, row in enumerate(rows[:5]):
            attempt = claim(db, identity, owner, row)
            assert attempt is not None
            mark_provider_mutation_boundary(db, attempt.id, bounded_batch_id=identity, bounded_owner=owner)
            current = snapshot(db, identity)
            assert current["attempts_reserved"] == i + 1
            assert current["admission_closed"] is (i == 4)
            state = complete(db, identity, row)
            assert state == ("COMPLETED" if i == 4 else "RUNNING")
        assert db.scalar(sa.select(sa.func.count()).select_from(RoutineScheduledQuotaReservation)) == 5
        assert db.scalar(sa.select(sa.func.count()).select_from(RoutineAttemptBoundary)) == 5
        assert db.get(RoutinePublishingControl, "default").state == "PAUSED"
        with pytest.raises(batch.BoundedBatchError, match="CLOSED"):
            claim(db, identity, owner, rows[5])
        assert snapshot(db, identity)["attempts_reserved"] == 5


def test_concurrent_reservation_single_winner(ready):
    _, sessions, identity, owner, rows = ready
    barrier = threading.Barrier(2)

    def attempt():
        with sessions() as db:
            barrier.wait(timeout=5)
            try:
                result = claim(db, identity, owner, rows[0])
                return bool(result)
            except batch.BoundedBatchError:
                db.rollback()
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]
    with sessions() as db:
        assert snapshot(db, identity)["attempts_reserved"] == 1
        assert db.scalar(sa.select(sa.func.count()).select_from(RoutineAttemptBoundary)) == 1


def test_restart_resume_preserves_count_and_rejects_old_owner(ready):
    _, sessions, identity, owner, rows = ready
    with sessions() as db:
        claim(db, identity, owner, rows[0])
        complete(db, identity, rows[0])
        db.execute(schema.batches.update().where(schema.batches.c.id == identity).values(
            lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        db.commit()
    with sessions() as restarted:
        new_owner = batch.acquire_batch(restarted, identity, settings=settings())
        assert new_owner != owner
        claim(restarted, identity, new_owner, rows[1])
        assert snapshot(restarted, identity)["attempts_reserved"] == 2


def test_lease_excludes_competing_owner_and_expiry_closes(ready):
    _, sessions, identity, owner, rows = ready
    with sessions() as db:
        with pytest.raises(batch.BoundedBatchError, match="LEASE_HELD"):
            batch.acquire_batch(db, identity, settings=settings())
        db.rollback()
        db.execute(schema.batches.update().where(schema.batches.c.id == identity).values(
            lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        db.commit()
        with pytest.raises(batch.BoundedBatchError, match="LEASE_LOST"):
            claim(db, identity, owner, rows[0])
        assert snapshot(db, identity)["admission_closed"]


def test_exactly_five_manifest_required(postgres):
    with postgres[1]() as db:
        rows = graph(db)
        identity = batch.create_batch(db, settings=settings())
        db.execute(schema.batches.update().where(schema.batches.c.id == identity).values(state="PREPARING"))
        db.execute(schema.entries.insert(), [dict(batch_id=identity, **r) for r in rows[:4]])
        db.commit()
        with pytest.raises(batch.BoundedBatchError, match="EXACTLY_FIVE"):
            batch.seal_batch(db, identity, settings=settings())
        assert snapshot(db, identity)["admission_closed"]


def test_interrupted_reserved_attempt_not_retried(ready):
    _, sessions, identity, owner, rows = ready
    with sessions() as db:
        claim(db, identity, owner, rows[0])
        db.execute(schema.batches.update().where(schema.batches.c.id == identity).values(
            lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        db.commit()
    with sessions() as db:
        with pytest.raises(batch.BoundedBatchError, match="INTERRUPTED"):
            batch.acquire_batch(db, identity, settings=settings())
        assert snapshot(db, identity)["state"] == "PAUSED"
        assert snapshot(db, identity)["attempts_reserved"] == 1


@pytest.mark.parametrize("status,state", [
    (d.PublicationStatus.PUBLISH_FAILED, "FAILED"),
    (d.PublicationStatus.PUBLISH_UNKNOWN, "PAUSED"),
])
def test_failure_unknown_consumes_and_closes_without_replacement(ready, status, state):
    _, sessions, identity, owner, rows = ready
    with sessions() as db:
        claim(db, identity, owner, rows[0])
        assert complete(db, identity, rows[0], status) == state
        assert snapshot(db, identity)["attempts_reserved"] == 1
        assert snapshot(db, identity)["admission_closed"] is True
        with pytest.raises(batch.BoundedBatchError):
            claim(db, identity, owner, rows[1])
        assert db.scalar(sa.select(sa.func.count()).select_from(RoutineDispatchPermit).where(
            RoutineDispatchPermit.status == "ACTIVE")) == 1  # sixth, non-manifest permit only


@pytest.mark.parametrize("change", ["permit", "publication", "route", "fingerprint", "nonmanifest"])
def test_manifest_mismatch_fail_closed(ready, change):
    _, sessions, identity, owner, rows = ready
    row = dict(rows[0])
    with sessions() as db:
        if change == "permit":
            row["permit_id"] = rows[1]["permit_id"]
        elif change == "publication":
            row["publication_id"] = rows[1]["publication_id"]
        elif change == "route":
            db.get(d.PinPublication, row["publication_id"]).pinterest_board_id_snapshot = "drift"
            db.commit()
        elif change == "fingerprint":
            db.get(RoutineDispatchPermit, row["permit_id"]).publication_fingerprint = "f" * 64
            db.commit()
        else:
            row = rows[5]
        with pytest.raises(batch.BoundedBatchError, match="MISMATCH"):
            claim(db, identity, owner, row)
        assert snapshot(db, identity)["attempts_reserved"] == 0
        assert snapshot(db, identity)["admission_closed"]


def test_duplicate_dispatch_denied(ready):
    _, sessions, identity, owner, rows = ready
    with sessions() as db:
        claim(db, identity, owner, rows[0])
        complete(db, identity, rows[0])
        with pytest.raises(batch.BoundedBatchError):
            claim(db, identity, owner, rows[0])
        assert snapshot(db, identity)["attempts_reserved"] == 1


def test_utc_reset_cannot_expand_total(ready):
    _, sessions, identity, owner, rows = ready
    with sessions() as db:
        for row in rows[:5]:
            attempt = claim(db, identity, owner, row)
            mark_provider_mutation_boundary(db, attempt.id, now=datetime.now(timezone.utc)-timedelta(days=1),
                                           bounded_batch_id=identity, bounded_owner=owner)
            complete(db, identity, row)
        from app.services.routine_publishing_control import daily_provider_write_count
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        assert daily_provider_write_count(db, day_start=midnight) == 0
        with pytest.raises(batch.BoundedBatchError):
            claim(db, identity, owner, rows[5])
        assert snapshot(db, identity)["attempts_reserved"] == 5


@pytest.mark.parametrize("field", [
    "pinterest_board_provisioning_enabled", "pinterest_board_write_scope_enabled",
    "pinterest_autonomous_board_ensure_enabled", "routine_pinterest_scheduler_enabled",
    "routine_scheduled_autonomy_enabled",
    "pinterest_write_scope_enabled", "pinterest_single_pin_pilot_enabled", "buffer_single_pin_pilot_enabled",
])
def test_board_creation_and_recurring_expansion_denied(postgres, field):
    with postgres[1]() as db:
        with pytest.raises(batch.BoundedBatchError):
            batch.create_batch(db, settings=settings(**{field: True}))
        assert db.scalar(sa.select(sa.func.count()).select_from(schema.batches)) == 0


def test_disabled_gate_and_open_default_closed(postgres):
    with postgres[1]() as db:
        with pytest.raises(batch.BoundedBatchError, match="DISABLED"):
            batch.create_batch(db, settings=Settings(database_url="sqlite://"))
        identity = batch.create_batch(db, settings=settings())
        assert snapshot(db, identity)["state"] == "OPEN"
        assert snapshot(db, identity)["admission_closed"] is True
        with pytest.raises(batch.BoundedBatchError, match="CLOSED"):
            batch.acquire_batch(db, identity, settings=settings())


def test_disabled_gate_cannot_reclassify_manifest_as_ordinary(ready):
    from app.services.routine_buffer_dispatch import RoutineDispatchError
    _, sessions, _, _, rows = ready
    with sessions() as db:
        with pytest.raises(RoutineDispatchError, match="MANIFEST_CONTEXT"):
            claim_for_routine(db, db.get(d.PinPublication, rows[0]["publication_id"]),
                              db.get(RoutineDispatchPermit, rows[0]["permit_id"]),
                              settings=settings(routine_bounded_batch_enabled=False))


@pytest.mark.parametrize("operation", ["identity", "delete", "refund", "truncate"])
def test_database_guards_preserve_manifest_and_consumption(ready, operation):
    engine, sessions, identity, owner, rows = ready
    with sessions() as db:
        claim(db, identity, owner, rows[0])
    with engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError):
            if operation == "identity":
                connection.execute(schema.entries.update().where(schema.entries.c.batch_id == identity).values(product_id="product-5"))
            elif operation == "delete":
                connection.execute(schema.entries.delete().where(schema.entries.c.batch_id == identity))
            elif operation == "refund":
                connection.execute(schema.batches.update().where(schema.batches.c.id == identity).values(attempts_reserved=0))
            else:
                connection.exec_driver_sql("TRUNCATE routine_autonomous_batch_entries")
        connection.rollback()


def test_canonical_0034_and_guard_drift(postgres):
    engine, _ = postgres
    with engine.begin() as connection:
        verify_frozen_schema_at_head(connection, revision="0034")
        connection.exec_driver_sql("ALTER TABLE routine_autonomous_batch_entries DISABLE TRIGGER routine_autonomous_batch_entries_immutable")
        with pytest.raises(RuntimeError, match="trigger mismatch"):
            schema.verify(connection)
        connection.rollback()


def test_quota_failure_rolls_back_claim_not_safeguards(ready):
    from app.services.routine_buffer_dispatch import RoutineDispatchError
    _, sessions, identity, owner, rows = ready
    with sessions() as db:
        # Existing default monthly product cap is 3. Canonical commitments for
        # this product already exhaust it; the controller must not relax it.
        product = db.get(d.Product, rows[0]["product_id"])
        db.get(d.PinterestPortfolioPlan, "plan").target_pins = 1
        db.commit()
        with pytest.raises(RoutineDispatchError, match="QUOTA"):
            claim(db, identity, owner, rows[0])
        assert snapshot(db, identity)["attempts_reserved"] == 0
        assert db.get(RoutineDispatchPermit, rows[0]["permit_id"]).status == "ACTIVE"
        assert product.id == rows[0]["product_id"]


def test_invalid_state_transition_and_reopening_denied(ready):
    engine, sessions, identity, _, _ = ready
    with sessions() as db:
        batch.close_batch(db, identity)
        batch.close_batch(db, identity)  # idempotent
    with engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError):
            connection.execute(schema.batches.update().where(schema.batches.c.id == identity).values(
                state="RUNNING", admission_closed=False))
        connection.rollback()


def test_worker_without_batch_context_cannot_expand(ready):
    import asyncio
    from app.services.routine_pinterest_worker import run_once
    with ready[1]() as db:
        result = asyncio.run(run_once(db, settings=settings()))
        assert result == {"status": "BOUNDED_BATCH_CONTEXT_REQUIRED", "dispatched": 0}
        assert snapshot(db, ready[2])["attempts_reserved"] == 0


@pytest.mark.parametrize("unknown_at", [None, 1, 5])
def test_real_worker_dispatch_with_fake_provider_only(ready, monkeypatch, unknown_at):
    import asyncio
    from types import SimpleNamespace
    from app.services import routine_buffer_dispatch as dispatch
    from app.services import routine_pinterest_worker as worker
    _, sessions, identity, owner, _ = ready
    calls = []

    class FakeGateway:
        async def create_pinterest_post(self, payload):
            calls.append(payload["publication"])
            if len(calls) == unknown_at:
                raise RuntimeError("synthetic ambiguous transport failure")
            return SimpleNamespace(status="sent", buffer_post_id=str(len(calls)),
                                   external_link="https://pinterest.invalid/pin/test")

    async def evidence(*a, **k):
        return SimpleNamespace(observed_at=k["now"])

    async def destination(*a, **k):
        pass

    async def reconcile(db, publication_id, **kwargs):
        pub = db.get(d.PinPublication, publication_id)
        pub.status = d.PublicationStatus.PUBLISHED
        item = db.scalar(sa.select(d.PinterestPortfolioPlanItem).where(
            d.PinterestPortfolioPlanItem.publication_id == publication_id))
        item.status = "PUBLISHED"
        db.commit()

    monkeypatch.setattr(worker, "build_routine_execution_evidence", evidence)
    monkeypatch.setattr(worker, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
    monkeypatch.setattr(dispatch, "evidence_matches", lambda *a, **k: True)
    monkeypatch.setattr(dispatch, "verify_destination", destination)
    monkeypatch.setattr(dispatch, "build_pinterest_payload", lambda pub, s: {"publication": pub.id})
    monkeypatch.setattr(dispatch, "reconcile_buffer", reconcile)
    with sessions() as db:
        result = asyncio.run(worker.run_once(
            db, settings=settings(), gateway=FakeGateway(),
            bounded_batch_id=identity, bounded_owner=owner))
        expected = 5 if unknown_at is None else unknown_at
        assert len(calls) == len(set(calls)) == expected, result
        assert snapshot(db, identity)["attempts_reserved"] == expected
        assert snapshot(db, identity)["admission_closed"]
        assert snapshot(db, identity)["state"] == ("COMPLETED" if unknown_at is None else "PAUSED")
        assert result["published"] == (5 if unknown_at is None else unknown_at - 1)
        assert result["unknown"] == (0 if unknown_at is None else 1)
        assert db.get(RoutinePublishingControl, "default").state == "PAUSED"
        # A subsequent scheduler-style tick cannot extend this frozen batch.
        retry = asyncio.run(worker.run_once(db, settings=settings()))
        assert retry["dispatched"] == 0
        assert len(calls) == expected


def test_autonomous_preparation_freezes_first_five_and_machine_permits(postgres, monkeypatch):
    from types import SimpleNamespace
    from app.services import routine_bounded_preparation as preparation
    with postgres[1]() as db:
        rows = graph(db)
        for row in rows:
            item = db.get(d.PinterestPortfolioPlanItem, row["item_id"])
            item.publication_id = None
            item.status = "PLANNED"
        db.commit()
        identity = batch.create_batch(db, settings=settings())
        selected = []
        monkeypatch.setattr(preparation, "board_strategy", lambda *a, **k: {
            "status": "ROUTE_EXISTING", "selected_board_id": rows[0]["board_id"],
            "selected_external_board_id": rows[0]["external_board_id"],
        })

        def execute(db, item_id, **kwargs):
            # Fake renderer/execution, but durable graph/manifest/permit checks
            # are real. Existing execution policy suites exercise actual stages.
            selected.append(item_id)
            row = next(r for r in rows if r["item_id"] == item_id)
            db.get(d.PinterestPortfolioPlanItem, item_id).publication_id = row["publication_id"]
            db.commit()
            return SimpleNamespace(status="SUCCEEDED", stage="PERMITTED",
                                   publication_id=row["publication_id"], routine_permit_id=row["permit_id"])

        monkeypatch.setattr(preparation, "execute_autonomous_item", execute)
        preparation.prepare_batch(db, identity, "plan", settings=settings())
        assert selected == [r["item_id"] for r in rows[:5]]
        assert len(batch._rows(db, identity)) == 5
        assert snapshot(db, identity)["state"] == "READY"
        preparation.prepare_batch(db, identity, "plan", settings=settings())
        assert len(selected) == 5  # idempotent, no replacements


def test_preparation_does_not_create_missing_board(postgres, monkeypatch):
    from app.services import routine_bounded_preparation as preparation
    with postgres[1]() as db:
        rows = graph(db)
        for row in rows:
            item = db.get(d.PinterestPortfolioPlanItem, row["item_id"])
            item.publication_id = None
            item.status = "PLANNED"
        db.commit()
        identity = batch.create_batch(db, settings=settings())
        monkeypatch.setattr(preparation, "board_strategy", lambda *a, **k: {"status": "CREATE_REQUIRED"})
        monkeypatch.setattr(preparation, "execute_autonomous_item", lambda *a, **k: pytest.fail("must not generate"))
        with pytest.raises(batch.BoundedBatchError, match="EXISTING_BOARD"):
            preparation.prepare_batch(db, identity, "plan", settings=settings())
        assert snapshot(db, identity)["admission_closed"]


def test_preparing_candidate_protected_before_publication_binding(postgres, monkeypatch):
    from app.services.routine_buffer_dispatch import RoutineDispatchError
    from app.services import routine_buffer_dispatch as dispatch
    with postgres[1]() as db:
        rows = graph(db)
        identity = batch.create_batch(db, settings=settings())
        db.execute(schema.batches.update().where(schema.batches.c.id == identity).values(state="PREPARING"))
        db.execute(schema.entries.insert(), [{**{k: r[k] for k in (
            "slot", "item_id", "product_id", "board_id", "external_board_id", "item_fingerprint",
        )}, "batch_id": identity} for r in rows[:5]])
        db.commit()
        assert batch.member_batch(db, rows[0]["publication_id"]) == identity
        monkeypatch.setattr(dispatch, "validate_permit", lambda *a, **k: {"valid": True})
        with pytest.raises(RoutineDispatchError, match="MANIFEST_CONTEXT"):
            claim_for_routine(db, db.get(d.PinPublication, rows[0]["publication_id"]),
                              db.get(RoutineDispatchPermit, rows[0]["permit_id"]),
                              settings=settings(routine_bounded_batch_enabled=False))
        batch.close_batch(db, identity)
        assert db.scalar(sa.select(sa.func.count()).select_from(RoutineDispatchPermit).where(
            RoutineDispatchPermit.status == "ACTIVE")) == 1


def test_later_explicit_tick_waits_for_same_frozen_schedule(postgres):
    import asyncio
    with postgres[1]() as db:
        rows = graph(db)
        for row in rows[:5]:
            pub = db.get(d.PinPublication, row["publication_id"])
            pub.scheduled_for = datetime.now(timezone.utc) + timedelta(hours=1)
            permit = db.get(RoutineDispatchPermit, row["permit_id"])
            permit.scheduled_for_snapshot = pub.scheduled_for
            permit.request_fingerprint = request_fingerprint_for(pub)
            row["request_fingerprint"] = permit.request_fingerprint
        db.commit()
        identity = batch.create_batch(db, settings=settings())
        db.execute(schema.batches.update().where(schema.batches.c.id == identity).values(state="PREPARING"))
        db.execute(schema.entries.insert(), [dict(batch_id=identity, **r) for r in rows[:5]])
        db.commit()
        batch.seal_batch(db, identity, settings=settings())
        for _ in range(2):
            result = asyncio.run(batch.run_bounded_batch_once(db, identity, settings=settings()))
            assert result["scanned"] == 0
            assert result["batch_state"] == "RUNNING"
            current = snapshot(db, identity)
            assert current["attempts_reserved"] == 0
            assert current["owner"] is None
            assert not current["admission_closed"]
        assert len(batch._rows(db, identity)) == 5


def test_migration_downgrade_reupgrade_and_evidence_refusal(postgres, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from app.core.config import get_settings
    engine, sessions = postgres
    monkeypatch.setattr(get_settings(), "database_url", str(engine.url))
    config = Config("alembic.ini")
    command.downgrade(config, "0033")
    command.upgrade(config, "0034")
    with engine.connect() as connection:
        verify_frozen_schema_at_head(connection, revision="0034")
    with sessions() as db:
        batch.create_batch(db, settings=settings())
    with pytest.raises(RuntimeError, match="evidence"):
        command.downgrade(config, "0033")

def _closed_preparation_settings():
    return settings(
        publishing_enabled=False,
        buffer_publishing_enabled=False,
        routine_pinterest_worker_enabled=False,
        routine_buffer_dispatch_enabled=False,
        routine_scheduled_live_admission_enabled=False,
        routine_pinterest_dry_run=True,
    )


def _preflight_receipt(db, rows, monkeypatch):
    from app.services import routine_bounded_preparation as preparation
    plan = db.get(d.PinterestPortfolioPlan, "plan")
    plan.plan_fingerprint = "a" * 64
    for index, row in enumerate(rows):
        item = db.get(d.PinterestPortfolioPlanItem, row["item_id"])
        item.publication_id = None
        item.status = "PLANNED"
        item.selection_metadata = {"candidate_fingerprint": f"{100 + index:064x}"}
    control = db.get(RoutinePublishingControl, "default")
    control.state = "PAUSED"
    db.commit()

    route = {
        "status": "ROUTE_EXISTING",
        "selected_board_id": rows[0]["board_id"],
        "selected_external_board_id": rows[0]["external_board_id"],
    }
    monkeypatch.setattr(preparation, "board_strategy", lambda *a, **k: route)
    monkeypatch.setattr(
        preparation,
        "execution_readiness",
        lambda *a, **k: {"ready": True, "blockers": []},
    )
    now = batch._now(db)
    candidates = [
        preparation._candidate_identity(
            db.get(d.PinterestPortfolioPlanItem, row["item_id"]), route,
        )
        for row in rows[:5]
    ]
    receipt = {
        "contract": preparation.PREFLIGHT_CONTRACT,
        "database_revision": "0034",
        "month_start": now.date().replace(day=1).isoformat(),
        "current_date": now.date().isoformat(),
        "plan_id": plan.id,
        "plan_fingerprint": plan.plan_fingerprint,
        "candidates": candidates,
    }
    receipt["preflight_fingerprint"] = preparation._digest(receipt)
    return receipt, route


def test_preflight_bound_preparation_rejects_coherent_wrong_receipt_before_batch_write(
    postgres, monkeypatch,
):
    from app.services import bounded_pilot_preparation_operator as operator
    from app.services import routine_bounded_preparation as preparation

    with postgres[1]() as db:
        rows = graph(db)
        receipt, route = _preflight_receipt(db, rows, monkeypatch)
        monkeypatch.setattr(operator, "board_strategy", lambda *a, **k: route)
        monkeypatch.setattr(
            preparation,
            "execute_autonomous_item",
            lambda *a, **k: pytest.fail("execution must not start for receipt drift"),
        )

        altered = {**receipt, "candidates": [dict(row) for row in receipt["candidates"]]}
        altered["candidates"][0]["product_id"] = "product-tampered"
        raw = {
            key: altered["candidates"][0][key]
            for key in altered["candidates"][0]
            if key != "candidate_identity_fingerprint"
        }
        altered["candidates"][0]["candidate_identity_fingerprint"] = preparation._digest(raw)
        payload = {key: altered[key] for key in (
            "contract", "database_revision", "month_start", "current_date",
            "plan_id", "plan_fingerprint", "candidates",
        )}
        altered["preflight_fingerprint"] = preparation._digest(payload)

        with pytest.raises(
            operator.BoundedPreparationOperatorError,
            match="BOUNDED_BATCH_PREFLIGHT_RECEIPT_MISMATCH",
        ):
            operator.prepare_certified_batch(
                db,
                settings=_closed_preparation_settings(),
                actor="operator",
                receipt=altered,
            )

        assert db.scalar(sa.select(sa.func.count()).select_from(schema.batches)) == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(schema.entries)) == 0


def test_preflight_bound_preparation_creates_one_ready_batch_and_is_idempotent(
    postgres, monkeypatch,
):
    from types import SimpleNamespace
    from app.services import bounded_pilot_preparation_operator as operator
    from app.services import routine_bounded_preparation as preparation

    with postgres[1]() as db:
        rows = graph(db)
        receipt, route = _preflight_receipt(db, rows, monkeypatch)
        monkeypatch.setattr(operator, "board_strategy", lambda *a, **k: route)
        selected = []

        def execute(db, item_id, **kwargs):
            selected.append(item_id)
            row = next(r for r in rows if r["item_id"] == item_id)
            item = db.get(d.PinterestPortfolioPlanItem, item_id)
            item.publication_id = row["publication_id"]
            item.status = "SCHEDULED"
            db.commit()
            return SimpleNamespace(
                status="SUCCEEDED",
                stage="PERMITTED",
                publication_id=row["publication_id"],
                routine_permit_id=row["permit_id"],
            )

        monkeypatch.setattr(preparation, "execute_autonomous_item", execute)
        safe = _closed_preparation_settings()
        first = operator.prepare_certified_batch(
            db, settings=safe, actor="operator", receipt=receipt,
        )
        assert first["success"] is True
        assert first["status"] == "READY"
        assert first["candidate_count"] == 5
        assert first["idempotent"] is False
        assert first["publishing_admission"] == "NOT_GRANTED"
        assert first["provider_calls"] == first["buffer_calls"] == 0
        assert first["pinterest_calls"] == first["oauth_calls"] == first["ai_calls"] == 0
        assert selected == [row["item_id"] for row in rows[:5]]
        assert db.scalar(sa.select(sa.func.count()).select_from(schema.batches)) == 1
        assert db.scalar(sa.select(sa.func.count()).select_from(schema.entries)) == 5
        current = snapshot(db, first["batch_id"])
        assert current["state"] == "READY"
        assert current["attempts_reserved"] == 0
        assert current["admission_closed"] is False

        second = operator.prepare_certified_batch(
            db, settings=safe, actor="operator", receipt=receipt,
        )
        assert second["batch_id"] == first["batch_id"]
        assert second["batch_manifest_sha256"] == first["batch_manifest_sha256"]
        assert second["entries"] == first["entries"]
        assert second["idempotent"] is True
        assert selected == [row["item_id"] for row in rows[:5]]
        assert db.scalar(sa.select(sa.func.count()).select_from(schema.batches)) == 1
        assert db.scalar(sa.select(sa.func.count()).select_from(schema.entries)) == 5


def _ready_certification_fixture(db):
    from app.state_transfer import ready_bounded_batch_certification as certification

    rows = graph(db)
    plan = db.get(d.PinterestPortfolioPlan, "plan")
    plan.plan_fingerprint = "a" * 64
    now = datetime.now(timezone.utc)
    for index, row in enumerate(rows[:5]):
        pub = db.get(d.PinPublication, row["publication_id"])
        pub.scheduled_for = now + timedelta(hours=2 + index)
        pub.attempt_started_at = None
        pub.pinterest_pin_id = None
        pub.published_at = None

        permit = db.get(RoutineDispatchPermit, row["permit_id"])
        permit.scheduled_for_snapshot = pub.scheduled_for
        permit.request_fingerprint = request_fingerprint_for(pub)
        permit.status = "ACTIVE"
        permit.consumed_at = None
        permit.revoked_at = None
        permit.revoked_by = None
        permit.revoke_reason = None

        item = db.get(d.PinterestPortfolioPlanItem, row["item_id"])
        item.status = "SCHEDULED"
        item.publication_id = pub.id

        creative = db.get(d.PinCreative, pub.creative_id)
        creative.render_status = "RENDERED"
        creative.sha256 = f"{500 + index:064x}"
        creative.size_bytes = 12345 + index
        creative.rendered_url = f"/api/pins/creatives/{creative.id}/image"

        row["request_fingerprint"] = permit.request_fingerprint

    db.commit()
    identity = batch.create_batch(db, settings=settings())
    db.execute(schema.batches.update().where(
        schema.batches.c.id == identity
    ).values(state="PREPARING"))
    db.execute(schema.entries.insert(), [
        dict(batch_id=identity, **row) for row in rows[:5]
    ])
    db.commit()
    batch.seal_batch(db, identity, settings=settings())

    control = db.get(RoutinePublishingControl, "default")
    control.state = "PAUSED"
    db.commit()
    return certification, identity, rows


def test_ready_batch_readonly_certification_accepts_exact_unattempted_manifest(postgres):
    engine, sessions = postgres
    with sessions() as db:
        certification, identity, rows = _ready_certification_fixture(db)

    report = {"database_transactions": 0}
    dossier = certification.database_snapshot(engine, report)

    assert report["database_transactions"] == 1
    assert dossier["contract"] == "FIVE_PIN_READY_BATCH_CERTIFICATION_V1"
    assert dossier["database_revision"] == "0034"
    assert dossier["routine_state"] == "PAUSED"
    assert dossier["batch_id"] == identity
    assert dossier["batch_state"] == "READY"
    assert dossier["target_count"] == 5
    assert dossier["attempts_reserved"] == 0
    assert dossier["admission_closed"] is False
    assert dossier["candidate_count"] == 5
    assert len(dossier["entries"]) == 5
    assert [entry["slot"] for entry in dossier["entries"]] == list(range(5))
    assert [entry["publication_id"] for entry in dossier["entries"]] == [
        row["publication_id"] for row in rows[:5]
    ]
    assert [entry["permit_id"] for entry in dossier["entries"]] == [
        row["permit_id"] for row in rows[:5]
    ]
    assert len(dossier["ready_batch_fingerprint"]) == 64

    with sessions() as db:
        current = snapshot(db, identity)
        assert current["state"] == "READY"
        assert current["attempts_reserved"] == 0
        assert current["admission_closed"] is False


def test_ready_batch_readonly_certification_refuses_permit_drift(postgres):
    engine, sessions = postgres
    with sessions() as db:
        certification, identity, rows = _ready_certification_fixture(db)
        permit = db.get(RoutineDispatchPermit, rows[0]["permit_id"])
        permit.status = "REVOKED"
        permit.revoked_at = datetime.now(timezone.utc)
        permit.revoked_by = "test"
        permit.revoke_reason = "test-drift"
        db.commit()

    with pytest.raises(Exception):
        certification.database_snapshot(engine, {"database_transactions": 0})

    with sessions() as db:
        current = snapshot(db, identity)
        assert current["state"] == "READY"
        assert current["attempts_reserved"] == 0

"""Task 61.49: PostgreSQL read-only five-Pin bounded preflight regressions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
import json

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.db import bounded_batch_schema_0034 as bounded_schema
from app.models import domain as d
from app.models.routine_publishing import RoutineDispatchPermit, RoutinePublishingControl
from app.state_transfer import bounded_pilot_preflight as cert
from app.services import routine_bounded_preparation as preparation
from app.services.pinterest_optimizer_apply import OPTIMIZER_METADATA_KEY
from app.state_transfer import certify_bounded_pilot_preflight as cli
from app.state_transfer.production_media_certification import CLOSED_FALSE_GATES, SAFE_SCALARS
from tests.test_readiness_execution_admission_0032 import _isolated_database, pytestmark


PRIVATE_TITLE = "PRIVATE_PRODUCT_TITLE_SENTINEL"
PRIVATE_ACCESS = "PRIVATE_ACCESS_CIPHERTEXT_SENTINEL"
PRIVATE_REFRESH = "PRIVATE_REFRESH_CIPHERTEXT_SENTINEL"


def _add(db, model, **values):
    for column in model.__table__.columns:
        if (column.name not in values and not column.nullable
                and column.default is None and column.server_default is None):
            if column.foreign_keys:
                raise AssertionError(f"seed must bind {model.__name__}.{column.name}")
            if isinstance(column.type, sa.String):
                values[column.name] = "x" * (64 if column.type.length == 64 else 1)
            elif isinstance(column.type, (sa.Integer, sa.Numeric)):
                values[column.name] = 1
            elif isinstance(column.type, sa.Boolean):
                values[column.name] = False
            elif isinstance(column.type, sa.JSON):
                values[column.name] = {}
    obj = model(**values)
    db.add(obj)
    db.flush()
    return obj


def _seed(engine, *, item_count=6):
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    today = now.date()
    month_start = today.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)

    with sessions() as db:
        control = db.get(RoutinePublishingControl, "default")
        assert control is not None
        control.state = "PAUSED"

        store = _add(
            db, d.Store, id="store", name="Store", shop_domain="store.invalid",
        )
        connection = _add(
            db,
            d.PinterestConnection,
            id="connection",
            external_user_id="external-user",
            access_token_ciphertext=PRIVATE_ACCESS,
            refresh_token_ciphertext=PRIVATE_REFRESH,
            status="CONNECTED",
            boards_last_synced_at=now,
        )
        provider_board = _add(
            db,
            d.PinterestBoard,
            id="provider-board",
            connection_id=connection.id,
            external_board_id="external-board",
            name="Existing Board",
            routing_label="existing",
            is_active=True,
            is_eligible=True,
            is_ads_only=False,
            last_synced_at=now,
        )
        local_board = _add(
            db,
            d.Board,
            id="local-board",
            store_id=store.id,
            name="Existing Board",
            slug="existing",
            rules={},
            active=True,
        )
        angle = _add(
            db, d.ContentAngle, id="angle", key="angle", name="Angle", active=True,
        )
        plan = _add(
            db,
            d.PinterestPortfolioPlan,
            id="plan",
            store_id=store.id,
            month_start=month_start,
            month_end=month_end,
            target_pins=150,
            existing_commitments=0,
            planned_active_slots=max(item_count, 5),
            reserve_slots=0,
            policy_version="PINTEREST_PORTFOLIO_V2",
            input_fingerprint="1" * 64,
            plan_fingerprint="2" * 64,
            status="ACTIVE",
            metadata_json={},
        )
        optimizer = _add(
            db,
            d.PinterestOptimizerApplication,
            id="optimizer",
            plan_id=plan.id,
            plan_fingerprint_snapshot=plan.plan_fingerprint,
            optimizer_policy_version="TEST_OPTIMIZER_V1",
            optimizer_fingerprint="3" * 64,
            learning_fingerprint=None,
            input_state_fingerprint="4" * 64,
            frozen_item_count=0,
            optimizable_item_count=item_count,
            exploit_count=item_count,
            explore_count=0,
            recommendation_snapshot={},
            status="APPLIED",
            applied_by="test",
            applied_at=now,
        )
        products = []
        for index in range(item_count):
            product = _add(
                db,
                d.Product,
                id=f"product-{index}",
                store_id=store.id,
                shopify_product_id=str(index),
                handle=f"product-{index}",
                title=PRIVATE_TITLE if index == 0 else f"Product {index}",
                product_url=f"https://catalog.invalid/product-{index}",
                inventory_total=1,
                status="ACTIVE",
            )
            products.append(product)
            _add(
                db,
                d.ProductIntelligence,
                id=f"intel-{index}",
                product_id=product.id,
                brand=f"Brand {index}",
                image_available=True,
                inventory_eligible=True,
                eligibility_score=100,
                eligibility_status="ELIGIBLE",
                eligibility_reasons=[],
                normalization_status="NORMALIZED",
                normalized_data={},
            )
            _add(
                db,
                d.ProductImage,
                id=f"image-{index}",
                product_id=product.id,
                shopify_media_id=f"media-{index}",
                source_url=f"https://cdn.shopify.com/s/files/test/product-{index}.jpg",
                source_sha256=f"{900 + index:064x}",
                is_primary=True,
                editorial_eligible=True,
            )
            _add(
                db,
                d.PinterestPortfolioPlanItem,
                id=f"item-{index}",
                plan_id=plan.id,
                slot_index=index,
                is_reserve=False,
                planned_date=today + timedelta(days=index + 1),
                product_id=product.id,
                local_board_id=local_board.id,
                board_key_snapshot=local_board.slug,
                content_angle_id=angle.id,
                angle_key_snapshot=angle.key,
                seed_keywords=[f"{product.title} perfume"],
                selection_score=1,
                selection_metadata={
                    "candidate_fingerprint": f"{100 + index:064x}",
                    OPTIMIZER_METADATA_KEY: {
                        "optimizer_policy_version": optimizer.optimizer_policy_version,
                        "optimizer_fingerprint": optimizer.optimizer_fingerprint,
                        "input_state_fingerprint": optimizer.input_state_fingerprint,
                        "recommended_position": index,
                        "target_slot_index": index,
                        "target_planned_date": (today + timedelta(days=index + 1)).isoformat(),
                        "selection_reason": "test",
                    },
                },
                item_fingerprint=f"{200 + index:064x}",
                status="PLANNED",
                publication_id=None,
            )
        db.commit()

    return {
        "sessions": sessions,
        "now": now,
        "today": today,
        "month_start": month_start,
        "month_end": month_end,
        "store_id": store.id,
        "plan_id": plan.id,
        "provider_board_id": provider_board.id,
        "local_board_id": local_board.id,
        "angle_id": angle.id,
        "product_ids": [p.id for p in products],
    }


@pytest.fixture
def production_preflight(monkeypatch):
    with _isolated_database("0034") as (engine, database_url):
        seeded = _seed(engine)
        monkeypatch.setenv(cert.DATABASE_ENV, database_url)
        for name in CLOSED_FALSE_GATES:
            monkeypatch.setenv(name, "false")
        for name, value in SAFE_SCALARS.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        try:
            yield engine, seeded
        finally:
            get_settings.cache_clear()


def _invoke(engine, monkeypatch):
    monkeypatch.setattr(cert.sa, "create_engine", lambda *args, **kwargs: engine)
    return cert.run()


def _mutation_counts(engine):
    names = (
        "pin_publications",
        "pin_creatives",
        "pin_approvals",
        "routine_dispatch_permits",
        "routine_autonomous_batches",
        "routine_autonomous_batch_entries",
    )
    with engine.connect() as connection:
        return {
            name: connection.exec_driver_sql(
                f'SELECT count(*) FROM public."{name}"'
            ).scalar_one()
            for name in names
        }


def test_success_is_exactly_one_readonly_transaction_and_first_five_only(
    production_preflight, monkeypatch
):
    engine, _ = production_preflight
    before = _mutation_counts(engine)
    statements = []
    begins = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    def begin(connection):
        begins.append(1)

    sa.event.listen(engine, "before_cursor_execute", observe)
    sa.event.listen(engine, "begin", begin)
    try:
        result = _invoke(engine, monkeypatch)
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe)
        sa.event.remove(engine, "begin", begin)

    after = _mutation_counts(engine)
    assert result["success"] is True
    assert result["terminal_stage"] == "COMPLETE"
    assert result["bounded_preflight_certification"] == "PASS"
    assert result["database_revision"] == "0034"
    assert result["schema_canonicality"] == "PASS"
    assert result["routine_state"] == "PAUSED"
    assert result["publish_unknown_count"] == 0
    assert result["conflicting_nonterminal_batch_count"] == 0
    assert result["plan_id"] == "plan"
    assert result["plan_fingerprint"] == "2" * 64
    assert result["candidate_count"] == 5
    assert result["candidate_pool_has_more"] is True
    assert [row["item_id"] for row in result["candidates"]] == [
        f"item-{index}" for index in range(5)
    ]
    assert [row["product_id"] for row in result["candidates"]] == [
        f"product-{index}" for index in range(5)
    ]
    assert all(row["local_board_id"] == "local-board" for row in result["candidates"])
    assert all(
        row["pinterest_board_record_id"] == "provider-board"
        and row["external_board_id"] == "external-board"
        for row in result["candidates"]
    )
    assert len({row["candidate_identity_fingerprint"] for row in result["candidates"]}) == 5
    assert result["publishing_admission"] == "NOT_GRANTED"
    assert result["database_transactions"] == 1
    assert begins == [1]
    assert before == after
    assert all(
        statement.lstrip().split(None, 1)[0].upper() in {"SELECT", "SET", "SHOW"}
        for statement in statements
    )
    assert any("SET TRANSACTION READ ONLY" in statement for statement in statements)
    for key in (
        "database_writes", "object_storage_reads", "object_storage_writes",
        "provider_calls", "provider_reads", "provider_writes", "buffer_calls",
        "pinterest_calls", "oauth_calls", "ai_calls", "automatic_retries",
        "publication_creations", "creative_creations", "approval_creations",
        "permit_creations", "batch_creations", "scheduler_activations",
        "worker_activations", "autonomy_activations",
    ):
        assert result[key] == 0


def test_execution_ready_selector_skips_elapsed_schedule_slots_before_freeze(
    production_preflight, monkeypatch,
):
    engine, seeded = production_preflight
    schedule_day = seeded["today"]
    with seeded["sessions"]() as db:
        for index in range(6):
            item = db.get(d.PinterestPortfolioPlanItem, f"item-{index}")
            item.planned_date = schedule_day
            metadata = dict(item.selection_metadata)
            optimizer = dict(metadata[OPTIMIZER_METADATA_KEY])
            optimizer["recommended_position"] = index
            optimizer["target_planned_date"] = schedule_day.isoformat()
            metadata[OPTIMIZER_METADATA_KEY] = optimizer
            item.selection_metadata = metadata
        for index in range(6, 8):
            product = _add(
                db, d.Product, id=f"product-{index}", store_id=seeded["store_id"],
                shopify_product_id=str(index), handle=f"product-{index}", title=f"Product {index}",
                product_url=f"https://catalog.invalid/product-{index}", inventory_total=1, status="ACTIVE",
            )
            _add(
                db,
                d.ProductIntelligence,
                id=f"intel-{index}",
                product_id=product.id,
                brand=f"Brand {index}",
                image_available=True,
                inventory_eligible=True,
                eligibility_score=100,
                eligibility_status="ELIGIBLE",
                eligibility_reasons=[],
                normalization_status="NORMALIZED",
                normalized_data={},
            )
            _add(
                db,
                d.ProductImage,
                id=f"image-{index}",
                product_id=product.id,
                shopify_media_id=f"media-{index}",
                source_url=f"https://cdn.shopify.com/s/files/test/product-{index}.jpg",
                source_sha256=f"{900 + index:064x}",
                is_primary=True,
                editorial_eligible=True,
            )
            _add(
                db, d.PinterestPortfolioPlanItem, id=f"item-{index}", plan_id=seeded["plan_id"],
                slot_index=index, is_reserve=False, planned_date=schedule_day,
                product_id=product.id, local_board_id=seeded["local_board_id"],
                board_key_snapshot="existing", content_angle_id=seeded["angle_id"],
                angle_key_snapshot="angle", seed_keywords=[f"{product.title} perfume"], selection_score=1,
                selection_metadata={
                    "candidate_fingerprint": f"{100 + index:064x}",
                    OPTIMIZER_METADATA_KEY: {
                        "optimizer_policy_version": "TEST_OPTIMIZER_V1",
                        "optimizer_fingerprint": "3" * 64,
                        "input_state_fingerprint": "4" * 64,
                        "recommended_position": index,
                        "target_slot_index": index,
                        "target_planned_date": schedule_day.isoformat(),
                        "selection_reason": "test",
                    },
                },
                item_fingerprint=f"{200 + index:064x}", status="PLANNED", publication_id=None,
            )
        optimizer_app = db.get(d.PinterestOptimizerApplication, "optimizer")
        optimizer_app.optimizable_item_count = 8
        optimizer_app.exploit_count = 8
        db.commit()

    settings = Settings(
        _env_file=None,
        PINTEREST_AUTONOMOUS_SCHEDULE_START_MINUTE_UTC=840,
        PINTEREST_AUTONOMOUS_SCHEDULE_END_MINUTE_UTC=1320,
    )
    now = datetime(
        schedule_day.year, schedule_day.month, schedule_day.day, 17, 0,
        tzinfo=timezone.utc,
    )
    with seeded["sessions"]() as db:
        ready = preparation._execution_ready_items(
            db,
            seeded["plan_id"],
            settings=settings,
            now=now,
            limit=6,
            lock=False,
        )
        assert [item.id for item in ready] == [
            "item-3", "item-4", "item-5", "item-6", "item-7",
        ]

    real_selector = preparation._execution_ready_items

    def fixed_time_selector(db, plan_id, *, settings, now, limit, lock=False):
        return real_selector(
            db,
            plan_id,
            settings=settings,
            now=now.replace(
                year=schedule_day.year,
                month=schedule_day.month,
                day=schedule_day.day,
                hour=17,
                minute=0,
                second=0,
                microsecond=0,
            ),
            limit=limit,
            lock=lock,
        )

    monkeypatch.setattr(cert, "_execution_ready_items", fixed_time_selector)
    monkeypatch.setattr(
        cert,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            PINTEREST_AUTONOMOUS_SCHEDULE_START_MINUTE_UTC=840,
            PINTEREST_AUTONOMOUS_SCHEDULE_END_MINUTE_UTC=1320,
        ),
    )
    certified = _invoke(engine, monkeypatch)
    assert certified["success"] is True
    assert [row["item_id"] for row in certified["candidates"]] == [
        "item-3", "item-4", "item-5", "item-6", "item-7",
    ]
    assert all(
        row["item_id"] not in {"item-0", "item-1", "item-2"}
        for row in certified["candidates"]
    )
    payload = {
        "contract": "FIVE_PIN_BOUNDED_PREFLIGHT_V1",
        "database_revision": "0034",
        "month_start": certified["month_start"],
        "current_date": certified["current_date"],
        "plan_id": certified["plan_id"],
        "plan_fingerprint": certified["plan_fingerprint"],
        "candidates": certified["candidates"],
    }
    assert certified["preflight_fingerprint"] == cert.digest(payload)

    receipt = {
        **payload,
        "preflight_fingerprint": certified["preflight_fingerprint"],
    }
    monkeypatch.setattr(preparation.batch, "_now", lambda db: now)
    with seeded["sessions"]() as db:
        _, validated_now, validated_items, _ = preparation.validate_preflight_receipt(
            db,
            seeded["plan_id"],
            settings=settings,
            expected_preflight=receipt,
        )
        assert validated_now == now
        assert [item.id for item in validated_items] == [
            "item-3", "item-4", "item-5", "item-6", "item-7",
        ]


def test_terminal_failed_batch_frozen_item_is_excluded_from_new_preflight(
    production_preflight, monkeypatch,
):
    engine, seeded = production_preflight
    with seeded["sessions"]() as db:
        first = db.get(d.PinterestPortfolioPlanItem, "item-0")
        db.execute(bounded_schema.batches.insert().values(
            id="failed-preparation-batch",
            state="PREPARING",
            admission_closed=True,
        ))
        db.execute(bounded_schema.entries.insert().values(
            batch_id="failed-preparation-batch",
            slot=0,
            item_id=first.id,
            product_id=first.product_id,
            board_id=seeded["provider_board_id"],
            external_board_id="external-board",
            item_fingerprint=first.item_fingerprint,
        ))
        db.execute(
            bounded_schema.batches.update()
            .where(bounded_schema.batches.c.id == "failed-preparation-batch")
            .values(state="FAILED", reason="BOUNDED_BATCH_PREPARATION_FAILED")
        )
        db.commit()

    result = _invoke(engine, monkeypatch)
    assert result["success"] is True
    assert result["conflicting_nonterminal_batch_count"] == 0
    assert [row["item_id"] for row in result["candidates"]] == [
        "item-1", "item-2", "item-3", "item-4", "item-5",
    ]
    assert "item-0" not in {row["item_id"] for row in result["candidates"]}
    with seeded["sessions"]() as db:
        evidence = db.execute(
            sa.select(bounded_schema.entries).where(
                bounded_schema.entries.c.batch_id == "failed-preparation-batch"
            )
        ).mappings().one()
        assert evidence["item_id"] == "item-0"
        failed = db.execute(
            sa.select(bounded_schema.batches).where(
                bounded_schema.batches.c.id == "failed-preparation-batch"
            )
        ).mappings().one()
        assert failed["state"] == "FAILED"


def test_more_candidates_never_expand_or_change_first_five(production_preflight, monkeypatch):
    engine, seeded = production_preflight
    first = _invoke(engine, monkeypatch)
    assert first["success"]

    sessions = seeded["sessions"]
    with sessions() as db:
        product = _add(
            db, d.Product, id="product-6", store_id=seeded["store_id"],
            shopify_product_id="6", handle="product-6", title="Product 6",
            product_url="https://catalog.invalid/product-6", inventory_total=1, status="ACTIVE",
        )
        _add(
            db, d.PinterestPortfolioPlanItem, id="item-6", plan_id=seeded["plan_id"],
            slot_index=6, is_reserve=False, planned_date=seeded["today"] + timedelta(days=6),
            product_id=product.id, local_board_id=seeded["local_board_id"],
            board_key_snapshot="existing", content_angle_id=seeded["angle_id"],
            angle_key_snapshot="angle", seed_keywords=[], selection_score=1,
            selection_metadata={"candidate_fingerprint": f"{106:064x}"},
            item_fingerprint=f"{206:064x}", status="PLANNED", publication_id=None,
        )
        db.commit()

    second = _invoke(engine, monkeypatch)
    assert second["success"]
    assert second["candidate_count"] == 5
    assert [r["item_id"] for r in second["candidates"]] == [
        f"item-{index}" for index in range(5)
    ]
    assert second["preflight_fingerprint"] == first["preflight_fingerprint"]


def test_fewer_than_five_candidates_fails_closed(production_preflight, monkeypatch):
    engine, seeded = production_preflight
    with seeded["sessions"]() as db:
        db.query(d.PinterestPortfolioPlanItem).filter(
            d.PinterestPortfolioPlanItem.id.in_(["item-4", "item-5"])
        ).delete(synchronize_session=False)
        db.commit()
    result = _invoke(engine, monkeypatch)
    assert result["success"] is False
    assert result["terminal_stage"] == "READ_ONLY_DATABASE"
    assert result["candidate_count"] == 0
    assert result["bounded_preflight_certification"] == "NOT_GRANTED"


def test_plan_absence_and_multiple_active_plan_conflict_fail_closed(
    production_preflight, monkeypatch
):
    engine, seeded = production_preflight
    with seeded["sessions"]() as db:
        plan = db.get(d.PinterestPortfolioPlan, seeded["plan_id"])
        plan.status = "COMPLETED"
        db.commit()
    assert _invoke(engine, monkeypatch)["success"] is False

    with seeded["sessions"]() as db:
        plan = db.get(d.PinterestPortfolioPlan, seeded["plan_id"])
        plan.status = "ACTIVE"
        other = _add(db, d.Store, id="other-store", name="Other", shop_domain="other.invalid")
        _add(
            db, d.PinterestPortfolioPlan, id="other-plan", store_id=other.id,
            month_start=seeded["month_start"], month_end=seeded["month_end"],
            target_pins=5, existing_commitments=0, planned_active_slots=5, reserve_slots=0,
            policy_version="PINTEREST_PORTFOLIO_V2", input_fingerprint="3" * 64,
            plan_fingerprint="4" * 64, status="ACTIVE", metadata_json={},
        )
        db.commit()
    assert _invoke(engine, monkeypatch)["success"] is False


def test_board_route_drift_fails_closed(production_preflight, monkeypatch):
    engine, seeded = production_preflight
    with seeded["sessions"]() as db:
        db.get(d.PinterestBoard, seeded["provider_board_id"]).is_eligible = False
        db.commit()
    result = _invoke(engine, monkeypatch)
    assert result["success"] is False
    assert result["provider_calls"] == 0
    assert result["database_writes"] == 0


def test_duplicate_candidate_identity_fails_closed(production_preflight, monkeypatch):
    engine, seeded = production_preflight
    with seeded["sessions"]() as db:
        first = db.get(d.PinterestPortfolioPlanItem, "item-0")
        second = db.get(d.PinterestPortfolioPlanItem, "item-1")
        second.selection_metadata = dict(first.selection_metadata)
        db.commit()
    result = _invoke(engine, monkeypatch)
    assert result["success"] is False
    assert result["bounded_preflight_certification"] == "NOT_GRANTED"


def test_publish_unknown_fails_closed(production_preflight, monkeypatch):
    engine, seeded = production_preflight
    with seeded["sessions"]() as db:
        product = db.get(d.Product, seeded["product_ids"][0])
        image = _add(db, d.ProductImage, id="unknown-image", product_id=product.id)
        concept = _add(
            db, d.PinConcept, id="unknown-concept", store_id=seeded["store_id"],
            product_id=product.id, content_angle_id=seeded["angle_id"],
            board_id=seeded["local_board_id"], fingerprint="5" * 64,
        )
        draft = _add(db, d.PinDraft, id="unknown-draft", concept_id=concept.id)
        template = _add(db, d.CreativeTemplate, id="unknown-template")
        creative = _add(
            db, d.PinCreative, id="unknown-creative", draft_id=draft.id,
            template_id=template.id, source_image_id=image.id,
            creative_fingerprint="6" * 64,
        )
        _add(
            db, d.PinPublication, id="unknown-publication", draft_id=draft.id,
            creative_id=creative.id, publication_fingerprint="7" * 64,
            status=d.PublicationStatus.PUBLISH_UNKNOWN,
        )
        db.commit()
    result = _invoke(engine, monkeypatch)
    assert result["success"] is False
    assert result["publish_unknown_count"] is None
    assert result["publishing_admission"] == "NOT_GRANTED"


def test_nonterminal_bounded_batch_conflict_fails_closed(production_preflight, monkeypatch):
    engine, seeded = production_preflight
    with seeded["sessions"]() as db:
        db.execute(bounded_schema.batches.insert().values(id="existing-batch"))
        db.commit()
    result = _invoke(engine, monkeypatch)
    assert result["success"] is False
    assert result["batch_creations"] == 0


def test_gate_drift_refuses_before_database(production_preflight, monkeypatch):
    engine, _ = production_preflight
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")

    def forbidden(*args, **kwargs):
        pytest.fail("database opened after gate drift")

    monkeypatch.setattr(cert.sa, "create_engine", forbidden)
    result = cert.run()
    assert result["success"] is False
    assert result["terminal_stage"] == "GATES"
    assert result["database_transactions"] == 0
    assert result["provider_calls"] == result["database_writes"] == 0


def test_determinism_and_sanitization(production_preflight, monkeypatch):
    engine, _ = production_preflight
    first = _invoke(engine, monkeypatch)
    second = _invoke(engine, monkeypatch)
    assert first["success"] and second["success"]
    assert first["preflight_fingerprint"] == second["preflight_fingerprint"]
    assert first["candidates"] == second["candidates"]

    rendered = json.dumps(first, sort_keys=True)
    assert PRIVATE_TITLE not in rendered
    assert PRIVATE_ACCESS not in rendered
    assert PRIVATE_REFRESH not in rendered
    assert "DATABASE_URL" not in rendered
    assert "postgresql" not in rendered
    source = inspect.getsource(cert)
    for forbidden in (
        "BufferGateway", "PinterestClient", "create_pinterest_post",
        "create_batch(", "prepare_batch(", "execute_autonomous_item",
    ):
        assert forbidden not in source


def test_cli_rejects_arguments_without_echoing_private_values(monkeypatch, capsys):
    private = "PRIVATE_ARGUMENT_SENTINEL"
    assert cli.main(["--plan-id", private]) == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert private not in output.out
    result = json.loads(output.out)
    assert result["terminal_stage"] == "ARGUMENTS"
    assert result["database_writes"] == result["provider_calls"] == 0
    assert result["publishing_admission"] == "NOT_GRANTED"


def test_cli_success_is_sanitized(production_preflight, monkeypatch, capsys):
    engine, _ = production_preflight
    monkeypatch.setattr(cert.sa, "create_engine", lambda *args, **kwargs: engine)
    assert cli.main([]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    result = json.loads(output.out)
    assert result["success"] is True
    assert result["bounded_preflight_certification"] == "PASS"
    assert PRIVATE_TITLE not in output.out
    assert PRIVATE_ACCESS not in output.out
    assert PRIVATE_REFRESH not in output.out

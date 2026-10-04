"""Only disposable PostgreSQL; reproduce the ten reviewed catalog differences."""
import pytest
import sqlalchemy as sa

from app.state_transfer import select_bridge as bridge
from app.state_transfer import bridge_catalog_semantics as semantics
from tests.test_migration_closed_state_transfer import source
from tests.test_readiness_execution_admission_0032 import pytestmark


CHECKS = (
    ("publication_dispatch_authorizations", "ck_publication_dispatch_authorization_status"),
    ("publication_reconciliation_events", "ck_publication_reconciliation_action"),
    ("routine_dispatch_permits", "ck_routine_dispatch_permit_status"),
    ("routine_publishing_control", "ck_routine_publishing_control_state"),
    ("routine_publishing_runs", "ck_routine_publishing_run_status"),
)
RENAMES = (
    ("pin_creatives", "uq_pin_creative_fingerprint", "pin_creatives_creative_fingerprint_key"),
    ("pin_publications", "uq_pin_publication_fingerprint", "pin_publications_publication_fingerprint_key"),
)
PINS = bridge.frozen()["catalog"]


def check_change(table, name):
    expected = next(c for c in PINS[table]["catalog"]["constraints"] if c["name"] == name)
    alternate, = semantics.cast_renderings(expected["definition"])
    return [
        f'ALTER TABLE public."{table}" DROP CONSTRAINT "{name}"',
        f'ALTER TABLE public."{table}" ADD CONSTRAINT "{name}" {alternate}',
    ]


def partial_change():
    expected = next(i for i in PINS["catalog_sync_jobs"]["catalog"]["indexes"]
                    if i["name"] == "uq_catalog_sync_active")
    alternate, = semantics.cast_renderings(expected["predicate"])
    return [
        "DROP INDEX public.uq_catalog_sync_active",
        expected["definition"].replace(expected["predicate"], alternate),
    ]


OBSERVED = [
    (f"{table}:unique_{kind}", [f'ALTER TABLE public."{table}" RENAME CONSTRAINT "{old}" TO "{new}"'])
    for table, old, new in RENAMES for kind in ("constraint", "backing_index")
] + [(f"{table}:check_cast", check_change(table, name)) for table, name in CHECKS] + [
    ("catalog_sync_jobs:partial_index_cast", partial_change()),
]


def changed_capture(source, changes, *, refuse=False):
    with source.connect() as connection:
        tx = connection.begin()
        try:
            for statement in changes:
                connection.exec_driver_sql(statement)
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            if refuse:
                with pytest.raises(sa.exc.DBAPIError) as error:
                    connection.exec_driver_sql(bridge.source_sql(),
                                              execution_options={"no_parameters": True})
                assert error.value.orig.sqlstate == "22012"
            else:
                capsule = connection.exec_driver_sql(
                    bridge.source_sql(), execution_options={"no_parameters": True}).scalar_one()
                assert capsule["payload"]["catalog"] == PINS
                bundle = bridge.wrap_source_result(capsule, capsule["capsule_sha256"])
                assert sum(v["source_count"] for v in bundle["manifest"]["tables"].values()) == 12459
                assert sum(v["exported_count"] for v in bundle["manifest"]["tables"].values()) == 12454
                return capsule
        finally:
            tx.rollback()


@pytest.mark.parametrize("name,changes", OBSERVED, ids=[name for name, _ in OBSERVED])
def test_each_observed_production_difference_is_importable(source, name, changes):
    changed_capture(source, changes)


def test_all_ten_observed_differences_together(source):
    changes = [f'ALTER TABLE public."{table}" RENAME CONSTRAINT "{old}" TO "{new}"'
               for table, old, new in RENAMES]
    changes += [statement for table, name in CHECKS for statement in check_change(table, name)]
    changes += partial_change()
    changed_capture(source, changes)


@pytest.mark.parametrize("table,name", CHECKS)
def test_exact_observed_check_rendering_is_canonicalized(source, monkeypatch, table, name):
    # Feed the exact reviewed live text to the SELECT descriptor independently
    # of how this particular PostgreSQL release deparses the equivalent DDL.
    from copy import deepcopy
    from app.state_transfer.catalog import canonical
    live = deepcopy(PINS[table])
    constraint = next(c for c in live["catalog"]["constraints"] if c["name"] == name)
    constraint["definition"], = semantics.cast_renderings(constraint["definition"])
    assert live != PINS[table]
    monkeypatch.setattr(semantics, "descriptor", lambda: "e.live_catalog")
    expression = bridge.qualify_builtins(semantics.semantic_descriptor(PINS))
    with source.connect() as connection, connection.begin():
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        normalized = connection.execute(sa.text(
            "WITH e AS (SELECT CAST(:name AS text) name,CAST(:expected AS jsonb) expected_catalog,"
            "CAST(:live AS jsonb) live_catalog) SELECT " + expression + " FROM e"
        ), {"name": table, "expected": canonical(PINS[table]), "live": canonical(live)}).scalar_one()
    assert normalized == PINS[table]


def test_exact_observed_partial_index_rendering_is_canonicalized(source, monkeypatch):
    from copy import deepcopy
    from app.state_transfer.catalog import canonical
    live = deepcopy(PINS["catalog_sync_jobs"])
    index = next(i for i in live["catalog"]["indexes"] if i["name"] == "uq_catalog_sync_active")
    alternate, = semantics.cast_renderings(index["predicate"])
    index["definition"] = index["definition"].replace(index["predicate"], alternate)
    index["predicate"] = alternate
    monkeypatch.setattr(semantics, "descriptor", lambda: "e.live_catalog")
    expression = bridge.qualify_builtins(semantics.semantic_descriptor(PINS))
    with source.connect() as connection, connection.begin():
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        normalized = connection.execute(sa.text(
            "WITH e AS (SELECT 'catalog_sync_jobs'::text name,CAST(:expected AS jsonb) expected_catalog,"
            "CAST(:live AS jsonb) live_catalog) SELECT " + expression + " FROM e"
        ), {"expected": canonical(PINS["catalog_sync_jobs"]), "live": canonical(live)}).scalar_one()
    assert normalized == PINS["catalog_sync_jobs"]


NEGATIVE = {
    "unique_key_column": [
        "ALTER TABLE public.pin_creatives DROP CONSTRAINT uq_pin_creative_fingerprint",
        "ALTER TABLE public.pin_creatives ADD CONSTRAINT generated_unique UNIQUE (id)",
    ],
    "unique_extra_key": [
        "ALTER TABLE public.pin_creatives DROP CONSTRAINT uq_pin_creative_fingerprint",
        "ALTER TABLE public.pin_creatives ADD CONSTRAINT generated_unique UNIQUE (creative_fingerprint,id)",
    ],
    "unique_key_order": [
        "ALTER TABLE public.pin_creatives DROP CONSTRAINT uq_pin_creative_fingerprint",
        "ALTER TABLE public.pin_creatives ADD CONSTRAINT generated_unique UNIQUE (id,creative_fingerprint)",
    ],
    "unique_deferrable": [
        "ALTER TABLE public.pin_creatives DROP CONSTRAINT uq_pin_creative_fingerprint",
        "ALTER TABLE public.pin_creatives ADD CONSTRAINT generated_unique UNIQUE (creative_fingerprint) DEFERRABLE",
    ],
    "unique_initially_deferred": [
        "ALTER TABLE public.pin_creatives DROP CONSTRAINT uq_pin_creative_fingerprint",
        "ALTER TABLE public.pin_creatives ADD CONSTRAINT generated_unique UNIQUE (creative_fingerprint) DEFERRABLE INITIALLY DEFERRED",
    ],
    "unique_nulls_not_distinct": [
        "ALTER TABLE public.pin_creatives DROP CONSTRAINT uq_pin_creative_fingerprint",
        "ALTER TABLE public.pin_creatives ADD CONSTRAINT generated_unique UNIQUE NULLS NOT DISTINCT (creative_fingerprint)",
    ],
    "unique_multiplicity": [
        "ALTER TABLE public.pin_creatives ADD CONSTRAINT generated_duplicate UNIQUE (creative_fingerprint)",
    ],
    "standalone_unique_not_constraint": [
        "ALTER TABLE public.pin_creatives DROP CONSTRAINT uq_pin_creative_fingerprint",
        "CREATE UNIQUE INDEX uq_pin_creative_fingerprint ON public.pin_creatives (creative_fingerprint)",
    ],
    "nonunique_index": [
        "DROP INDEX public.uq_catalog_sync_active",
        "CREATE INDEX uq_catalog_sync_active ON public.catalog_sync_jobs (store_id) WHERE status::text=ANY(ARRAY['QUEUED','RUNNING'])",
    ],
    "index_key_column": [
        "DROP INDEX public.uq_catalog_sync_active",
        "CREATE UNIQUE INDEX uq_catalog_sync_active ON public.catalog_sync_jobs (id) WHERE status::text=ANY(ARRAY['QUEUED','RUNNING'])",
    ],
    "index_sort_order": [
        "DROP INDEX public.uq_catalog_sync_active",
        "CREATE UNIQUE INDEX uq_catalog_sync_active ON public.catalog_sync_jobs (store_id DESC) WHERE status::text=ANY(ARRAY['QUEUED','RUNNING'])",
    ],
    "index_operator_class": [
        "DROP INDEX public.uq_catalog_sync_active",
        "CREATE UNIQUE INDEX uq_catalog_sync_active ON public.catalog_sync_jobs (store_id varchar_pattern_ops) WHERE status::text=ANY(ARRAY['QUEUED','RUNNING'])",
    ],
    "index_null_semantics": [
        "DROP INDEX public.uq_catalog_sync_active",
        "CREATE UNIQUE INDEX uq_catalog_sync_active ON public.catalog_sync_jobs (store_id) NULLS NOT DISTINCT WHERE status::text=ANY(ARRAY['QUEUED','RUNNING'])",
    ],
    "index_extra_literal": [
        "DROP INDEX public.uq_catalog_sync_active",
        "CREATE UNIQUE INDEX uq_catalog_sync_active ON public.catalog_sync_jobs (store_id) WHERE status::text=ANY(ARRAY['QUEUED','RUNNING','EXTRA'])",
    ],
    "index_missing_literal": [
        "DROP INDEX public.uq_catalog_sync_active",
        "CREATE UNIQUE INDEX uq_catalog_sync_active ON public.catalog_sync_jobs (store_id) WHERE status::text=ANY(ARRAY['QUEUED'])",
    ],
    "index_changed_operator": [
        "DROP INDEX public.uq_catalog_sync_active",
        "CREATE UNIQUE INDEX uq_catalog_sync_active ON public.catalog_sync_jobs (store_id) WHERE status::text<>ALL(ARRAY['QUEUED','RUNNING'])",
    ],
    "index_absent_predicate": [
        "DROP INDEX public.uq_catalog_sync_active",
        "CREATE UNIQUE INDEX uq_catalog_sync_active ON public.catalog_sync_jobs (store_id)",
    ],
    "index_invalid": ["UPDATE pg_catalog.pg_index SET indisvalid=false WHERE indexrelid='public.uq_catalog_sync_active'::regclass"],
    "index_not_ready": ["UPDATE pg_catalog.pg_index SET indisready=false WHERE indexrelid='public.uq_catalog_sync_active'::regclass"],
    "standalone_index_name": ["ALTER INDEX public.uq_catalog_sync_active RENAME TO generated_index"],
    "check_extra_literal": [
        "ALTER TABLE public.routine_publishing_control DROP CONSTRAINT ck_routine_publishing_control_state",
        "ALTER TABLE public.routine_publishing_control ADD CONSTRAINT ck_routine_publishing_control_state CHECK (state::text=ANY(ARRAY['PAUSED','DRY_RUN','LIVE','EXTRA']))",
    ],
    "check_missing_literal": [
        "ALTER TABLE public.routine_publishing_control DROP CONSTRAINT ck_routine_publishing_control_state",
        "ALTER TABLE public.routine_publishing_control ADD CONSTRAINT ck_routine_publishing_control_state CHECK (state::text=ANY(ARRAY['PAUSED','LIVE']))",
    ],
    "check_changed_operator": [
        "ALTER TABLE public.routine_publishing_control DROP CONSTRAINT ck_routine_publishing_control_state",
        "ALTER TABLE public.routine_publishing_control ADD CONSTRAINT ck_routine_publishing_control_state CHECK (state::text<>ALL(ARRAY['EXTRA']))",
    ],
    "check_unvalidated": [
        "ALTER TABLE public.routine_publishing_control DROP CONSTRAINT ck_routine_publishing_control_state",
        "ALTER TABLE public.routine_publishing_control ADD CONSTRAINT ck_routine_publishing_control_state CHECK (state::text=ANY(ARRAY['PAUSED','DRY_RUN','LIVE'])) NOT VALID",
    ],
    "check_name": [
        "ALTER TABLE public.routine_publishing_control RENAME CONSTRAINT ck_routine_publishing_control_state TO generated_check",
    ],
    "fk_delete_action": [
        "ALTER TABLE public.products DROP CONSTRAINT products_store_id_fkey",
        "ALTER TABLE public.products ADD CONSTRAINT products_store_id_fkey FOREIGN KEY (store_id) REFERENCES public.stores(id) ON DELETE RESTRICT",
    ],
    "fk_update_action": [
        "ALTER TABLE public.products DROP CONSTRAINT products_store_id_fkey",
        "ALTER TABLE public.products ADD CONSTRAINT products_store_id_fkey FOREIGN KEY (store_id) REFERENCES public.stores(id) ON DELETE CASCADE ON UPDATE CASCADE",
    ],
    "fk_name": ["ALTER TABLE public.products RENAME CONSTRAINT products_store_id_fkey TO generated_fk"],
}


@pytest.mark.parametrize("changes", NEGATIVE.values(), ids=NEGATIVE)
def test_material_or_unreviewed_catalog_change_refuses(source, changes):
    changed_capture(source, changes, refuse=True)


def test_constraint_enforcement_metadata_is_fail_closed_across_versions(source):
    with source.connect() as connection:
        version = int(connection.exec_driver_sql("SHOW server_version_num").scalar_one())
        if version < 180000:
            assert connection.exec_driver_sql(
                "SELECT bool_and(to_jsonb(c)->>'conenforced' IS NULL) "
                "FROM pg_catalog.pg_constraint c "
                "WHERE c.conrelid='public.routine_publishing_control'::regclass"
            ).scalar_one() is True
            return
    changed_capture(source, [
        "UPDATE pg_catalog.pg_constraint SET conenforced=false WHERE conname='ck_routine_publishing_control_state' AND conrelid='public.routine_publishing_control'::regclass",
    ], refuse=True)


@pytest.mark.parametrize("replacement", [
    "CHECK (state::text <> ANY (ARRAY['PAUSED'::character varying]::text[]))",
    "CHECK (state::text = ANY (ARRAY['PAUSED'::text]::text[]))",
    "CHECK (state::text = ANY (ARRAY[lower('PAUSED')]::text[]))",
])
def test_cast_rendering_generator_does_not_generalize_unreviewed_grammar(replacement):
    assert semantics.cast_renderings(replacement) == []
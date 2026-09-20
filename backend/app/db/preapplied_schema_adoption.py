from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa


class PreappliedSchemaAdoptionError(RuntimeError):
    pass


def _col(kind: str, detail: Any = None, nullable: bool = False, default: str | None = None):
    return (kind, detail, nullable, default)


CONTRACTS: dict[str, dict[str, dict[str, Any]]] = {
    "0020": {
        "pinterest_portfolio_plans": {
            "columns": [
                ("id", _col("string", 36)),
                ("store_id", _col("string", 36)),
                ("month_start", _col("date")),
                ("month_end", _col("date")),
                ("target_pins", _col("integer")),
                ("existing_commitments", _col("integer", default="0")),
                ("planned_active_slots", _col("integer", default="0")),
                ("reserve_slots", _col("integer", default="0")),
                ("policy_version", _col("string", 80)),
                ("input_fingerprint", _col("string", 64)),
                ("plan_fingerprint", _col("string", 64)),
                ("status", _col("string", 20, default="DRAFT")),
                ("metadata_json", _col("json")),
                ("created_at", _col("datetime", True, default="now")),
                ("updated_at", _col("datetime", True, default="now")),
            ],
            "pk": ("id",),
            "fks": {
                (("store_id",), "stores", ("id",), "CASCADE"),
            },
            "uniques": {
                "uq_pinterest_portfolio_plan_fingerprint": ("plan_fingerprint",),
            },
            "checks": {
                "ck_pinterest_portfolio_plan_status": ("status", "DRAFT", "ACTIVE", "COMPLETED", "CANCELLED"),
            },
            "indexes": {
                "ix_pinterest_portfolio_plans_store_id": (("store_id",), False, ()),
                "ix_pinterest_portfolio_plans_month_start": (("month_start",), False, ()),
                "ix_pinterest_portfolio_plans_input_fp": (("input_fingerprint",), False, ()),
                "ix_pinterest_portfolio_plans_status": (("status",), False, ()),
                "uq_pinterest_portfolio_active_month": (("store_id", "month_start"), True, ("status", "DRAFT", "ACTIVE")),
            },
        },
        "pinterest_portfolio_plan_items": {
            "columns": [
                ("id", _col("string", 36)),
                ("plan_id", _col("string", 36)),
                ("slot_index", _col("integer")),
                ("is_reserve", _col("boolean", default="false")),
                ("planned_date", _col("date", nullable=True)),
                ("product_id", _col("string", 36)),
                ("local_board_id", _col("string", 36)),
                ("board_key_snapshot", _col("string", 255)),
                ("content_angle_id", _col("string", 36)),
                ("angle_key_snapshot", _col("string", 100)),
                ("seed_keywords", _col("json")),
                ("selection_score", _col("numeric", (12, 6))),
                ("selection_metadata", _col("json")),
                ("item_fingerprint", _col("string", 64)),
                ("status", _col("string", 20, default="PLANNED")),
                ("publication_id", _col("string", 36, nullable=True)),
                ("created_at", _col("datetime", True, default="now")),
                ("updated_at", _col("datetime", True, default="now")),
            ],
            "pk": ("id",),
            "fks": {
                (("plan_id",), "pinterest_portfolio_plans", ("id",), "CASCADE"),
                (("product_id",), "products", ("id",), "RESTRICT"),
                (("local_board_id",), "boards", ("id",), "RESTRICT"),
                (("content_angle_id",), "content_angles", ("id",), "RESTRICT"),
                (("publication_id",), "pin_publications", ("id",), "SET NULL"),
            },
            "uniques": {
                "uq_pinterest_portfolio_plan_item_slot": ("plan_id", "slot_index"),
                "uq_pinterest_portfolio_item_fingerprint": ("item_fingerprint",),
            },
            "checks": {
                "ck_pinterest_portfolio_plan_item_status": (
                    "status", "PLANNED", "PROMOTED", "GENERATED", "SCHEDULED", "PUBLISHED", "FAILED", "SKIPPED"
                ),
            },
            "indexes": {
                "ix_pinterest_portfolio_items_plan_id": (("plan_id",), False, ()),
                "ix_pinterest_portfolio_items_planned_date": (("planned_date",), False, ()),
                "ix_pinterest_portfolio_items_product_id": (("product_id",), False, ()),
                "ix_pinterest_portfolio_items_board_id": (("local_board_id",), False, ()),
                "ix_pinterest_portfolio_items_angle_id": (("content_angle_id",), False, ()),
                "ix_pinterest_portfolio_items_status": (("status",), False, ()),
                "ix_pinterest_portfolio_items_publication_id": (("publication_id",), False, ()),
            },
        },
    },
    "0021": {
        "pinterest_seo_briefs": {
            "columns": [
                ("id", _col("string", 36)),
                ("portfolio_item_id", _col("string", 36)),
                ("policy_version", _col("string", 80)),
                ("input_fingerprint", _col("string", 64)),
                ("seo_fingerprint", _col("string", 64)),
                ("primary_keyword", _col("string", 255)),
                ("secondary_keywords", _col("json")),
                ("intent", _col("string", 100)),
                ("source_evidence", _col("json")),
                ("dimension_scores", _col("json")),
                ("coverage_targets", _col("json")),
                ("guidance", _col("json")),
                ("cannibalization_warnings", _col("json")),
                ("status", _col("string", 20, default="CURRENT")),
                ("created_at", _col("datetime", True, default="now")),
            ],
            "pk": ("id",),
            "fks": {
                (("portfolio_item_id",), "pinterest_portfolio_plan_items", ("id",), "CASCADE"),
            },
            "uniques": {
                "uq_pinterest_seo_brief_portfolio_item": ("portfolio_item_id",),
                "uq_pinterest_seo_brief_fingerprint": ("seo_fingerprint",),
            },
            "checks": {
                "ck_pinterest_seo_brief_status": ("status", "CURRENT", "SUPERSEDED"),
            },
            "indexes": {
                "ix_pinterest_seo_briefs_portfolio_item_id": (("portfolio_item_id",), False, ()),
                "ix_pinterest_seo_briefs_input_fingerprint": (("input_fingerprint",), False, ()),
                "ix_pinterest_seo_briefs_status": (("status",), False, ()),
            },
        },
    },
    "0022": {
        "pinterest_autonomous_generation_runs": {
            "columns": [
                ("id", _col("string", 36)),
                ("portfolio_item_id", _col("string", 36)),
                ("seo_brief_id", _col("string", 36)),
                ("input_fingerprint", _col("string", 64)),
                ("status", _col("string", 20, default="STARTED")),
                ("concept_id", _col("string", 36, nullable=True)),
                ("draft_id", _col("string", 36, nullable=True)),
                ("creative_id", _col("string", 36, nullable=True)),
                ("safe_metadata", _col("json")),
                ("started_at", _col("datetime", True)),
                ("completed_at", _col("datetime", True, nullable=True)),
                ("created_at", _col("datetime", True, default="now")),
            ],
            "pk": ("id",),
            "fks": {
                (("portfolio_item_id",), "pinterest_portfolio_plan_items", ("id",), "CASCADE"),
                (("seo_brief_id",), "pinterest_seo_briefs", ("id",), "RESTRICT"),
                (("concept_id",), "pin_concepts", ("id",), "SET NULL"),
                (("draft_id",), "pin_drafts", ("id",), "SET NULL"),
                (("creative_id",), "pin_creatives", ("id",), "SET NULL"),
            },
            "uniques": {
                "uq_pinterest_autonomous_generation_item": ("portfolio_item_id",),
                "uq_pinterest_autonomous_generation_input_fp": ("input_fingerprint",),
            },
            "checks": {
                "ck_pinterest_autonomous_generation_run_status": ("status", "STARTED", "SUCCEEDED", "FAILED"),
            },
            "indexes": {
                "ix_pinterest_generation_runs_portfolio_item": (("portfolio_item_id",), False, ()),
                "ix_pinterest_generation_runs_seo_brief": (("seo_brief_id",), False, ()),
                "ix_pinterest_generation_runs_status": (("status",), False, ()),
                "ix_pinterest_generation_runs_concept": (("concept_id",), False, ()),
                "ix_pinterest_generation_runs_draft": (("draft_id",), False, ()),
                "ix_pinterest_generation_runs_creative": (("creative_id",), False, ()),
            },
        },
    },
    "0023": {
        "pinterest_analytics_snapshots": {
            "columns": [
                ("id", _col("string", 36)),
                ("publication_id", _col("string", 36)),
                ("pinterest_pin_id", _col("string", 80)),
                ("metric_policy_version", _col("string", 80)),
                ("observation_window", _col("string", 4)),
                ("range_start", _col("date")),
                ("range_end", _col("date")),
                ("provider_payload_fingerprint", _col("string", 64)),
                ("impressions", _col("integer")),
                ("saves", _col("integer")),
                ("pin_clicks", _col("integer")),
                ("outbound_clicks", _col("integer")),
                ("engagements", _col("integer")),
                ("save_rate", _col("numeric", (24, 12))),
                ("pin_click_rate", _col("numeric", (24, 12))),
                ("outbound_click_rate", _col("numeric", (24, 12))),
                ("engagement_rate", _col("numeric", (24, 12))),
                ("safe_metric_map", _col("json")),
                ("observed_at", _col("datetime", True)),
                ("finalized_at", _col("datetime", True)),
                ("created_at", _col("datetime", True, default="now")),
            ],
            "pk": ("id",),
            "fks": {
                (("publication_id",), "pin_publications", ("id",), "RESTRICT"),
            },
            "uniques": {
                "uq_pinterest_analytics_observation": ("publication_id", "observation_window", "metric_policy_version"),
            },
            "checks": {
                "ck_pinterest_analytics_window": ("observation_window", "D1", "D7", "D30", "D90"),
                "ck_pinterest_analytics_counts_nonnegative": (
                    "impressions", "saves", "pin_clicks", "outbound_clicks", "engagements", ">=", "0"
                ),
            },
            "indexes": {
                "ix_pinterest_analytics_snapshots_publication_id": (("publication_id",), False, ()),
                "ix_pinterest_analytics_snapshots_payload_fp": (("provider_payload_fingerprint",), False, ()),
            },
        },
        "pinterest_analytics_ingestion_runs": {
            "columns": [
                ("id", _col("string", 36)),
                ("publication_id", _col("string", 36)),
                ("observation_window", _col("string", 4)),
                ("pinterest_pin_id", _col("string", 80)),
                ("range_start", _col("date")),
                ("range_end", _col("date")),
                ("status", _col("string", 20, default="STARTED")),
                ("provider_payload_fingerprint", _col("string", 64, nullable=True)),
                ("snapshot_id", _col("string", 36, nullable=True)),
                ("started_at", _col("datetime", True)),
                ("completed_at", _col("datetime", True, nullable=True)),
                ("error_code", _col("string", 100, nullable=True)),
                ("safe_metadata", _col("json")),
                ("created_at", _col("datetime", True, default="now")),
            ],
            "pk": ("id",),
            "fks": {
                (("publication_id",), "pin_publications", ("id",), "RESTRICT"),
                (("snapshot_id",), "pinterest_analytics_snapshots", ("id",), "SET NULL"),
            },
            "uniques": {},
            "checks": {
                "ck_pinterest_analytics_run_window": ("observation_window", "D1", "D7", "D30", "D90"),
                "ck_pinterest_analytics_run_status": ("status", "STARTED", "SUCCEEDED", "FAILED"),
            },
            "indexes": {
                "ix_pinterest_analytics_runs_publication_id": (("publication_id",), False, ()),
                "ix_pinterest_analytics_runs_status": (("status",), False, ()),
                "ix_pinterest_analytics_runs_payload_fp": (("provider_payload_fingerprint",), False, ()),
                "ix_pinterest_analytics_runs_snapshot_id": (("snapshot_id",), False, ()),
            },
        },
    },
    "0024": {
        "pinterest_learning_snapshots": {
            "columns": [
                ("id", _col("string", 36)),
                ("store_id", _col("string", 36)),
                ("policy_version", _col("string", 80)),
                ("as_of_at", _col("datetime", True)),
                ("input_fingerprint", _col("string", 64)),
                ("learning_fingerprint", _col("string", 64)),
                ("publication_count", _col("integer")),
                ("snapshot_count", _col("integer")),
                ("global_priors", _col("json")),
                ("rankings", _col("json")),
                ("optimizer_ready", _col("boolean")),
                ("created_at", _col("datetime", True, default="now")),
            ],
            "pk": ("id",),
            "fks": {
                (("store_id",), "stores", ("id",), "RESTRICT"),
            },
            "uniques": {
                "uq_pinterest_learning_input": ("store_id", "policy_version", "input_fingerprint"),
                "uq_pinterest_learning_fingerprint": ("learning_fingerprint",),
            },
            "checks": {
                "ck_pinterest_learning_counts_nonnegative": ("publication_count", "snapshot_count", ">=", "0"),
            },
            "indexes": {
                "ix_pinterest_learning_snapshots_store_id": (("store_id",), False, ()),
                "ix_pinterest_learning_snapshots_as_of_at": (("as_of_at",), False, ()),
                "ix_pinterest_learning_snapshots_input_fp": (("input_fingerprint",), False, ()),
            },
        },
    },
    "0025": {
        "pinterest_optimizer_applications": {
            "columns": [
                ("id", _col("string", 36)),
                ("plan_id", _col("string", 36)),
                ("plan_fingerprint_snapshot", _col("string", 64)),
                ("optimizer_policy_version", _col("string", 80)),
                ("optimizer_fingerprint", _col("string", 64)),
                ("learning_fingerprint", _col("string", 64, nullable=True)),
                ("input_state_fingerprint", _col("string", 64)),
                ("frozen_item_count", _col("integer")),
                ("optimizable_item_count", _col("integer")),
                ("exploit_count", _col("integer")),
                ("explore_count", _col("integer")),
                ("recommendation_snapshot", _col("json")),
                ("status", _col("string", 20, default="APPLIED")),
                ("applied_by", _col("string", 255)),
                ("applied_at", _col("datetime", True)),
                ("created_at", _col("datetime", True, default="now")),
            ],
            "pk": ("id",),
            "fks": {
                (("plan_id",), "pinterest_portfolio_plans", ("id",), "RESTRICT"),
            },
            "uniques": {
                "uq_pinterest_optimizer_application_plan": ("plan_id",),
                "uq_pinterest_optimizer_application_fingerprint": ("optimizer_fingerprint",),
            },
            "checks": {
                "ck_pinterest_optimizer_application_status": ("status", "APPLIED"),
                "ck_pinterest_optimizer_application_counts": (
                    "frozen_item_count", "optimizable_item_count", "exploit_count", "explore_count", ">=", "0"
                ),
            },
            "indexes": {
                "ix_pinterest_optimizer_applications_plan_id": (("plan_id",), False, ()),
                "ix_pinterest_optimizer_applications_input_state": (("input_state_fingerprint",), False, ()),
                "ix_pinterest_optimizer_applications_status": (("status",), False, ()),
            },
        },
    },
    "0026": {
        "pinterest_autonomous_execution_runs": {
            "columns": [
                ("id", _col("string", 36)),
                ("portfolio_item_id", _col("string", 36)),
                ("plan_id", _col("string", 36)),
                ("optimizer_application_id", _col("string", 36)),
                ("input_fingerprint", _col("string", 64)),
                ("status", _col("string", 20, default="STARTED")),
                ("stage", _col("string", 40, default="STARTED")),
                ("seo_brief_id", _col("string", 36, nullable=True)),
                ("generation_run_id", _col("string", 36, nullable=True)),
                ("approval_id", _col("string", 36, nullable=True)),
                ("publication_id", _col("string", 36, nullable=True)),
                ("routine_permit_id", _col("string", 36, nullable=True)),
                ("scheduled_for", _col("datetime", True)),
                ("safe_metadata", _col("json")),
                ("started_at", _col("datetime", True)),
                ("completed_at", _col("datetime", True, nullable=True)),
                ("created_at", _col("datetime", True, default="now")),
            ],
            "pk": ("id",),
            "fks": {
                (("portfolio_item_id",), "pinterest_portfolio_plan_items", ("id",), "RESTRICT"),
                (("plan_id",), "pinterest_portfolio_plans", ("id",), "RESTRICT"),
                (("optimizer_application_id",), "pinterest_optimizer_applications", ("id",), "RESTRICT"),
                (("seo_brief_id",), "pinterest_seo_briefs", ("id",), "SET NULL"),
                (("generation_run_id",), "pinterest_autonomous_generation_runs", ("id",), "SET NULL"),
                (("approval_id",), "pin_approvals", ("id",), "SET NULL"),
                (("publication_id",), "pin_publications", ("id",), "SET NULL"),
                (("routine_permit_id",), "routine_dispatch_permits", ("id",), "SET NULL"),
            },
            "uniques": {
                "uq_pinterest_auto_exec_portfolio_item": ("portfolio_item_id",),
                "uq_pinterest_auto_exec_input_fingerprint": ("input_fingerprint",),
            },
            "checks": {
                "ck_pinterest_auto_exec_status": ("status", "STARTED", "SUCCEEDED", "FAILED"),
                "ck_pinterest_auto_exec_stage": (
                    "stage", "STARTED", "SEO_READY", "GENERATED", "AUTHORIZED", "PUBLICATION_CREATED", "PERMITTED"
                ),
            },
            "indexes": {
                "ix_pinterest_auto_exec_plan_id": (("plan_id",), False, ()),
                "ix_pinterest_auto_exec_optimizer_app": (("optimizer_application_id",), False, ()),
                "ix_pinterest_auto_exec_status": (("status",), False, ()),
                "ix_pinterest_auto_exec_stage": (("stage",), False, ()),
                "ix_pinterest_auto_exec_scheduled_for": (("scheduled_for",), False, ()),
                "ix_pinterest_auto_exec_plan_stage": (("plan_id", "stage"), False, ()),
                "ix_pinterest_auto_exec_seo_brief": (("seo_brief_id",), False, ()),
                "ix_pinterest_auto_exec_generation": (("generation_run_id",), False, ()),
                "ix_pinterest_auto_exec_approval": (("approval_id",), False, ()),
                "ix_pinterest_auto_exec_publication": (("publication_id",), False, ()),
                "ix_pinterest_auto_exec_permit": (("routine_permit_id",), False, ()),
            },
        },
    },
}


def _fail(revision: str, message: str) -> None:
    raise PreappliedSchemaAdoptionError(
        f"{revision} pre-applied schema adoption refused: {message}"
    )


def _default_semantic(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    while len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        text = text[1:-1].strip()
    lower = text.lower()
    if lower in {"now()", "current_timestamp", "current_timestamp()"}:
        return "now"
    if "::" in text:
        text = text.split("::", 1)[0].strip()
    while len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        text = text[1:-1].strip()
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        text = text[1:-1]
    lower = text.lower()
    if lower in {"false", "true"}:
        return lower
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return text
    return text


def _type_signature(type_: Any) -> tuple[str, Any]:
    if isinstance(type_, sa.JSON):
        return ("json", None)
    if isinstance(type_, sa.DateTime):
        return ("datetime", bool(type_.timezone))
    if isinstance(type_, sa.Date):
        return ("date", None)
    if isinstance(type_, sa.String):
        return ("string", type_.length)
    if isinstance(type_, sa.Integer):
        return ("integer", None)
    if isinstance(type_, sa.Boolean):
        return ("boolean", None)
    if isinstance(type_, sa.Numeric):
        return ("numeric", (type_.precision, type_.scale))
    return (type(type_).__name__.lower(), None)


def _predicate_text(index: dict[str, Any]) -> str:
    options = index.get("dialect_options") or {}
    value = options.get("postgresql_where")
    return "" if value is None else str(value)


def _tokens_present(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return all(str(token).lower() in lowered for token in tokens)


def _validate_columns(inspector: Any, revision: str, table: str, contract: dict[str, Any]) -> None:
    actual = inspector.get_columns(table)
    expected = contract["columns"]
    if tuple(row["name"] for row in actual) != tuple(name for name, _ in expected):
        _fail(revision, f"{table} column order/set differs")
    for row, (name, signature) in zip(actual, expected):
        kind, detail, nullable, default = signature
        if _type_signature(row["type"]) != (kind, detail):
            _fail(revision, f"{table}.{name} type differs")
        if bool(row.get("nullable")) != nullable:
            _fail(revision, f"{table}.{name} nullability differs")
        if _default_semantic(row.get("default")) != default:
            _fail(revision, f"{table}.{name} server default differs")
        if row.get("identity") is not None or row.get("computed") is not None:
            _fail(revision, f"{table}.{name} unexpectedly generated")


def _validate_pk(inspector: Any, revision: str, table: str, contract: dict[str, Any]) -> None:
    pk = inspector.get_pk_constraint(table) or {}
    if tuple(pk.get("constrained_columns") or ()) != tuple(contract["pk"]):
        _fail(revision, f"{table} primary key differs")


def _validate_fks(inspector: Any, revision: str, table: str, contract: dict[str, Any]) -> None:
    actual = set()
    for fk in inspector.get_foreign_keys(table):
        options = fk.get("options") or {}
        actual.add((
            tuple(fk.get("constrained_columns") or ()),
            fk.get("referred_table"),
            tuple(fk.get("referred_columns") or ()),
            str(options.get("ondelete") or "NO ACTION").upper(),
        ))
    if actual != set(contract["fks"]):
        _fail(revision, f"{table} foreign keys differ")


def _validate_uniques(inspector: Any, revision: str, table: str, contract: dict[str, Any]) -> None:
    actual = {
        item.get("name"): tuple(item.get("column_names") or item.get("constrained_columns") or ())
        for item in inspector.get_unique_constraints(table)
    }
    if actual != contract["uniques"]:
        _fail(revision, f"{table} unique constraints differ")


def _validate_checks(inspector: Any, revision: str, table: str, contract: dict[str, Any]) -> None:
    actual = {
        item.get("name"): str(item.get("sqltext") or "")
        for item in inspector.get_check_constraints(table)
    }
    if set(actual) != set(contract["checks"]):
        _fail(revision, f"{table} check constraint set differs")
    for name, tokens in contract["checks"].items():
        if not _tokens_present(actual[name], tuple(tokens)):
            _fail(revision, f"{table} check constraint {name} differs")


def _validate_indexes(inspector: Any, revision: str, table: str, contract: dict[str, Any]) -> None:
    actual: dict[str, dict[str, Any]] = {}
    for index in inspector.get_indexes(table):
        if index.get("duplicates_constraint"):
            continue
        actual[index.get("name")] = index
    if set(actual) != set(contract["indexes"]):
        _fail(revision, f"{table} index set differs")
    for name, (columns, unique, predicate_tokens) in contract["indexes"].items():
        index = actual[name]
        if tuple(index.get("column_names") or ()) != tuple(columns):
            _fail(revision, f"{table} index {name} columns differ")
        if bool(index.get("unique")) != bool(unique):
            _fail(revision, f"{table} index {name} uniqueness differs")
        predicate = _predicate_text(index)
        if predicate_tokens:
            if not predicate or not _tokens_present(predicate, tuple(predicate_tokens)):
                _fail(revision, f"{table} index {name} predicate differs")
        elif predicate:
            _fail(revision, f"{table} index {name} unexpectedly partial")


def _validate_empty(bind: Any, revision: str, table: str) -> None:
    count = bind.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()
    if int(count) != 0:
        _fail(revision, f"{table} is not empty")


def adopt_preapplied_revision(bind: Any, revision: str) -> bool:
    """Return True only when this revision's exact empty schema already exists.

    False means none of the revision-owned tables exist and the migration should
    execute its canonical DDL normally. Any partial/drifted/non-empty presence
    raises and prevents Alembic bookkeeping from advancing.
    """

    contract = CONTRACTS.get(revision)
    if contract is None:
        raise ValueError(f"unsupported adoption revision {revision}")

    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    owned = tuple(contract)
    present = [table for table in owned if table in existing_tables]

    if not present:
        return False
    if len(present) != len(owned):
        _fail(revision, "revision-owned table presence is partial")
    if bind.dialect.name != "postgresql":
        _fail(revision, "pre-applied adoption is supported only for PostgreSQL")

    for table in owned:
        table_contract = contract[table]
        _validate_columns(inspector, revision, table, table_contract)
        _validate_pk(inspector, revision, table, table_contract)
        _validate_fks(inspector, revision, table, table_contract)
        _validate_uniques(inspector, revision, table, table_contract)
        _validate_checks(inspector, revision, table, table_contract)
        _validate_indexes(inspector, revision, table, table_contract)
        _validate_empty(bind, revision, table)

    return True

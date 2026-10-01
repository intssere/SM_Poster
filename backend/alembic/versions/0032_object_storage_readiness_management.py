"""Add durable management-only object-storage readiness admissions.

Revision ID: 0032
Revises: 0031

This additive revision owns one PostgreSQL-only coordinator table. It has no
business foreign keys and performs no business-row writes.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


TABLE = "management_readiness_admissions"


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("0032 object-storage readiness management requires PostgreSQL")

    # Existing target tables are never accepted by name alone. Refusing all
    # pre-existing states is safer than adopting a partial or subtly drifted
    # coordinator contract.
    if bind.execute(
        sa.text("SELECT to_regclass('public.management_readiness_admissions')")
    ).scalar_one() is not None:
        raise RuntimeError(
            "0032 refuses a pre-existing management_readiness_admissions table"
        )
    for function in (
        "public.management_readiness_admission_guard()",
        "public.management_readiness_admission_truncate_guard()",
    ):
        if bind.execute(
            sa.text("SELECT to_regprocedure(:function)"),
            {"function": function},
        ).scalar_one() is not None:
            raise RuntimeError("0032 refuses pre-existing readiness guard functions")

    op.create_table(
        TABLE,
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("release_commit_sha", sa.String(64), nullable=False),
        sa.Column("release_tree_sha", sa.String(64), nullable=False),
        sa.Column("descriptor_sha256", sa.String(64), nullable=False),
        sa.Column("grant_id", sa.String(255), nullable=False),
        sa.Column("actor_hash", sa.String(64), nullable=False),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            sa.String(16),
            server_default=sa.text("'ADMITTED'"),
            nullable=False,
        ),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("receipt", postgresql.JSONB(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "operation",
            "release_commit_sha",
            "release_tree_sha",
            name="pk_management_readiness_admissions",
        ),
        sa.UniqueConstraint(
            "grant_id",
            name="uq_management_readiness_admissions_grant",
        ),
        sa.CheckConstraint(
            "operation = 'object_storage_readiness_v1'",
            name="ck_management_readiness_admissions_operation",
        ),
        sa.CheckConstraint(
            "descriptor_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_management_readiness_admissions_descriptor_hash",
        ),
        sa.CheckConstraint(
            "actor_hash ~ '^[0-9a-f]{64}$'",
            name="ck_management_readiness_admissions_actor_hash",
        ),
        sa.CheckConstraint(
            "outcome = 'ADMITTED' OR outcome = 'PASS' OR "
            "outcome = 'FAILED' OR outcome = 'UNKNOWN'",
            name="ck_management_readiness_admissions_outcome",
        ),
        sa.CheckConstraint(
            "("
            "outcome = 'ADMITTED' AND exit_code IS NULL "
            "AND receipt IS NULL AND finished_at IS NULL"
            ") OR ("
            "outcome = 'PASS' AND exit_code = 0 AND receipt IS NOT NULL "
            "AND jsonb_typeof(receipt) = 'object' "
            "AND receipt ->> 'final_status' IS NOT DISTINCT FROM 'PASS' "
            "AND finished_at IS NOT NULL"
            ") OR ("
            "outcome = 'FAILED' AND exit_code IS NOT NULL "
            "AND exit_code IN (1, 2) AND receipt IS NOT NULL "
            "AND jsonb_typeof(receipt) = 'object' AND ("
            "(exit_code = 1 AND receipt ->> 'final_status' "
            "IS NOT DISTINCT FROM 'FAILED') OR "
            "(exit_code = 2 AND receipt ->> 'final_status' "
            "IS NOT DISTINCT FROM 'BLOCKED')"
            ") AND finished_at IS NOT NULL"
            ") OR ("
            "outcome = 'UNKNOWN' AND finished_at IS NOT NULL AND ("
            "receipt IS NULL OR (jsonb_typeof(receipt) = 'object' "
            "AND exit_code IS NOT NULL AND ("
            "(exit_code = 0 AND receipt ->> 'final_status' "
            "IS NOT DISTINCT FROM 'PASS') OR "
            "(exit_code = 1 AND receipt ->> 'final_status' "
            "IS NOT DISTINCT FROM 'FAILED') OR "
            "(exit_code = 2 AND receipt ->> 'final_status' "
            "IS NOT DISTINCT FROM 'BLOCKED')"
            ")))"
            ")",
            name="ck_management_readiness_admissions_outcome_evidence",
        ),
        schema="public",
    )

    op.execute(
        """
        CREATE FUNCTION public.management_readiness_admission_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.outcome <> 'ADMITTED'
                   OR NEW.finished_at IS NOT NULL
                   OR NEW.exit_code IS NOT NULL
                   OR NEW.receipt IS NOT NULL THEN
                    RAISE EXCEPTION 'readiness admission must start ADMITTED'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'readiness admissions cannot be deleted'
                    USING ERRCODE = '55000';
            ELSIF TG_OP = 'UPDATE' THEN
                IF OLD.outcome <> 'ADMITTED' THEN
                    RAISE EXCEPTION 'terminal readiness outcomes are immutable'
                        USING ERRCODE = '55000';
                END IF;
                IF ROW(
                    NEW.operation,
                    NEW.release_commit_sha,
                    NEW.release_tree_sha,
                    NEW.descriptor_sha256,
                    NEW.grant_id,
                    NEW.actor_hash,
                    NEW.consumed_at
                ) IS DISTINCT FROM ROW(
                    OLD.operation,
                    OLD.release_commit_sha,
                    OLD.release_tree_sha,
                    OLD.descriptor_sha256,
                    OLD.grant_id,
                    OLD.actor_hash,
                    OLD.consumed_at
                ) THEN
                    RAISE EXCEPTION 'readiness admission binding is immutable'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.outcome NOT IN ('PASS', 'FAILED', 'UNKNOWN')
                   OR NEW.finished_at IS NULL THEN
                    RAISE EXCEPTION 'readiness outcome must transition once to terminal'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.outcome = 'PASS' AND (
                    NEW.exit_code IS DISTINCT FROM 0
                    OR NEW.receipt IS NULL
                    OR jsonb_typeof(NEW.receipt) IS DISTINCT FROM 'object'
                    OR NEW.receipt ->> 'final_status' IS DISTINCT FROM 'PASS'
                ) THEN
                    RAISE EXCEPTION 'PASS readiness requires a valid PASS receipt'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.outcome = 'FAILED' AND (
                    NEW.receipt IS NULL
                    OR jsonb_typeof(NEW.receipt) IS DISTINCT FROM 'object'
                    OR NEW.exit_code IS NULL
                    OR NOT (
                        (NEW.exit_code = 1 AND
                         NEW.receipt ->> 'final_status' IS NOT DISTINCT FROM 'FAILED')
                        OR
                        (NEW.exit_code = 2 AND
                         NEW.receipt ->> 'final_status' IS NOT DISTINCT FROM 'BLOCKED')
                    )
                ) THEN
                    RAISE EXCEPTION 'FAILED readiness requires matching failed receipt'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.outcome = 'UNKNOWN' AND NEW.receipt IS NOT NULL AND (
                    jsonb_typeof(NEW.receipt) IS DISTINCT FROM 'object'
                    OR NEW.exit_code IS NULL
                    OR NOT (
                        (NEW.exit_code = 0 AND
                         NEW.receipt ->> 'final_status' IS NOT DISTINCT FROM 'PASS')
                        OR
                        (NEW.exit_code = 1 AND
                         NEW.receipt ->> 'final_status' IS NOT DISTINCT FROM 'FAILED')
                        OR
                        (NEW.exit_code = 2 AND
                         NEW.receipt ->> 'final_status' IS NOT DISTINCT FROM 'BLOCKED')
                    )
                ) THEN
                    RAISE EXCEPTION 'UNKNOWN readiness receipt and exit code mismatch'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'unsupported readiness admission operation'
                USING ERRCODE = '55000';
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.management_readiness_admission_truncate_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'readiness admissions cannot be truncated'
                USING ERRCODE = '55000';
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER management_readiness_admissions_immutable
        BEFORE INSERT OR UPDATE OR DELETE
        ON public.management_readiness_admissions
        FOR EACH ROW
        EXECUTE FUNCTION public.management_readiness_admission_guard()
        """
    )
    op.execute(
        """
        CREATE TRIGGER management_readiness_admissions_no_truncate
        BEFORE TRUNCATE
        ON public.management_readiness_admissions
        FOR EACH STATEMENT
        EXECUTE FUNCTION public.management_readiness_admission_truncate_guard()
        """
    )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("0032 object-storage readiness management requires PostgreSQL")
    bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    bind.execute(
        sa.text(
            'LOCK TABLE "public"."management_readiness_admissions" '
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    count = bind.scalar(
        sa.text('SELECT count(*) FROM "public"."management_readiness_admissions"')
    )
    if count:
        raise RuntimeError(
            "0032 downgrade blocked: readiness admission evidence exists"
        )
    op.drop_table(TABLE, schema="public")
    op.execute(
        "DROP FUNCTION public.management_readiness_admission_guard()"
    )
    op.execute(
        "DROP FUNCTION public.management_readiness_admission_truncate_guard()"
    )
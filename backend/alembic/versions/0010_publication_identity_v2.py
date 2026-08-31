"""Bind approvals and publication snapshots to exact reviewed identities.

Revision ID: 0010
Revises: 0009

All new identity columns are nullable so historical approvals/publications remain
readable.  The migration intentionally does not infer revision identity from
mutable proposal state.
"""

from alembic import op
import sqlalchemy as sa


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("pin_approvals", sa.Column("revision_id", sa.String(36), nullable=True))
    op.add_column("pin_approvals", sa.Column("creative_id", sa.String(36), nullable=True))
    op.add_column("pin_approvals", sa.Column("approved_version_id", sa.String(36), nullable=True))
    op.create_index("ix_pin_approvals_revision_id", "pin_approvals", ["revision_id"])
    op.create_index("ix_pin_approvals_creative_id", "pin_approvals", ["creative_id"])
    op.create_index("ix_pin_approvals_approved_version_id", "pin_approvals", ["approved_version_id"])
    op.create_foreign_key(
        "fk_pin_approvals_revision_id", "pin_approvals", "content_revisions",
        ["revision_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_pin_approvals_creative_id", "pin_approvals", "pin_creatives",
        ["creative_id"], ["id"], ondelete="SET NULL",
    )

    publication_columns = (
        sa.Column("revision_id", sa.String(36), nullable=True),
        sa.Column("approval_id", sa.String(36), nullable=True),
        sa.Column("source_image_id", sa.String(36), nullable=True),
        sa.Column("template_id", sa.String(36), nullable=True),
        sa.Column("template_key", sa.String(120), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("text_fingerprint", sa.String(64), nullable=True),
        sa.Column("creative_fingerprint", sa.String(64), nullable=True),
        sa.Column("pinterest_board_id", sa.String(80), nullable=True),
        sa.Column("integration_account_id", sa.String(36), nullable=True),
        sa.Column("destination_url", sa.Text(), nullable=True),
        sa.Column("utm_url", sa.Text(), nullable=True),
    )
    for column in publication_columns:
        op.add_column("pin_publications", column)

    indexed = (
        "revision_id", "approval_id", "source_image_id", "template_id",
        "text_fingerprint", "creative_fingerprint", "pinterest_board_id",
        "integration_account_id",
    )
    for column in indexed:
        op.create_index(f"ix_pin_publications_{column}", "pin_publications", [column])
    for name, target in (
        ("revision_id", "content_revisions"),
        ("approval_id", "pin_approvals"),
        ("source_image_id", "product_images"),
        ("template_id", "creative_templates"),
        ("integration_account_id", "integration_accounts"),
    ):
        op.create_foreign_key(
            f"fk_pin_publications_{name}", "pin_publications", target,
            [name], ["id"], ondelete="SET NULL",
        )


def downgrade():
    for name in (
        "revision_id", "approval_id", "source_image_id", "template_id",
        "integration_account_id",
    ):
        op.drop_constraint(f"fk_pin_publications_{name}", "pin_publications", type_="foreignkey")
    for column in (
        "revision_id", "approval_id", "source_image_id", "template_id",
        "text_fingerprint", "creative_fingerprint", "pinterest_board_id",
        "integration_account_id",
    ):
        op.drop_index(f"ix_pin_publications_{column}", table_name="pin_publications")
    for column in (
        "utm_url", "destination_url", "integration_account_id", "pinterest_board_id",
        "creative_fingerprint", "text_fingerprint", "template_version", "template_key",
        "template_id", "source_image_id", "approval_id", "revision_id",
    ):
        op.drop_column("pin_publications", column)

    op.drop_constraint("fk_pin_approvals_creative_id", "pin_approvals", type_="foreignkey")
    op.drop_constraint("fk_pin_approvals_revision_id", "pin_approvals", type_="foreignkey")
    op.drop_index("ix_pin_approvals_approved_version_id", table_name="pin_approvals")
    op.drop_index("ix_pin_approvals_creative_id", table_name="pin_approvals")
    op.drop_index("ix_pin_approvals_revision_id", table_name="pin_approvals")
    op.drop_column("pin_approvals", "approved_version_id")
    op.drop_column("pin_approvals", "creative_id")
    op.drop_column("pin_approvals", "revision_id")

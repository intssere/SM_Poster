"""Pinterest board and section snapshot."""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("pinterest_boards",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("pinterest_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_board_id", sa.String(255), nullable=False), sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()), sa.Column("privacy", sa.String(40)), sa.Column("owner_username", sa.String(255)),
        sa.Column("pin_count", sa.Integer()), sa.Column("follower_count", sa.Integer()), sa.Column("collaborator_count", sa.Integer()),
        sa.Column("is_ads_only", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("image_cover_url", sa.Text()),
        sa.Column("board_pins_modified_at", sa.DateTime(timezone=True)), sa.Column("provider_created_at", sa.DateTime(timezone=True)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("is_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("routing_label", sa.String(120)), sa.Column("last_seen_at", sa.DateTime(timezone=True)), sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("connection_id", "external_board_id", name="uq_pinterest_board_identity"))
    op.create_index("ix_pinterest_boards_connection_id", "pinterest_boards", ["connection_id"])
    op.create_table("pinterest_board_sections",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("board_id", sa.String(36), sa.ForeignKey("pinterest_boards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_section_id", sa.String(255), nullable=False), sa.Column("name", sa.String(255), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)), sa.Column("last_synced_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("board_id", "external_section_id", name="uq_pinterest_section_identity"))
    op.create_index("ix_pinterest_board_sections_board_id", "pinterest_board_sections", ["board_id"])

def downgrade():
    op.drop_index("ix_pinterest_board_sections_board_id", table_name="pinterest_board_sections"); op.drop_table("pinterest_board_sections")
    op.drop_index("ix_pinterest_boards_connection_id", table_name="pinterest_boards"); op.drop_table("pinterest_boards")

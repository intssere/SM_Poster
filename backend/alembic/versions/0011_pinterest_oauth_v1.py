"""Pinterest OAuth state and encrypted connection storage."""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("pinterest_oauth_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("initiated_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("redirect_after", sa.Text()), sa.UniqueConstraint("state_hash"))
    op.create_index("ix_pinterest_oauth_states_state_hash", "pinterest_oauth_states", ["state_hash"])
    op.create_table("pinterest_connections",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("external_user_id", sa.String(255), nullable=False), sa.Column("username", sa.String(255)),
        sa.Column("account_type", sa.String(80)), sa.Column("profile_image_url", sa.Text()),
        sa.Column("granted_scopes", sa.JSON(), nullable=False), sa.Column("access_token_ciphertext", sa.Text(), nullable=False),
        sa.Column("refresh_token_ciphertext", sa.Text(), nullable=False), sa.Column("access_token_expires_at", sa.DateTime(timezone=True)),
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True)), sa.Column("token_type", sa.String(40)),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("connected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("refreshed_at", sa.DateTime(timezone=True)), sa.Column("disconnected_at", sa.DateTime(timezone=True)),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)), sa.Column("last_error_code", sa.String(120)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.create_index("ix_pinterest_connections_status", "pinterest_connections", ["status"])

def downgrade():
    op.drop_index("ix_pinterest_connections_status", table_name="pinterest_connections")
    op.drop_table("pinterest_connections")
    op.drop_index("ix_pinterest_oauth_states_state_hash", table_name="pinterest_oauth_states")
    op.drop_table("pinterest_oauth_states")

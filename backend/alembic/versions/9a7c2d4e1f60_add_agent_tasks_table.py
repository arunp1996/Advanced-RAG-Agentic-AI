"""Add agent_tasks table.

Revision ID: 9a7c2d4e1f60
Revises: ff8f1ab88451
"""

from alembic import op
import sqlalchemy as sa

revision = "9a7c2d4e1f60"
down_revision = "ff8f1ab88451"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("logs", sa.JSON(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("pause_requested", sa.Boolean(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_agent_tasks_agent_type",
        "agent_tasks",
        ["agent_type"],
    )
    op.create_index(
        "ix_agent_tasks_requested_by_user_id",
        "agent_tasks",
        ["requested_by_user_id"],
    )
    op.create_index(
        "ix_agent_tasks_status",
        "agent_tasks",
        ["status"],
    )


def downgrade():
    op.drop_index("ix_agent_tasks_status", table_name="agent_tasks")
    op.drop_index(
        "ix_agent_tasks_requested_by_user_id",
        table_name="agent_tasks",
    )
    op.drop_index("ix_agent_tasks_agent_type", table_name="agent_tasks")
    op.drop_table("agent_tasks")

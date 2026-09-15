"""Add the per-session outgoing message queue.

A session can only run one turn at a time. Until now a message sent while a
turn was running was rejected, so the user had to wait and retype it. This
revision adds a durable FIFO queue per session: messages accepted while the
runtime is busy are stored here and dispatched in order as each turn finishes.

The queue is a table rather than in-memory state because the connector owning
the runtime can restart, and the server can fail over between workers, without
the user losing messages they already submitted.

``client_message_id`` is what the composer submitted, so the optimistic bubble
can be reconciled with the queued row; ``position`` orders the queue and is
unique per session so two workers cannot claim the same slot.

Revision ID: v2_36
Revises: v2_35
"""

import sqlalchemy as sa
from alembic import op

revision = "v2_36"
down_revision = "v2_35"
branch_labels = None
depends_on = None

QUEUE_TABLE = "session_message_queue"
POSITION_INDEX = "idx_session_message_queue_position"


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        statement = sa.text(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"
        )
    else:
        statement = sa.text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = :name"
        )
    return bind.execute(statement, {"name": name}).first() is not None


def upgrade() -> None:
    if _table_exists(QUEUE_TABLE):
        return
    op.create_table(
        QUEUE_TABLE,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("attachments_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("selections_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("client_message_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("updated_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    # Every read is "this session's queue in order", so the index carries both
    # keys and the dispatcher can take the head row without a sort.
    op.create_index(
        POSITION_INDEX,
        QUEUE_TABLE,
        ["session_id", "position"],
    )


def downgrade() -> None:
    if _table_exists(QUEUE_TABLE):
        op.drop_table(QUEUE_TABLE)

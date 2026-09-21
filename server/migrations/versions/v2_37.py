"""Record timeline items the reader deleted.

A session's timeline is a mirror of the runtime's own history, and no runtime
exposes a delete: DSH, Codex and Claude all answer "method not found". Removing a
row outright would therefore be undone by the next sync, because the runtime
still reports the item and the ingest path re-inserts any id it does not find.

Deletion is consequently local and durable: the row stays, and a timestamp marks
it as hidden. The column is deliberately outside the set the runtime write path
sets, so a later sync updates the item's content without clearing the mark — the
reader's decision outlives the runtime's continued reporting of the item.

Revision ID: v2_37
Revises: v2_36
"""

import sqlalchemy as sa
from alembic import op

revision = "v2_37"
down_revision = "v2_36"
branch_labels = None
depends_on = None

TABLE = "timeline_items"
COLUMN = "hidden_at"


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {entry["name"] for entry in inspector.get_columns(table)}


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    if not _table_exists(TABLE) or _column_exists(TABLE, COLUMN):
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.Text(), nullable=True))
    # Queries filter hidden rows out of a session's timeline, so the index keeps
    # that filter from scanning items the reader never hid.
    op.create_index(
        "idx_timeline_items_session_visible",
        TABLE,
        ["session_id", COLUMN],
    )


def downgrade() -> None:
    if not _table_exists(TABLE) or not _column_exists(TABLE, COLUMN):
        return
    op.drop_index("idx_timeline_items_session_visible", table_name=TABLE)
    op.drop_column(TABLE, COLUMN)

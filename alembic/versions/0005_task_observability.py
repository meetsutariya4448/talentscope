"""Add task_executions/failed_tasks (Celery task-state + dead-letter store)
and fix search_vector staleness by making it a Postgres GENERATED column.

Previously search_vector was populated once, by a manual `UPDATE ... SET
search_vector = to_tsvector(...)` run only on insert (app/ingestion/ingest.py).
A posting whose title/description/location changed on a later fetch got a
new PostingDescriptionVersion row but its search_vector was never
recomputed, so FTS silently searched stale text forever. A GENERATED ALWAYS
... STORED column is recomputed by Postgres on every row write, which is
what "incremental indexing" should mean here — the index tracks its source
columns automatically instead of relying on every write path remembering to
refresh it by hand.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-26 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TSVECTOR_EXPR = (
    "to_tsvector('english', coalesce(title,'') || ' ' || "
    "coalesce(description,'') || ' ' || coalesce(location,''))"
)


def upgrade() -> None:
    op.create_table(
        "task_executions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(155), nullable=False),
        sa.Column("task_name", sa.String(255), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("worker_hostname", sa.String(255), nullable=True),
        sa.Column("args", sa.Text(), nullable=True),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_task_executions_task_id"),
    )
    op.create_index(
        "ix_task_executions_task_name_state", "task_executions", ["task_name", "state"]
    )
    op.create_index("ix_task_executions_started_at", "task_executions", ["started_at"])

    op.create_table(
        "failed_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(155), nullable=False),
        sa.Column("task_name", sa.String(255), nullable=False),
        sa.Column("args", sa.Text(), nullable=True),
        sa.Column("kwargs", sa.Text(), nullable=True),
        sa.Column("exception", sa.Text(), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_failed_tasks_task_name", "failed_tasks", ["task_name"])
    op.create_index("ix_failed_tasks_failed_at", "failed_tasks", ["failed_at"])

    # Postgres has no ALTER COLUMN ... ADD GENERATED for an existing column —
    # the column must be dropped and re-added. Drop the dependent GIN index
    # first, drop the plain column, re-add it as a generated column (which
    # backfills existing rows automatically), then rebuild the index.
    op.drop_index("ix_postings_search_vector", table_name="postings")
    op.drop_column("postings", "search_vector")
    op.execute(
        f"ALTER TABLE postings ADD COLUMN search_vector tsvector "
        f"GENERATED ALWAYS AS ({_TSVECTOR_EXPR}) STORED"
    )
    op.create_index(
        "ix_postings_search_vector", "postings", ["search_vector"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_postings_search_vector", table_name="postings")
    op.drop_column("postings", "search_vector")
    op.add_column("postings", sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True))
    op.execute(f"UPDATE postings SET search_vector = {_TSVECTOR_EXPR}")
    op.create_index(
        "ix_postings_search_vector", "postings", ["search_vector"], postgresql_using="gin"
    )

    op.drop_index("ix_failed_tasks_failed_at", table_name="failed_tasks")
    op.drop_index("ix_failed_tasks_task_name", table_name="failed_tasks")
    op.drop_table("failed_tasks")

    op.drop_index("ix_task_executions_started_at", table_name="task_executions")
    op.drop_index("ix_task_executions_task_name_state", table_name="task_executions")
    op.drop_table("task_executions")

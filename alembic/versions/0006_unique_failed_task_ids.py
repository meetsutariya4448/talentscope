"""Ensure dead-letter entries are unique by Celery task id.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-24 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep the newest copy if a previously redelivered failure signal created
    # duplicate rows before this constraint existed.
    op.execute(
        "DELETE FROM failed_tasks older USING failed_tasks newer "
        "WHERE older.task_id = newer.task_id AND older.id < newer.id"
    )
    op.create_unique_constraint(
        "uq_failed_tasks_task_id", "failed_tasks", ["task_id"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_failed_tasks_task_id", "failed_tasks", type_="unique"
    )

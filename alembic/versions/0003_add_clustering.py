"""Add skill_clusters table and cluster_id column on postings

Revision ID: 0003
Revises: 0002
Create Date: 2024-01-03 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("postings", sa.Column("cluster_id", sa.Integer(), nullable=True))

    op.create_table(
        "skill_clusters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(255)),
        sa.Column("size", sa.Integer()),
        sa.Column("top_skills", sa.Text()),   # JSON list of skill names
        sa.Column("silhouette", sa.Numeric(8, 6)),
        sa.Column("run_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_skill_clusters_run_at", "skill_clusters", ["run_at"])


def downgrade() -> None:
    op.drop_index("ix_skill_clusters_run_at", table_name="skill_clusters")
    op.drop_table("skill_clusters")
    op.drop_column("postings", "cluster_id")

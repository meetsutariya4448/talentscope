"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create companies table
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("greenhouse_token", sa.String(255), nullable=True),
        sa.Column("lever_slug", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )

    # Create skills table
    op.create_table(
        "skills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    # Create postings table
    op.create_table(
        "postings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("location", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("salary_min", sa.Numeric(12, 2), nullable=True),
        sa.Column("salary_max", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(10), nullable=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(512), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_id", name="uq_postings_source_source_id"),
    )
    op.create_index(
        "ix_postings_search_vector",
        "postings",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_postings_company_title_location",
        "postings",
        ["company_id", "title", "location"],
    )
    op.create_index("ix_postings_posted_at", "postings", ["posted_at"])
    op.create_index("ix_postings_source", "postings", ["source"])

    # Create posting_skills table
    op.create_table(
        "posting_skills",
        sa.Column("posting_id", sa.Integer(), nullable=False),
        sa.Column("skill_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("posting_id", "skill_id"),
    )


def downgrade() -> None:
    op.drop_table("posting_skills")
    op.drop_index("ix_postings_source", table_name="postings")
    op.drop_index("ix_postings_posted_at", table_name="postings")
    op.drop_index("ix_postings_company_title_location", table_name="postings")
    op.drop_index("ix_postings_search_vector", table_name="postings")
    op.drop_table("postings")
    op.drop_table("skills")
    op.drop_table("companies")

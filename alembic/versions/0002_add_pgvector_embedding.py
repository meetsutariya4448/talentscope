"""Add pgvector extension and embedding column to postings

Revision ID: 0002
Revises: 0001
Create Date: 2024-01-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column(
        "postings",
        sa.Column("embedding", sa.Text(), nullable=True),  # stored as native vector via raw DDL
    )
    # Replace the TEXT column with an actual vector(384) column.
    # SQLAlchemy's add_column can't express the pgvector type directly, so we
    # drop and recreate using raw DDL.
    op.execute("ALTER TABLE postings DROP COLUMN embedding")
    op.execute("ALTER TABLE postings ADD COLUMN embedding vector(384)")

    # HNSW index: supports incremental inserts without rebuild, better than IVFFlat
    # for a growing corpus.  m=16, ef_construction=64 are pgvector's recommended defaults.
    op.execute(
        "CREATE INDEX ix_postings_embedding_hnsw ON postings "
        "USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_postings_embedding_hnsw")
    op.execute("ALTER TABLE postings DROP COLUMN IF EXISTS embedding")
    op.execute("DROP EXTENSION IF EXISTS vector")

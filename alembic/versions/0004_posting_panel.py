"""Add posting-panel tracking: snapshots, description versions, per-company
collection health, monitored-company registry, and application log.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-19 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("ashby_board_name", sa.String(255), nullable=True))

    op.add_column("postings", sa.Column("company_token", sa.String(255), nullable=True))
    op.add_column("postings", sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("postings", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("postings", sa.Column("disappeared_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("postings", sa.Column("description_hash", sa.String(64), nullable=True))
    op.add_column("postings", sa.Column("raw_hash", sa.String(64), nullable=True))
    op.add_column(
        "postings",
        sa.Column("left_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "postings",
        sa.Column("absence_episode_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # Backfill: TalentScope's pre-panel ingestion gives no information about how
    # long an existing posting had already been open, so every row from before
    # this migration is left-truncated by definition.
    op.execute(
        "UPDATE postings SET first_seen_at = created_at, last_seen_at = created_at, "
        "left_truncated = true WHERE first_seen_at IS NULL"
    )

    op.create_index(
        "ix_postings_active_last_seen",
        "postings",
        ["last_seen_at"],
        postgresql_where=sa.text("disappeared_at IS NULL"),
    )

    op.create_table(
        "posting_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("posting_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("description_hash", sa.String(64), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("posting_id", "snapshot_date", name="uq_posting_snapshots_posting_date"),
    )
    op.create_index("ix_posting_snapshots_date", "posting_snapshots", ["snapshot_date"])

    op.create_table(
        "posting_description_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("posting_id", sa.Integer(), nullable=False),
        sa.Column("version_seq", sa.Integer(), nullable=False),
        sa.Column("description_text", sa.Text(), nullable=True),
        sa.Column("description_hash", sa.String(64), nullable=True),
        sa.Column("first_seen_snapshot_date", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("posting_id", "version_seq", name="uq_posting_desc_versions_posting_seq"),
    )

    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("collection_date", sa.Date(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("companies_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("postings_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_postings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disappeared_postings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "collection_date", name="uq_collection_runs_source_date"),
    )

    op.create_table(
        "collection_run_companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("company_token", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("postings_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["collection_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "source", "company_token", name="uq_collection_run_companies_run_source_token"
        ),
        sa.CheckConstraint(
            "status IN ('ok','http_error','timeout','empty')", name="ck_collection_run_companies_status"
        ),
    )

    op.create_table(
        "monitored_companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("company_token", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("monitoring_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("monitoring_stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "company_token", name="uq_monitored_companies_source_token"),
    )

    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("posting_id", sa.Integer(), nullable=True),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["posting_id"], ["postings.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "outcome IN ('pending','no_response','rejected','oa','interview','offer','withdrawn')",
            name="ck_applications_outcome",
        ),
    )


def downgrade() -> None:
    op.drop_table("applications")
    op.drop_table("monitored_companies")
    op.drop_table("collection_run_companies")
    op.drop_table("collection_runs")
    op.drop_table("posting_description_versions")
    op.drop_index("ix_posting_snapshots_date", table_name="posting_snapshots")
    op.drop_table("posting_snapshots")
    op.drop_index("ix_postings_active_last_seen", table_name="postings")
    op.drop_column("postings", "absence_episode_count")
    op.drop_column("postings", "left_truncated")
    op.drop_column("postings", "raw_hash")
    op.drop_column("postings", "description_hash")
    op.drop_column("postings", "disappeared_at")
    op.drop_column("postings", "last_seen_at")
    op.drop_column("postings", "first_seen_at")
    op.drop_column("postings", "company_token")
    op.drop_column("companies", "ashby_board_name")

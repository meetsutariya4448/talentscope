from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Numeric, DateTime, Date, Boolean, ForeignKey,
    UniqueConstraint, Index, CheckConstraint, Computed
)
import json
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector
from app.database import Base


class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255), unique=True, nullable=False)
    greenhouse_token = Column(String(255))
    lever_slug = Column(String(255))
    ashby_board_name = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)

    postings = relationship("Posting", back_populates="company")


class Posting(Base):
    __tablename__ = "postings"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    title = Column(String(512), nullable=False)
    location = Column(String(255))
    description = Column(Text)
    salary_min = Column(Numeric(12, 2))
    salary_max = Column(Numeric(12, 2))
    currency = Column(String(10), default="USD")
    source = Column(String(50), nullable=False)   # greenhouse | lever | adzuna
    source_id = Column(String(512), nullable=False)
    url = Column(Text)
    posted_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Postgres GENERATED column (see migration 0005) — Computed() tells the
    # ORM this is server-maintained so it's excluded from INSERT/UPDATE
    # statements (and fetched back via RETURNING) instead of writing NULL
    # into it, which Postgres rejects for a generated column.
    search_vector = Column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title,'') || ' ' || "
            "coalesce(description,'') || ' ' || coalesce(location,''))",
            persisted=True,
        ),
    )
    embedding = Column(Vector(384))
    cluster_id = Column(Integer, nullable=True)

    # Posting-panel fields (survival analysis)
    company_token = Column(String(255), nullable=True)  # board token/slug this posting was fetched under
    first_seen_at = Column(DateTime(timezone=True), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    disappeared_at = Column(DateTime(timezone=True), nullable=True)
    description_hash = Column(String(64), nullable=True)  # hash of normalized description; drives versioning
    raw_hash = Column(String(64), nullable=True)           # hash of raw payload; diagnostic only
    left_truncated = Column(Boolean, nullable=False, default=False)
    absence_episode_count = Column(Integer, nullable=False, default=0)

    company = relationship("Company", back_populates="postings")
    posting_skills = relationship("PostingSkill", back_populates="posting")

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_postings_source_source_id"),
        Index("ix_postings_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_postings_company_title_location", "company_id", "title", "location"),
        Index("ix_postings_posted_at", "posted_at"),
        Index("ix_postings_source", "source"),
        Index(
            "ix_postings_active_last_seen", "last_seen_at",
            postgresql_where=disappeared_at.is_(None),
        ),
    )


class SkillCluster(Base):
    __tablename__ = "skill_clusters"

    id         = Column(Integer, primary_key=True)
    cluster_id = Column(Integer, nullable=False)
    label      = Column(String(255))
    size       = Column(Integer)
    top_skills = Column(Text)       # JSON-encoded list[str]
    silhouette = Column(Numeric(8, 6))
    run_at     = Column(DateTime, nullable=False)

    @property
    def top_skills_list(self) -> list[str]:
        return json.loads(self.top_skills or "[]")


class Skill(Base):
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    category = Column(String(100))

    posting_skills = relationship("PostingSkill", back_populates="skill")


class PostingSkill(Base):
    __tablename__ = "posting_skills"

    posting_id = Column(Integer, ForeignKey("postings.id", ondelete="CASCADE"), primary_key=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True)

    posting = relationship("Posting", back_populates="posting_skills")
    skill = relationship("Skill", back_populates="posting_skills")


class PostingSnapshot(Base):
    """One row per (posting, day) the posting was observed present — the event
    history a Kaplan-Meier/Cox survival model reads directly."""
    __tablename__ = "posting_snapshots"

    id = Column(Integer, primary_key=True)
    posting_id = Column(Integer, ForeignKey("postings.id", ondelete="CASCADE"), nullable=False)
    snapshot_date = Column(Date, nullable=False)
    description_hash = Column(String(64), nullable=True)
    captured_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("posting_id", "snapshot_date", name="uq_posting_snapshots_posting_date"),
        Index("ix_posting_snapshots_date", "snapshot_date"),
    )


class PostingDescriptionVersion(Base):
    """Full description text, written only when description_hash changes —
    diffable history for requirement-drift analysis without duplicating text
    across every daily snapshot row."""
    __tablename__ = "posting_description_versions"

    id = Column(Integer, primary_key=True)
    posting_id = Column(Integer, ForeignKey("postings.id", ondelete="CASCADE"), nullable=False)
    version_seq = Column(Integer, nullable=False)
    description_text = Column(Text, nullable=True)
    description_hash = Column(String(64), nullable=True)
    first_seen_snapshot_date = Column(Date, nullable=False)

    __table_args__ = (
        UniqueConstraint("posting_id", "version_seq", name="uq_posting_desc_versions_posting_seq"),
    )


class CollectionRun(Base):
    """Parent rollup: one row per authoritative source per day, aggregated from
    CollectionRunCompany."""
    __tablename__ = "collection_runs"

    id = Column(Integer, primary_key=True)
    source = Column(String(50), nullable=False)
    collection_date = Column(Date, nullable=False)
    run_at = Column(DateTime(timezone=True), nullable=False)
    companies_checked = Column(Integer, nullable=False, default=0)
    postings_seen = Column(Integer, nullable=False, default=0)
    new_postings = Column(Integer, nullable=False, default=0)
    disappeared_postings = Column(Integer, nullable=False, default=0)
    errors_count = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=True)  # ok | degraded | failed

    __table_args__ = (
        UniqueConstraint("source", "collection_date", name="uq_collection_runs_source_date"),
    )


class CollectionRunCompany(Base):
    """Per-company health record for one day's collection run. This is what makes
    censoring correct: distinguishes 'req closed' from 'we failed to check that
    company', and is the only way to catch one board token going stale while the
    rest keep working."""
    __tablename__ = "collection_run_companies"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("collection_runs.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)
    company_token = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False)  # ok | http_error | timeout | empty
    http_status = Column(Integer, nullable=True)
    postings_seen = Column(Integer, nullable=False, default=0)
    error_detail = Column(Text, nullable=True)
    checked_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("run_id", "source", "company_token", name="uq_collection_run_companies_run_source_token"),
        CheckConstraint("status IN ('ok','http_error','timeout','empty')", name="ck_collection_run_companies_status"),
    )


class MonitoredCompany(Base):
    """Registry of companies under active daily observation, synced from
    config/target_companies.yml. Provides the monitoring_started_at timestamp
    that postings.left_truncated is computed against — without it, a company
    added mid-project looks like it only just started posting."""
    __tablename__ = "monitored_companies"

    id = Column(Integer, primary_key=True)
    source = Column(String(50), nullable=False)
    company_token = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=True)
    monitoring_started_at = Column(DateTime(timezone=True), nullable=False)
    monitoring_stopped_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("source", "company_token", name="uq_monitored_companies_source_token"),
    )


class TaskExecution(Base):
    """Explicit Celery task lifecycle state for coarse-grained tasks (fetch,
    dispatch, clustering, rollup), independent of Celery's Redis result
    backend which is ephemeral (TTL'd) and only queryable by task_id. Gives a
    durable, queryable history of what ran, when, and how many times it
    retried — see app.tasks.monitoring for the signal handlers that populate
    it and the _TRACK_STATE_FOR allowlist of which tasks are tracked."""
    __tablename__ = "task_executions"

    id = Column(Integer, primary_key=True)
    task_id = Column(String(155), nullable=False)
    task_name = Column(String(255), nullable=False)
    state = Column(String(20), nullable=False)  # STARTED | SUCCESS | FAILURE | RETRY
    worker_hostname = Column(String(255), nullable=True)
    args = Column(Text, nullable=True)  # JSON-encoded
    retries = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("task_id", name="uq_task_executions_task_id"),
        Index("ix_task_executions_task_name_state", "task_name", "state"),
        Index("ix_task_executions_started_at", "started_at"),
    )


class FailedTask(Base):
    """Dead-letter store: one row per task that exhausted max_retries or hit a
    non-retryable exception. This is what turns a 'poison message' from an
    infinite retry loop into a terminated, triageable record — Celery marks
    the task FAILURE and stops, and app.tasks.monitoring writes the failure
    here before the Redis result backend's TTL erases all trace of it."""
    __tablename__ = "failed_tasks"

    id = Column(Integer, primary_key=True)
    task_id = Column(String(155), nullable=False)
    task_name = Column(String(255), nullable=False)
    args = Column(Text, nullable=True)
    kwargs = Column(Text, nullable=True)
    exception = Column(Text, nullable=False)
    traceback = Column(Text, nullable=True)
    retries = Column(Integer, nullable=False, default=0)
    failed_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_failed_tasks_task_name", "task_name"),
        Index("ix_failed_tasks_failed_at", "failed_at"),
    )


class Application(Base):
    """The user's own application log, with outcomes modeled as censored data —
    most applications are ghosted, not resolved, and that's a censored
    observation at last_checked_at, not a missing value."""
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True)
    posting_id = Column(Integer, ForeignKey("postings.id"), nullable=True)
    company_name = Column(String(255), nullable=False)
    title = Column(String(512), nullable=False)
    applied_at = Column(DateTime(timezone=True), nullable=False)
    first_response_at = Column(DateTime(timezone=True), nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    outcome = Column(String(20), nullable=False, default="pending")
    outcome_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('pending','no_response','rejected','oa','interview','offer','withdrawn')",
            name="ck_applications_outcome",
        ),
    )

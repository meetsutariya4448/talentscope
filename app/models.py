from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Numeric, DateTime, ForeignKey,
    UniqueConstraint, Index
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
    search_vector = Column(TSVECTOR)
    embedding = Column(Vector(384))
    cluster_id = Column(Integer, nullable=True)

    company = relationship("Company", back_populates="postings")
    posting_skills = relationship("PostingSkill", back_populates="posting")

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_postings_source_source_id"),
        Index("ix_postings_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_postings_company_title_location", "company_id", "title", "location"),
        Index("ix_postings_posted_at", "posted_at"),
        Index("ix_postings_source", "source"),
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

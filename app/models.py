from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

DEFAULT_REMOTE_REGIONS = ["US", "USA", "United States", "North America", "Americas", "Anywhere", "Worldwide", "Global"]
DEFAULT_TITLE_TERMS = ["engineer", "developer", "programmer"]


class Profile(Base):
    """Single-row table holding the resume and search preferences (id is always 1)."""

    __tablename__ = "profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    resume_text: Mapped[str] = mapped_column(Text, default="")
    target_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    title_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    title_terms: Mapped[list[str]] = mapped_column(JSON, default=lambda: list(DEFAULT_TITLE_TERMS))
    locations: Mapped[list[str]] = mapped_column(JSON, default=list)
    remote_ok: Mapped[bool] = mapped_column(default=True)
    remote_regions: Mapped[list[str]] = mapped_column(JSON, default=lambda: list(DEFAULT_REMOTE_REGIONS))
    excluded_stacks: Mapped[list[str]] = mapped_column(JSON, default=list)
    salary_floor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_age_days: Mapped[int] = mapped_column(Integer, default=30)
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Source(Base):
    """A job source that can be switched on or off as a whole (e.g. "greenhouse")."""

    __tablename__ = "sources"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Company(Base):
    """A company board polled through its ATS; `ats` is a Source key."""

    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("ats", "slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    ats: Mapped[str] = mapped_column(String(40), ForeignKey("sources.key"))
    slug: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_job_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    jobs: Mapped[list["Job"]] = relationship(back_populates="company", passive_deletes=True)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # sha1 of normalized (company, title, location): the same posting seen twice is one row.
    dedupe_key: Mapped[str] = mapped_column(String(40), unique=True)
    source: Mapped[str] = mapped_column(String(40))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=True)
    company_name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(500))
    location: Mapped[str] = mapped_column(String(500), default="")
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # False once the posting disappears from a successful fetch of its board.
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # None means the job passed the hard filters; otherwise the first rule it failed.
    filter_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Fit against the resume, 0-100; None until scored.
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    red_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Set when the user dismisses the job; the reason feeds back into scoring.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismiss_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    company: Mapped[Company | None] = relationship(back_populates="jobs")


APPLICATION_STATUSES = ["Preparing", "Applied", "Screening", "Interviewing", "Offer", "Rejected", "Withdrawn", "No response"]


class Application(Base):
    """A job the user is applying or has applied to, with their cover letter and status."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable so applications survive removal of the company (which cascades its jobs),
    # and so jobs found elsewhere can be tracked too. The fields below are a snapshot.
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    company_name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="Preparing")
    applied_on: Mapped[date | None] = mapped_column(Date, nullable=True, default=date.today)
    cover_letter: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    job: Mapped[Job | None] = relationship()

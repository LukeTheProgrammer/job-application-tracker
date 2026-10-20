"""Cheap rule-based filters driven by the Profile. A job's filter_reason is None when it passes."""

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, Profile


def term_pattern(term: str) -> re.Pattern:
    # Word-ish boundaries that still work for terms like ".NET" and "C#".
    return re.compile(rf"(?<![\w.#+]){re.escape(term)}(?![\w#+])", re.I)


def contains_any(text: str, terms: list[str]) -> str | None:
    return next((term for term in terms if term_pattern(term).search(text)), None)


def location_matches(job: Job, profile: Profile) -> bool:
    if job.is_remote and profile.remote_ok:
        # A remote posting tied to a country/region outside the allowed list doesn't count.
        rest = re.sub(r"\b(remote|fully remote|100% remote)\b", "", job.location, flags=re.I)
        if not re.search(r"\w", rest) or contains_any(job.location, profile.remote_regions):
            return True
    cities = [loc.split(",")[0].strip() for loc in profile.locations if loc.strip().lower() != "remote"]
    return contains_any(job.location, [city for city in cities if city]) is not None


def filter_reason(job: Job, profile: Profile, now: datetime) -> str | None:
    if profile.title_keywords and not contains_any(job.title, profile.title_keywords):
        return "title: no seniority keyword"
    if profile.title_terms and not contains_any(job.title, profile.title_terms):
        return "title: not an engineering role"
    if excluded := contains_any(job.title, profile.excluded_stacks):
        return f"title: excluded stack {excluded}"
    if not location_matches(job, profile):
        return "location"
    if profile.salary_floor and job.salary_max and job.salary_max < profile.salary_floor:
        return f"salary: max {job.salary_max:,} below floor"
    if job.posted_at and job.posted_at < now - timedelta(days=profile.max_age_days):
        return f"age: posted over {profile.max_age_days} days ago"
    return None


def apply_filters(session: Session, profile: Profile) -> None:
    """Re-evaluate every active job; called after each fetch and whenever the profile changes."""
    now = datetime.now(UTC)
    for job in session.scalars(select(Job).where(Job.active)):
        job.filter_reason = filter_reason(job, profile, now)
    session.commit()

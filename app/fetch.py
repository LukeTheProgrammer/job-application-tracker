"""Fetch every enabled company board, upsert postings, then re-run the filters.

Run from cron/systemd with `python -m app.fetch`, or from the Sources page.
"""

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime

import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal, engine
from app.filters import apply_filters
from app.models import Company, Job, Source
from app.profile import get_profile
from app.seed import seed
from app.sources import ATS_FETCHERS, FetchedJob

log = logging.getLogger("jobs.fetch")

# Arbitrary constant: the Postgres advisory lock that keeps fetch runs from overlapping.
FETCH_LOCK_ID = 7_404_001
CONCURRENCY = 10


def dedupe_key(company: str, title: str, location: str) -> str:
    normalized = "|".join(re.sub(r"\s+", " ", part).strip().lower() for part in (company, title, location))
    return hashlib.sha1(normalized.encode()).hexdigest()


async def fetch_boards(companies: list[Company]) -> dict[int, list[FetchedJob] | Exception]:
    semaphore = asyncio.Semaphore(CONCURRENCY)
    headers = {"User-Agent": "jobs-finder/0.1 (personal job search)"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:

        async def one(company: Company) -> list[FetchedJob] | Exception:
            async with semaphore:
                try:
                    return await ATS_FETCHERS[company.ats](client, company.slug)
                except Exception as exc:  # one broken board must not stop the run
                    return exc

        results = await asyncio.gather(*(one(c) for c in companies))
    return {company.id: result for company, result in zip(companies, results)}


def store(session: Session, company: Company, fetched: list[FetchedJob], now: datetime) -> None:
    by_key = {dedupe_key(company.name, job.title, job.location): job for job in fetched}
    existing = {job.dedupe_key: job for job in session.scalars(select(Job).where(Job.dedupe_key.in_(by_key)))}

    for key, job in by_key.items():
        row = existing.get(key)
        if row is None:
            row = Job(dedupe_key=key, source=company.ats, company_name=company.name, first_seen_at=now)
            session.add(row)
        row.company_id = company.id
        row.title = job.title[:500]
        row.location = job.location[:500]
        row.is_remote = job.is_remote
        row.url = job.url
        row.description = job.description
        row.salary_min, row.salary_max, row.salary_currency = job.salary_min, job.salary_max, job.salary_currency
        row.posted_at = job.posted_at or row.posted_at
        row.last_seen_at = now
        row.active = True

    # Postings no longer on the board have been filled or pulled.
    for stale in session.scalars(
        select(Job).where(Job.company_id == company.id, Job.active, Job.dedupe_key.not_in(by_key))
    ):
        stale.active = False

    company.last_fetched_at = now
    company.last_job_count = len(fetched)
    company.last_error = None


def run_fetch() -> bool:
    """Returns False without doing anything if another fetch is already running."""
    with engine.connect() as lock_conn:
        if not lock_conn.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": FETCH_LOCK_ID}):
            log.info("fetch already running; skipping")
            return False
        try:
            with SessionLocal() as session:
                companies = list(
                    session.scalars(
                        select(Company).join(Source, Source.key == Company.ats).where(Company.enabled, Source.enabled)
                    )
                )
                results = asyncio.run(fetch_boards(companies))
                now = datetime.now(UTC)
                for company in companies:
                    result = results[company.id]
                    if isinstance(result, Exception):
                        company.last_error = f"{type(result).__name__}: {result}"[:1000]
                        log.warning("%s (%s/%s): %s", company.name, company.ats, company.slug, company.last_error)
                    else:
                        store(session, company, result, now)
                session.commit()
                apply_filters(session, get_profile(session))
                log.info("fetched %d boards", len(companies))
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": FETCH_LOCK_ID})
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    seed()
    run_fetch()

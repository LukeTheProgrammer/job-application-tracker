"""Jobs found outside the fetch pipeline: build a Job from a posting URL so an application can link to it."""

import hashlib
import html
import logging
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fetch import dedupe_key
from app.models import Application, Job
from app.sources.ats import FetchedJob, html_to_text
from app.sources.boards import BROWSER_HEADERS, jsonld_job, jsonld_posting

log = logging.getLogger("jobs.external")

SOURCE = "external"


def meta(page: str, name: str) -> str:
    match = re.search(rf'<meta[^>]+(?:property|name)="{name}"[^>]+content="([^"]*)"', page, re.I)
    return html.unescape(match.group(1)).strip() if match else ""


def fetch_posting(url: str) -> FetchedJob | None:
    """Read the posting from its page's JSON-LD, falling back to its title and meta description."""
    try:
        response = httpx.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=20)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Couldn't fetch %s: %s", url, exc)
        return None
    page = response.text
    external_id = hashlib.sha1(url.encode()).hexdigest()[:16]
    posting = jsonld_posting(page)
    if posting:
        try:
            return jsonld_job(posting, url, external_id)
        except (ValueError, TypeError, AttributeError) as exc:
            log.warning("Unreadable JobPosting on %s: %s", url, exc)
    # Pages built with JavaScript often carry nothing more than this; the cover letter skill
    # falls back to fetching the page itself when the description is a stub.
    title = meta(page, "og:title")
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        title = html_to_text(match.group(1)) if match else ""
    description = meta(page, "og:description") or meta(page, "description")
    return FetchedJob(external_id=external_id, title=title, location="", is_remote=False, url=url, description=description)


def link_job(session: Session, application: Application) -> None:
    """Give an application with a URL but no job a Job built from the posting page.

    Blank company and title fields on the application are filled in from the posting.
    """
    url = application.url
    if application.job_id is not None or not url.startswith(("http://", "https://")):
        return
    taken = select(Application.job_id).where(Application.job_id.is_not(None))
    job = session.scalar(select(Job).where(Job.url == url, Job.id.not_in(taken)))
    if job is None:
        fetched = fetch_posting(url)
        if fetched is None:
            return
        company = application.company_name or (fetched.company if fetched.company != "Unknown" else "") or "Unknown"
        title = application.title or fetched.title or "Untitled"
        key = dedupe_key(company, title, fetched.location)
        job = session.scalar(select(Job).where(Job.dedupe_key == key))
        if job is None:
            # Never marked stale by a fetch, since it belongs to no company board.
            job = Job(
                dedupe_key=key,
                source=SOURCE,
                company_name=company[:200],
                title=title[:500],
                location=fetched.location[:500],
                is_remote=fetched.is_remote,
                url=url,
                description=fetched.description,
                salary_min=fetched.salary_min,
                salary_max=fetched.salary_max,
                salary_currency=fetched.salary_currency,
                posted_at=fetched.posted_at,
            )
            session.add(job)
        elif session.scalar(select(Application.id).where(Application.job_id == job.id)) is not None:
            return
    application.job = job
    application.company_name = application.company_name or job.company_name
    application.title = application.title or job.title

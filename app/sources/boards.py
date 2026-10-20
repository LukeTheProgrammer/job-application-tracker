"""Fetchers for multi-company job boards.

They plug into the same pipeline as the ATS boards: each saved search is a Company row whose
`ats` is the board key and whose `slug` is the search (a category, query or listing path). The
postings carry their own company name in FetchedJob.company.
"""

import asyncio
import json
import re
from datetime import UTC, datetime

import httpx

from app.sources.ats import FetchedJob, gather_limited, html_to_text, mentions_remote, parse_iso, request

# Postings older than this are skipped while paging, since the age filter would drop them anyway.
MAX_AGE_DAYS = 30
BUILTIN_MAX_PAGES = 5
# Built In rate-limits bursts, so its detail pages are fetched one at a time with a pause.
BUILTIN_DELAY_SECONDS = 1
HIMALAYAS_MAX_PAGES = 10
# Built In serves HTML only and turns away obvious bots.
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) jobs-finder/0.1"}


def too_old(posted_at: datetime | None) -> bool:
    return posted_at is not None and (datetime.now(UTC) - posted_at).days > MAX_AGE_DAYS


async def fetch_workingnomads(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    """slug: a category name such as "Development", or "all"."""
    response = await client.get("https://www.workingnomads.com/api/exposed_jobs/")
    response.raise_for_status()
    jobs = []
    for job in response.json():
        if slug.lower() != "all" and (job.get("category_name") or "").lower() != slug.lower():
            continue
        location = job.get("location") or ""
        jobs.append(
            FetchedJob(
                external_id=job["url"],
                title=job["title"],
                # Every Working Nomads posting is remote; location holds the allowed regions.
                location=f"Remote ({location})" if location else "Remote",
                is_remote=True,
                url=job["url"],
                description=html_to_text(job.get("description") or ""),
                posted_at=parse_iso(job.get("pub_date")),
                company=job["company_name"],
            )
        )
    return jobs


async def fetch_jobicy(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    """slug: a Jobicy industry such as "engineering"."""
    response = await client.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "industry": slug})
    response.raise_for_status()
    jobs = []
    for job in response.json().get("jobs") or []:
        geo = job.get("jobGeo") or ""
        annual = job.get("salaryPeriod") == "yearly"
        jobs.append(
            FetchedJob(
                external_id=str(job["id"]),
                title=html_to_text(job["jobTitle"]),
                location=f"Remote ({geo})" if geo else "Remote",
                is_remote=True,
                url=job["url"],
                description=html_to_text(job.get("jobDescription") or ""),
                salary_min=job.get("salaryMin") if annual else None,
                salary_max=job.get("salaryMax") if annual else None,
                salary_currency=job.get("salaryCurrency") if annual else None,
                posted_at=parse_iso(job.get("pubDate")),
                company=html_to_text(job["companyName"]),
            )
        )
    return jobs


async def fetch_himalayas(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    """slug: a search query such as "software engineer". Results are newest first."""
    jobs = []
    for page in range(1, HIMALAYAS_MAX_PAGES + 1):
        response = await request(
            client, "GET", "https://himalayas.app/jobs/api/search", params={"q": slug, "sort": "recent", "page": page}
        )
        batch = response.json().get("jobs") or []
        for job in batch:
            posted_at = datetime.fromtimestamp(job["pubDate"], UTC) if job.get("pubDate") else None
            regions = ", ".join(job.get("locationRestrictions") or [])
            annual = job.get("salaryPeriod") == "annual"
            jobs.append(
                FetchedJob(
                    external_id=job["guid"],
                    title=html_to_text(job["title"]),
                    location=f"Remote ({regions})" if regions else "Remote",
                    is_remote=True,
                    url=job["guid"],
                    description=html_to_text(job.get("description") or ""),
                    salary_min=job.get("minSalary") if annual else None,
                    salary_max=job.get("maxSalary") if annual else None,
                    salary_currency=job.get("currency") if annual else None,
                    posted_at=posted_at,
                    company=job["companyName"],
                )
            )
        if not batch or too_old(jobs[-1].posted_at):
            break
    return jobs


def builtin_posting(page: str) -> dict:
    for script in re.findall(r'<script type="application/ld(?:\+|&#x2B;)json">(.*?)</script>', page, re.S):
        data = json.loads(script)
        for node in data.get("@graph", [data]) if isinstance(data, dict) else data:
            if isinstance(node, dict) and node.get("@type") == "JobPosting":
                return node
    return {}


async def fetch_builtin(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    """slug: a listing path from builtin.com, e.g. "jobs/remote/dev-engineering"."""
    base = "https://builtin.com/" + re.sub(r"^(https?://)?(www\.)?builtin\.com/", "", slug).strip("/")
    paths: dict[str, None] = {}
    for page in range(1, BUILTIN_MAX_PAGES + 1):
        response = await request(client, "GET", base, params={"page": page}, headers=BROWSER_HEADERS)
        found = re.findall(r'href="(/job/[^"]+/\d+)"', response.text)
        if not found:
            break
        paths.update(dict.fromkeys(found))

    async def detail(path: str) -> FetchedJob | None:
        await asyncio.sleep(BUILTIN_DELAY_SECONDS)
        response = await request(client, "GET", f"https://builtin.com{path}", headers=BROWSER_HEADERS)
        posting = builtin_posting(response.text)
        if not posting:
            return None
        address = (posting.get("jobLocation") or {})
        address = (address[0] if isinstance(address, list) and address else address).get("address") or {}
        location = ", ".join(
            p for p in (address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")) if p
        )
        remote = posting.get("jobLocationType") == "TELECOMMUTE"
        if remote:
            location = f"Remote ({location})" if location else "Remote"
        salary = posting.get("baseSalary") or {}
        value = salary.get("value") or {}
        annual = value.get("unitText") == "YEAR"
        return FetchedJob(
            external_id=path.rsplit("/", 1)[-1],
            title=html_to_text(posting.get("title") or ""),
            location=location,
            is_remote=remote or mentions_remote(location),
            url=f"https://builtin.com{path}",
            description=html_to_text(posting.get("description") or ""),
            salary_min=int(value["minValue"]) if annual and value.get("minValue") else None,
            salary_max=int(value["maxValue"]) if annual and value.get("maxValue") else None,
            salary_currency=salary.get("currency") if annual else None,
            posted_at=parse_iso(posting.get("datePosted")),
            company=(posting.get("hiringOrganization") or {}).get("name") or "Unknown",
        )

    return [job for job in await gather_limited((detail(path) for path in paths), limit=1) if job]


BOARD_FETCHERS = {
    "workingnomads": fetch_workingnomads,
    "jobicy": fetch_jobicy,
    "himalayas": fetch_himalayas,
    "builtin": fetch_builtin,
}

BOARD_NAMES = {
    "workingnomads": "Working Nomads",
    "jobicy": "Jobicy",
    "himalayas": "Himalayas",
    "builtin": "Built In",
}

# The saved search each board starts with; more can be added on the Companies page.
BOARD_DEFAULTS = {
    "workingnomads": ("Working Nomads: Development", "Development"),
    "jobicy": ("Jobicy: Engineering", "engineering"),
    "himalayas": ("Himalayas: software engineer", "software engineer"),
    "builtin": ("Built In: remote dev & engineering", "jobs/remote/dev-engineering"),
}

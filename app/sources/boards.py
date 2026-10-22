"""Fetchers for multi-company job boards.

They plug into the same pipeline as the ATS boards: each saved search is a Company row whose
`ats` is the board key and whose `slug` is the search (a category, query or listing path). The
postings carry their own company name in FetchedJob.company.
"""

import asyncio
import html
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from app.sources.ats import (
    BROWSER_HEADERS,
    FetchedJob,
    gather_limited,
    html_to_text,
    jsonld_details,
    jsonld_job,
    jsonld_posting,
    mentions_remote,
    meta,
    parse_iso,
    request,
)

# Postings older than this are skipped while paging, since the age filter would drop them anyway.
MAX_AGE_DAYS = 30
BUILTIN_MAX_PAGES = 5
# Built In rate-limits bursts, so its detail pages are fetched one at a time with a pause.
BUILTIN_DELAY_SECONDS = 1
HIMALAYAS_MAX_PAGES = 10


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
        try:
            response = await request(client, "GET", f"https://builtin.com{path}", headers=BROWSER_HEADERS)
        except httpx.HTTPStatusError as exc:
            # A posting taken down between the listing and its detail request; skip just that one.
            if exc.response.status_code in (404, 410):
                return None
            raise
        posting = jsonld_posting(response.text)
        if not posting:
            return None
        return jsonld_job(posting, f"https://builtin.com{path}", path.rsplit("/", 1)[-1])

    return [job for job in await gather_limited((detail(path) for path in paths), limit=1) if job]


async def fetch_remoteok(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    """slug: a tag such as "php" or "dev", or "all"."""
    response = await client.get("https://remoteok.com/api", headers=BROWSER_HEADERS)
    response.raise_for_status()
    jobs = []
    # The first element is the API's legal notice, not a job. The API ignores its tag parameter,
    # so the tag is matched here.
    for job in response.json():
        if "id" not in job or (slug.lower() != "all" and slug.lower() not in (job.get("tags") or [])):
            continue
        location = job.get("location") or ""
        jobs.append(
            FetchedJob(
                external_id=str(job["id"]),
                title=html_to_text(job["position"]),
                location=f"Remote ({location})" if location else "Remote",
                is_remote=True,
                url=job["url"],
                description=html_to_text(job.get("description") or ""),
                # RemoteOK reports 0 for an unlisted salary.
                salary_min=job.get("salary_min") or None,
                salary_max=job.get("salary_max") or None,
                salary_currency="USD" if job.get("salary_min") else None,
                posted_at=parse_iso(job.get("date")),
                company=html_to_text(job["company"]),
            )
        )
    return jobs


async def fetch_remotive(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    """slug: a Remotive category such as "software-dev"."""
    response = await client.get("https://remotive.com/api/remote-jobs", params={"category": slug})
    response.raise_for_status()
    jobs = []
    for job in response.json().get("jobs") or []:
        # Remotive's salary is free text ("$120k - $150k", "$1000 per task"), so it isn't parsed.
        region = job.get("candidate_required_location") or ""
        jobs.append(
            FetchedJob(
                external_id=str(job["id"]),
                title=job["title"],
                location=f"Remote ({region})" if region else "Remote",
                is_remote=True,
                url=job["url"],
                description=html_to_text(job.get("description") or ""),
                posted_at=parse_iso(job.get("publication_date")),
                company=job["company_name"],
            )
        )
    return jobs


async def fetch_weworkremotely(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    """slug: a category feed name such as "remote-full-stack-programming-jobs"."""
    response = await client.get(f"https://weworkremotely.com/categories/{slug}.rss", headers=BROWSER_HEADERS)
    response.raise_for_status()
    jobs = []
    for item in ET.fromstring(response.content).iter("item"):
        # Titles read "Company: Job title".
        company, _, title = (item.findtext("title") or "").partition(": ")
        region = item.findtext("region") or ""
        pub = item.findtext("pubDate")
        url = item.findtext("link") or item.findtext("guid") or ""
        jobs.append(
            FetchedJob(
                external_id=url,
                title=title or company,
                location=f"Remote ({region})" if region else "Remote",
                is_remote=True,
                url=url,
                description=html_to_text(item.findtext("description") or ""),
                posted_at=parsedate_to_datetime(pub) if pub else None,
                company=company if title else "Unknown",
            )
        )
    return jobs


AGE_UNITS = {"h": 1 / 24, "d": 1, "w": 7, "mo": 30, "y": 365}


def parse_age(text: str) -> datetime | None:
    """Turn a relative age such as "3d" or "2w" into a posting date."""
    match = re.fullmatch(r"(\d+)\s*(mo|[hdwy])", text.strip())
    if not match:
        return None
    return datetime.now(UTC) - timedelta(days=int(match.group(1)) * AGE_UNITS[match.group(2)])


async def fetch_larajobs(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    """slug: "all" for the front page, or a listing path such as "vuejs-jobs".

    The API and RSS feed list only a few postings, so this reads the listing page's cards, then each
    employer's posting page (where the apply link leads) for a description.
    """
    path = "" if slug.lower() == "all" else slug.strip("/")
    response = await request(client, "GET", f"https://larajobs.com/{path}", headers=BROWSER_HEADERS)
    cards = re.findall(r'<a data-url="([^"]*)" href="/job/(\d+)"[^>]*>(.*?)</a>', response.text, re.S)

    async def card_job(apply_url: str, job_id: str, card: str) -> FetchedJob:
        card = re.sub(r"<svg.*?</svg>", "", card, flags=re.S)
        # Card text in order: company, title, job type, "- salary" (optional), location, age, tags.
        parts = [html_to_text(p) for p in re.split(r"<[^>]+>", card)]
        parts = [p for p in parts if p]
        company, title, rest = parts[0], parts[1], parts[3:]
        salary = rest.pop(0).lstrip("- ") if rest and rest[0].startswith("-") else ""
        location = rest[0] if rest else ""
        posted_at = parse_age(rest[1]) if len(rest) > 1 else None
        apply_url = html.unescape(re.sub(r"[?&]utm_source=Larajobs", "", apply_url))
        try:
            page = (await request(client, "GET", apply_url, headers=BROWSER_HEADERS)).text
        except httpx.HTTPError:
            page = ""
        posting = jsonld_posting(page)
        if posting:
            description = html_to_text(posting.get("description") or "")
        else:
            description = meta(page, "og:description") or meta(page, "description")
        if salary:
            description = f"Salary: {salary}\n\n{description}"
        return FetchedJob(
            external_id=job_id,
            title=title,
            location=location,
            is_remote=mentions_remote(location),
            url=apply_url,
            description=description,
            posted_at=posted_at,
            company=company,
        )

    return await gather_limited(card_job(*card) for card in cards)


async def fetch_vuejobs(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    """slug: "remote" for remote postings only, or "all".

    The feed lists over a thousand postings with truncated descriptions, so only recent ones that
    pass the slug's location check get a detail request for the full JSON-LD posting.
    """
    response = await client.get("https://vuejobs.com/feed", headers=BROWSER_HEADERS)
    response.raise_for_status()
    urls: dict[str, str] = {}
    for item in ET.fromstring(response.content).iter("item"):
        pub = item.findtext("pubDate")
        if pub and too_old(parsedate_to_datetime(pub)):
            continue
        match = re.search(r"Location:</strong>\s*(.*?)</p>", item.findtext("description") or "")
        if slug.lower() == "remote" and not (match and mentions_remote(match.group(1))):
            continue
        url = (item.findtext("guid") or "").strip()
        urls[url] = url
    return await jsonld_details(client, urls)


JOBSPY_SITES = ["indeed", "linkedin"]
JOBSPY_RESULTS = 30
# Covers several runs of the 3-hourly timer, so a run that gets blocked doesn't lose postings.
JOBSPY_HOURS_OLD = 72


def jobspy_value(row: dict, key: str):
    """JobSpy rows are pandas records, where a missing value is NaN."""
    value = row.get(key)
    return None if value is None or value != value else value


async def fetch_jobspy(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    """slug: a search query such as "senior laravel developer", run on Indeed and LinkedIn for US remote jobs.

    LinkedIn and Indeed block scrapers often. JobSpy logs those failures instead of raising, so an
    empty result is reported as an error rather than marking every earlier posting inactive.
    """
    from jobspy import scrape_jobs  # heavy import (pandas); only needed when this source runs

    frame = await asyncio.to_thread(
        scrape_jobs,
        site_name=JOBSPY_SITES,
        search_term=slug,
        location="United States",
        is_remote=True,
        results_wanted=JOBSPY_RESULTS,
        hours_old=JOBSPY_HOURS_OLD,
        country_indeed="USA",
        fetch_description=True,
    )
    if frame.empty:
        raise RuntimeError("JobSpy returned no postings (LinkedIn/Indeed may be blocking requests)")
    jobs = []
    for row in frame.to_dict("records"):
        location = jobspy_value(row, "location") or ""
        remote = bool(jobspy_value(row, "is_remote"))
        annual = jobspy_value(row, "interval") == "yearly"
        posted = jobspy_value(row, "date_posted")
        jobs.append(
            FetchedJob(
                external_id=row["id"],
                title=row["title"],
                location=f"Remote ({location})" if remote and not mentions_remote(location) else location,
                is_remote=remote or mentions_remote(location),
                url=jobspy_value(row, "job_url_direct") or row["job_url"],
                description=jobspy_value(row, "description") or "",
                salary_min=int(row["min_amount"]) if annual and jobspy_value(row, "min_amount") else None,
                salary_max=int(row["max_amount"]) if annual and jobspy_value(row, "max_amount") else None,
                salary_currency=jobspy_value(row, "currency") if annual else None,
                posted_at=datetime(posted.year, posted.month, posted.day, tzinfo=UTC) if posted else None,
                company=jobspy_value(row, "company") or "Unknown",
            )
        )
    return jobs


BOARD_FETCHERS = {
    "workingnomads": fetch_workingnomads,
    "jobicy": fetch_jobicy,
    "himalayas": fetch_himalayas,
    "builtin": fetch_builtin,
    "remoteok": fetch_remoteok,
    "remotive": fetch_remotive,
    "weworkremotely": fetch_weworkremotely,
    "larajobs": fetch_larajobs,
    "vuejobs": fetch_vuejobs,
    "jobspy": fetch_jobspy,
}

BOARD_NAMES = {
    "workingnomads": "Working Nomads",
    "jobicy": "Jobicy",
    "himalayas": "Himalayas",
    "builtin": "Built In",
    "remoteok": "RemoteOK",
    "remotive": "Remotive",
    "weworkremotely": "We Work Remotely",
    "larajobs": "LaraJobs",
    "vuejobs": "VueJobs",
    "jobspy": "JobSpy (LinkedIn + Indeed)",
}

# The saved search each board starts with; more can be added on the Companies page.
BOARD_DEFAULTS = {
    "workingnomads": ("Working Nomads: Development", "Development"),
    "jobicy": ("Jobicy: Engineering", "engineering"),
    "himalayas": ("Himalayas: software engineer", "software engineer"),
    "builtin": ("Built In: remote dev & engineering", "jobs/remote/dev-engineering"),
    "remoteok": ("RemoteOK: dev", "dev"),
    "remotive": ("Remotive: software development", "software-dev"),
    "weworkremotely": ("We Work Remotely: full-stack", "remote-full-stack-programming-jobs"),
    "larajobs": ("LaraJobs: all", "all"),
    "vuejobs": ("VueJobs: remote", "remote"),
    "jobspy": ("JobSpy: senior laravel developer", "senior laravel developer"),
}

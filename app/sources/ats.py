"""Fetchers for the public, unauthenticated job-board APIs of the common ATS vendors.

Each fetcher takes a board slug and returns FetchedJob records. They raise on HTTP
errors; the caller records the error against the company and moves on.
"""

import html
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx


@dataclass
class FetchedJob:
    external_id: str
    title: str
    location: str
    is_remote: bool
    url: str
    description: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    posted_at: datetime | None = None


def html_to_text(raw: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def mentions_remote(text: str) -> bool:
    return bool(re.search(r"\bremote\b", text, re.I))


async def fetch_greenhouse(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    response = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    response.raise_for_status()
    jobs = []
    for job in response.json()["jobs"]:
        location = (job.get("location") or {}).get("name") or ""
        jobs.append(
            FetchedJob(
                external_id=str(job["id"]),
                title=job["title"],
                location=location,
                is_remote=mentions_remote(location),
                url=job["absolute_url"],
                description=html_to_text(html.unescape(job.get("content") or "")),
                posted_at=parse_iso(job.get("first_published") or job.get("updated_at")),
            )
        )
    return jobs


async def fetch_lever(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    response = await client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    response.raise_for_status()
    jobs = []
    for job in response.json():
        categories = job.get("categories") or {}
        location = categories.get("location") or ", ".join(categories.get("allLocations") or [])
        if job.get("country"):
            location = f"{location} ({job['country']})" if location else job["country"]
        salary = job.get("salaryRange") or {}
        annual = salary.get("interval") == "per-year-salary"
        jobs.append(
            FetchedJob(
                external_id=job["id"],
                title=job["text"],
                location=location,
                is_remote=job.get("workplaceType") == "remote" or mentions_remote(location),
                url=job["hostedUrl"],
                description="\n\n".join(
                    part for part in (job.get("descriptionPlain"), job.get("additionalPlain")) if part
                ),
                salary_min=salary.get("min") if annual else None,
                salary_max=salary.get("max") if annual else None,
                salary_currency=salary.get("currency") if annual else None,
                posted_at=datetime.fromtimestamp(job["createdAt"] / 1000, UTC) if job.get("createdAt") else None,
            )
        )
    return jobs


async def fetch_ashby(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    response = await client.get(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"}
    )
    response.raise_for_status()
    jobs = []
    for job in response.json()["jobs"]:
        if not job.get("isListed", True):
            continue
        locations = [job.get("location") or ""] + [s.get("location", "") for s in job.get("secondaryLocations") or []]
        location = "; ".join(loc for loc in locations if loc)
        country = ((job.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
        if country and country not in location:
            location = f"{location} ({country})" if location else country
        workplace = job.get("workplaceType")
        salary = next(
            (
                c
                for c in (job.get("compensation") or {}).get("summaryComponents") or []
                if c.get("compensationType") == "Salary" and c.get("interval") == "1 YEAR"
            ),
            {},
        )
        jobs.append(
            FetchedJob(
                external_id=job["id"],
                title=job["title"],
                location=location,
                is_remote=workplace == "Remote" if workplace else bool(job.get("isRemote")),
                url=job["jobUrl"],
                description=job.get("descriptionPlain") or "",
                salary_min=salary.get("minValue"),
                salary_max=salary.get("maxValue"),
                salary_currency=salary.get("currencyCode"),
                posted_at=parse_iso(job.get("publishedAt")),
            )
        )
    return jobs


async def fetch_workable(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    response = await client.get(
        f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"}
    )
    response.raise_for_status()
    jobs = []
    for job in response.json()["jobs"]:
        places = job.get("locations") or [{"city": job.get("city"), "region": job.get("state"), "country": job.get("country")}]
        location = "; ".join(
            ", ".join(p for p in (place.get("city"), place.get("region"), place.get("country")) if p) for place in places
        )
        published = job.get("published_on") or job.get("created_at")
        jobs.append(
            FetchedJob(
                external_id=job["shortcode"],
                title=job["title"],
                location=location,
                is_remote=bool(job.get("telecommuting")),
                url=job.get("url") or job["shortlink"],
                description=html_to_text(job.get("description") or ""),
                posted_at=parse_iso(published),
            )
        )
    return jobs


async def fetch_smartrecruiters(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    # The listing endpoint carries no description; the hard filters only need the title.
    jobs = []
    offset = 0
    while True:
        response = await client.get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 100, "offset": offset}
        )
        response.raise_for_status()
        data = response.json()
        for job in data["content"]:
            place = job.get("location") or {}
            location = place.get("fullLocation") or ", ".join(
                p for p in (place.get("city"), place.get("region"), (place.get("country") or "").upper()) if p
            )
            jobs.append(
                FetchedJob(
                    external_id=job["id"],
                    title=job["name"],
                    location=location,
                    is_remote=bool(place.get("remote")),
                    url=f"https://jobs.smartrecruiters.com/{slug}/{job['id']}",
                    posted_at=parse_iso(job.get("releasedDate")),
                )
            )
        offset += len(data["content"])
        if not data["content"] or offset >= data.get("totalFound", 0):
            return jobs


ATS_FETCHERS: dict[str, Callable[[httpx.AsyncClient, str], Awaitable[list[FetchedJob]]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
}

ATS_NAMES = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "workable": "Workable",
    "smartrecruiters": "SmartRecruiters",
}

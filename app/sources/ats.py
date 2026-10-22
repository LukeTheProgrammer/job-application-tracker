"""Fetchers for the public, unauthenticated job-board APIs of the common ATS vendors.

Each fetcher takes a board slug and returns FetchedJob records. They raise on HTTP
errors; the caller records the error against the company and moves on.
"""

import asyncio
import html
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

# Sites that serve HTML only (Built In, iCIMS, LaraJobs) turn away obvious bots.
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) jobs-finder/0.1"}


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
    # Set by job-board feeds, whose postings come from many companies; ATS boards leave it None.
    company: str | None = None


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


def meta(page: str, name: str) -> str:
    match = re.search(rf'<meta[^>]+(?:property|name)="{name}"[^>]+content="([^"]*)"', page, re.I)
    return html.unescape(match.group(1)).strip() if match else ""


def mentions_remote(text: str) -> bool:
    return bool(re.search(r"\bremote\b", text, re.I))


async def request(client: httpx.AsyncClient, method: str, url: str, retries: int = 4, **kwargs) -> httpx.Response:
    """Send a request, backing off when the host rate-limits; raises on any other HTTP error."""
    for attempt in range(retries + 1):
        response = await client.request(method, url, **kwargs)
        if response.status_code not in (429, 503) or attempt == retries:
            break
        retry_after = response.headers.get("Retry-After", "")
        await asyncio.sleep(min(int(retry_after), 60) if retry_after.isdigit() else 2 ** (attempt + 1))
    response.raise_for_status()
    return response


async def gather_limited[T](coros: Iterable[Awaitable[T]], limit: int = 3) -> list[T]:
    """Run per-posting detail requests concurrently without hammering one host."""
    semaphore = asyncio.Semaphore(limit)

    async def one(coro: Awaitable[T]) -> T:
        async with semaphore:
            return await coro

    return await asyncio.gather(*(one(c) for c in coros))


def jsonld_posting(page: str) -> dict:
    """The schema.org JobPosting embedded in a page's JSON-LD, or {} if there isn't one."""
    for script in re.findall(r'<script[^>]*type="application/ld(?:\+|&#x2B;)json"[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(script)
        except ValueError:
            continue
        for node in data.get("@graph", [data]) if isinstance(data, dict) else data:
            types = node.get("@type") if isinstance(node, dict) else None
            if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
                return node
    return {}


def jsonld_job(posting: dict, url: str, external_id: str) -> FetchedJob:
    address = posting.get("jobLocation") or {}
    address = (address[0] if isinstance(address, list) and address else address).get("address") or {}
    if isinstance(address, str):
        location = address
    else:
        country = address.get("addressCountry")
        country = country.get("name") if isinstance(country, dict) else country
        location = ", ".join(p for p in (address.get("addressLocality"), address.get("addressRegion"), country) if p)
    remote = posting.get("jobLocationType") == "TELECOMMUTE"
    if remote:
        location = f"Remote ({location})" if location else "Remote"
    salary = posting.get("baseSalary") if isinstance(posting.get("baseSalary"), dict) else {}
    value = salary.get("value") or {}
    annual = isinstance(value, dict) and value.get("unitText") == "YEAR"
    organization = posting.get("hiringOrganization") or {}
    return FetchedJob(
        external_id=external_id,
        title=html_to_text(posting.get("title") or ""),
        location=location,
        is_remote=remote or mentions_remote(location),
        url=url,
        description=html_to_text(posting.get("description") or ""),
        salary_min=int(float(value["minValue"])) if annual and value.get("minValue") else None,
        salary_max=int(float(value["maxValue"])) if annual and value.get("maxValue") else None,
        salary_currency=salary.get("currency") if annual else None,
        posted_at=parse_iso(posting.get("datePosted")),
        company=(organization.get("name") if isinstance(organization, dict) else organization) or "Unknown",
    )


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


async def fetch_recruitee(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    response = await client.get(f"https://{slug}.recruitee.com/api/offers/")
    response.raise_for_status()
    jobs = []
    for job in response.json()["offers"]:
        location = "; ".join(
            ", ".join(p for p in (place.get("city"), place.get("state"), place.get("country")) if p)
            for place in job.get("locations") or []
        ) or job.get("location") or ""
        salary = job.get("salary") or {}
        annual = salary.get("period") == "year"
        published = job.get("published_at") or job.get("created_at")
        jobs.append(
            FetchedJob(
                external_id=str(job["id"]),
                title=job["title"],
                location=location,
                is_remote=bool(job.get("remote")) or mentions_remote(location),
                url=job["careers_url"],
                description=html_to_text((job.get("description") or "") + (job.get("requirements") or "")),
                salary_min=int(salary["min"]) if annual and salary.get("min") else None,
                salary_max=int(salary["max"]) if annual and salary.get("max") else None,
                salary_currency=salary.get("currency") if annual else None,
                posted_at=datetime.strptime(published, "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=UTC) if published else None,
            )
        )
    return jobs


async def fetch_personio(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    response = await client.get(f"https://{slug}.jobs.personio.de/xml", params={"language": "en"})
    response.raise_for_status()
    jobs = []
    for position in ET.fromstring(response.content).iter("position"):
        offices = [position.findtext("office") or ""] + [o.text or "" for o in position.findall("additionalOffices/office")]
        location = "; ".join(o for o in offices if o)
        sections = [
            f"{d.findtext('name') or ''}\n{html_to_text(d.findtext('value') or '')}"
            for d in position.findall("jobDescriptions/jobDescription")
        ]
        job_id = position.findtext("id")
        jobs.append(
            FetchedJob(
                external_id=job_id,
                title=position.findtext("name") or "",
                location=location,
                is_remote=mentions_remote(location),
                url=f"https://{slug}.jobs.personio.de/job/{job_id}",
                description="\n\n".join(sections),
                posted_at=parse_iso(position.findtext("createdAt")),
            )
        )
    return jobs


async def fetch_bamboohr(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    # A non-existent company redirects to bamboohr.com instead of returning 404.
    response = await client.get(f"https://{slug}.bamboohr.com/careers/list", follow_redirects=False)
    if response.is_redirect:
        raise ValueError(f"no BambooHR board at {slug}.bamboohr.com")
    response.raise_for_status()

    async def detail(job: dict) -> FetchedJob:
        detail_response = await request(client, "GET", f"https://{slug}.bamboohr.com/careers/{job['id']}/detail")
        opening = detail_response.json()["result"]["jobOpening"]
        place = opening.get("location") or job.get("location") or {}
        location = ", ".join(
            p for p in (place.get("city"), place.get("state"), place.get("addressCountry")) if p
        )
        # locationType: 0 on-site, 1 remote, 2 hybrid.
        remote = job.get("isRemote") or str(job.get("locationType")) == "1"
        return FetchedJob(
            external_id=str(job["id"]),
            title=job["jobOpeningName"],
            location=location or ("Remote" if remote else ""),
            is_remote=bool(remote),
            url=f"https://{slug}.bamboohr.com/careers/{job['id']}",
            description=html_to_text(opening.get("description") or ""),
            posted_at=parse_iso(opening.get("datePosted")),
        )

    return await gather_limited(detail(job) for job in response.json()["result"])


def parse_workday_slug(slug: str) -> tuple[str, str, str]:
    """Split a careers URL like https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite
    into (host, tenant, site)."""
    parsed = urlparse(slug if "://" in slug else f"https://{slug}")
    path = [part for part in parsed.path.split("/") if part]
    if path and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", path[0]):
        path = path[1:]
    if not parsed.hostname or not parsed.hostname.endswith(".myworkdayjobs.com") or not path:
        raise ValueError("Workday slug must be the careers URL, e.g. https://acme.wd5.myworkdayjobs.com/External")
    return parsed.hostname, parsed.hostname.split(".")[0], path[0]


async def fetch_workday(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    host, tenant, site = parse_workday_slug(slug)
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    postings: list[dict] = []
    total = None
    while True:
        response = await request(
            client, "POST", f"{api}/jobs", json={"limit": 20, "offset": len(postings), "searchText": "", "appliedFacets": {}}
        )
        data = response.json()
        # Workday reports the total on the first page only; later pages say 0.
        total = data.get("total", 0) if total is None else total
        postings += data["jobPostings"]
        if not data["jobPostings"] or len(postings) >= total:
            break

    async def detail(posting: dict) -> FetchedJob:
        detail_response = await request(client, "GET", f"{api}{posting['externalPath']}")
        info = detail_response.json()["jobPostingInfo"]
        places = [info.get("location") or posting.get("locationsText") or ""] + (info.get("additionalLocations") or [])
        location = "; ".join(p for p in places if p)
        country = (info.get("country") or {}).get("descriptor")
        if country and country not in location:
            location = f"{location} ({country})"
        return FetchedJob(
            external_id=info.get("jobReqId") or info["id"],
            title=info["title"],
            location=location,
            is_remote=info.get("remoteType") == "Remote" or mentions_remote(location),
            url=info.get("externalUrl") or f"https://{host}/{site}{posting['externalPath']}",
            description=html_to_text(info.get("jobDescription") or ""),
            posted_at=parse_iso(info.get("startDate")),
        )

    # Large tenants list thousands of postings; only fetch details for ones young enough to pass the age filter.
    recent = [p for p in postings if "30+" not in (p.get("postedOn") or "")]
    return await gather_limited(detail(p) for p in recent)


async def fetch_rippling(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    api = f"https://ats.rippling.com/api/v2/board/{slug}/jobs"
    # The listing repeats a job once per location; collect them by id.
    by_id: dict[str, dict] = {}
    page = 0
    while True:
        response = await request(client, "GET", api, params={"page": page, "pageSize": 100})
        data = response.json()
        for item in data["items"]:
            entry = by_id.setdefault(item["id"], {"item": item, "locations": []})
            entry["locations"] += item.get("locations") or []
        page += 1
        if page >= data.get("totalPages", 0):
            break

    async def detail(entry: dict) -> FetchedJob:
        item = entry["item"]
        detail_response = await request(client, "GET", f"{api}/{item['id']}")
        info = detail_response.json()
        location = "; ".join(dict.fromkeys(place["name"] for place in entry["locations"] if place.get("name")))
        pay = next((p for p in info.get("payRangeDetails") or [] if p.get("frequency") == "YEAR"), {})
        return FetchedJob(
            external_id=item["id"],
            title=item["name"],
            location=location,
            is_remote=any(place.get("workplaceType") == "REMOTE" for place in entry["locations"]),
            url=item["url"],
            description=html_to_text(" ".join(v for v in (info.get("description") or {}).values() if v)),
            salary_min=int(pay["rangeStart"]) if pay.get("rangeStart") else None,
            salary_max=int(pay["rangeEnd"]) if pay.get("rangeEnd") else None,
            salary_currency=pay.get("currency"),
            posted_at=parse_iso(info.get("createdOn")),
        )

    return await gather_limited(detail(entry) for entry in by_id.values())


async def fetch_breezy(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    response = await client.get(f"https://{slug}.breezy.hr/json")
    response.raise_for_status()

    async def detail(job: dict) -> FetchedJob:
        # The JSON listing has no description; the posting page carries it in a plain div.
        page = (await request(client, "GET", job["url"])).text
        match = re.search(r'<div class="description">(.*?)<div class="apply-container">', page, re.S)
        places = job.get("locations") or [job.get("location") or {}]
        location = "; ".join(p["name"] for p in places if p.get("name"))
        remote = any(p.get("is_remote") for p in places)
        return FetchedJob(
            external_id=job["id"],
            title=job["name"],
            location=location or ("Remote" if remote else ""),
            is_remote=remote or mentions_remote(location),
            url=job["url"],
            description=html_to_text(match.group(1)) if match else "",
            posted_at=parse_iso(job.get("published_date")),
        )

    return await gather_limited(detail(job) for job in response.json())


async def jsonld_details(client: httpx.AsyncClient, urls: dict[str, str]) -> list[FetchedJob]:
    """Fetch each posting page ({external_id: url}) and read its schema.org JobPosting."""

    async def detail(external_id: str, url: str) -> FetchedJob | None:
        response = await request(client, "GET", url, headers=BROWSER_HEADERS)
        posting = jsonld_posting(response.text)
        return jsonld_job(posting, url, external_id) if posting else None

    return [job for job in await gather_limited(detail(i, u) for i, u in urls.items()) if job]


async def fetch_jazzhr(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    response = await client.get(f"https://{slug}.applytojob.com/apply", headers=BROWSER_HEADERS)
    response.raise_for_status()
    # A missing or closed board redirects to jazzhr.com or shows an "Inactive Career Page".
    if response.url.host != f"{slug}.applytojob.com" or "Inactive Career Page" in response.text:
        raise ValueError(f"no active JazzHR board at {slug}.applytojob.com")
    links = dict(
        (job_id, url)
        for url, job_id in re.findall(rf'href="(https://{re.escape(slug)}\.applytojob\.com/apply/(\w+)/[^"]*)"', response.text)
    )

    # Posting pages have no JobPosting JSON-LD and no posted date, only plain markup.
    async def detail(job_id: str, url: str) -> FetchedJob:
        page = (await request(client, "GET", url, headers=BROWSER_HEADERS)).text
        title = re.search(r"<div class='job-header'>.*?<h2>(.*?)</h2>", page, re.S)
        place = re.search(r'<div title="Location">(.*?)</div>', page, re.S)
        body = re.search(r'id="job-description">(.*?)<div class="resumator-mobile-apply-wrapper', page, re.S)
        location = html_to_text(place.group(1)) if place else ""
        return FetchedJob(
            external_id=job_id,
            title=html_to_text(title.group(1)) if title else "",
            location=location,
            is_remote=mentions_remote(location),
            url=url,
            description=html_to_text(body.group(1)) if body else "",
        )

    return await gather_limited(detail(job_id, url) for job_id, url in links.items())


async def fetch_jobvite(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    response = await client.get(f"https://jobs.jobvite.com/{slug}/jobs", headers=BROWSER_HEADERS)
    response.raise_for_status()
    if response.url.host != "jobs.jobvite.com":
        raise ValueError(f"no Jobvite board at jobs.jobvite.com/{slug}")
    links = re.findall(rf'href="(/{re.escape(slug)}/job/(\w+))"', response.text)
    return await jsonld_details(client, {job_id: f"https://jobs.jobvite.com{path}" for path, job_id in links})


ICIMS_MAX_PAGES = 10


async def fetch_icims(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    """slug: the careers subdomain, e.g. "careers-acme" for careers-acme.icims.com."""
    host = f"https://{slug}.icims.com"
    links: dict[str, str] = {}
    for page in range(ICIMS_MAX_PAGES):
        response = await request(
            client, "GET", f"{host}/jobs/search", params={"ss": 1, "in_iframe": 1, "pr": page}, headers=BROWSER_HEADERS
        )
        found = re.findall(rf'href="({re.escape(host)}/jobs/(\d+)/[^"?]*/job)', response.text)
        new = {job_id: f"{url}?in_iframe=1" for url, job_id in found if job_id not in links}
        if not new:
            break
        links.update(new)
    return await jsonld_details(client, links)


TEAMTAILOR_NS = {"tt": "https://teamtailor.com/locations"}


async def fetch_teamtailor(client: httpx.AsyncClient, slug: str) -> list[FetchedJob]:
    """slug: the careers site host, e.g. "acme.teamtailor.com" or a custom domain like "careers.acme.com"."""
    host = re.sub(r"^https?://", "", slug).split("/")[0]
    response = await client.get(f"https://{host}/jobs.rss")
    response.raise_for_status()
    jobs = []
    for item in ET.fromstring(response.content).iter("item"):
        places = [
            ", ".join(p for p in (loc.findtext("tt:city", "", TEAMTAILOR_NS), loc.findtext("tt:country", "", TEAMTAILOR_NS)) if p)
            for loc in item.findall("tt:locations/tt:location", TEAMTAILOR_NS)
        ]
        location = "; ".join(p for p in places if p)
        # remoteStatus: none, hybrid or fully.
        remote = item.findtext("remoteStatus") == "fully"
        if remote:
            location = f"Remote ({location})" if location else "Remote"
        pub = item.findtext("pubDate")
        jobs.append(
            FetchedJob(
                external_id=item.findtext("guid") or item.findtext("link") or "",
                title=item.findtext("title") or "",
                location=location,
                is_remote=remote or mentions_remote(location),
                url=item.findtext("link") or "",
                description=html_to_text(item.findtext("description") or ""),
                posted_at=parsedate_to_datetime(pub) if pub else None,
            )
        )
    return jobs


ATS_FETCHERS: dict[str, Callable[[httpx.AsyncClient, str], Awaitable[list[FetchedJob]]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
    "personio": fetch_personio,
    "bamboohr": fetch_bamboohr,
    "workday": fetch_workday,
    "rippling": fetch_rippling,
    "breezy": fetch_breezy,
    "jazzhr": fetch_jazzhr,
    "teamtailor": fetch_teamtailor,
    "jobvite": fetch_jobvite,
    "icims": fetch_icims,
}

ATS_NAMES = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "workable": "Workable",
    "smartrecruiters": "SmartRecruiters",
    "recruitee": "Recruitee",
    "personio": "Personio",
    "bamboohr": "BambooHR",
    "workday": "Workday",
    "rippling": "Rippling",
    "breezy": "Breezy HR",
    "jazzhr": "JazzHR",
    "teamtailor": "Teamtailor",
    "jobvite": "Jobvite",
    "icims": "iCIMS",
}

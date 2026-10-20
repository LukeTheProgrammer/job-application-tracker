import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.external import link_job
from app.fetch import run_fetch
from app.filters import apply_filters
from app.stack import find_stack
from app.models import APPLICATION_STATUSES, Application, Company, Job, Source
from app.profile import extract_resume_text, get_profile, parse_list
from app.seed import seed
from app.sources import ATS_NAMES

app = FastAPI(title="Jobs")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def age(when: datetime | None) -> str:
    if when is None:
        return "—"
    seconds = (datetime.now(UTC) - when).total_seconds()
    if seconds < 3600:
        return f"{max(int(seconds // 60), 1)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def workplace(job) -> str:
    # No ATS field is stored for hybrid, so it's read from the location and title text.
    text = f"{job.location or ''} {job.title}"
    if re.search(r"\bhybrid\b", text, re.I):
        return "Hybrid"
    return "Remote" if job.is_remote else "On Site"


templates.env.filters["age"] = age
templates.env.filters["workplace"] = workplace
templates.env.filters["stack"] = lambda job: find_stack(f"{job.title}\n{job.description or ''}")


@app.on_event("startup")
def startup() -> None:
    seed()


@app.get("/")
def home() -> RedirectResponse:
    return RedirectResponse("/jobs")


@app.get("/jobs")
def list_jobs(request: Request, filtered: bool = False, dismissed: bool = False, session: Session = Depends(get_session)):
    active = select(Job).where(Job.active)
    applied = select(Application.id).where(Application.job_id == Job.id, Application.status != "Preparing").exists()
    views = {
        # Jobs already applied to live on the Applications page; ones still being prepared stay here.
        "matches": Job.filter_reason.is_(None) & Job.dismissed_at.is_(None) & ~applied,
        "filtered": Job.filter_reason.is_not(None),
        "dismissed": Job.dismissed_at.is_not(None),
    }
    counts = {
        name: session.scalar(select(func.count()).select_from(active.where(cond).subquery()))
        for name, cond in views.items()
    }
    view = "dismissed" if dismissed else "filtered" if filtered else "matches"
    order = Job.dismissed_at.desc() if dismissed else Job.score.desc().nulls_last()
    jobs = session.scalars(
        active.where(views[view])
        .order_by(order, Job.posted_at.desc().nulls_last(), Job.first_seen_at.desc())
        .limit(500)
    ).all()
    tracked = dict(session.execute(select(Application.job_id, Application.id).where(Application.job_id.is_not(None))).all())
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "page": "jobs",
            "jobs": jobs,
            "view": view,
            "counts": counts,
            "tracked": tracked,
            "statuses": APPLICATION_STATUSES,
            "blank": Application(status="Applied", applied_on=date.today(), company_name="", title="", url="", cover_letter="", notes=""),
            "next": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
        },
    )


@app.post("/jobs/{job_id}/dismiss")
def dismiss_job(job_id: int, reason: Annotated[str, Form()] = "", session: Session = Depends(get_session)) -> RedirectResponse:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404)
    job.dismissed_at = datetime.now(UTC)
    job.dismiss_reason = reason.strip() or None
    session.commit()
    return RedirectResponse("/jobs", status_code=303)


@app.post("/jobs/{job_id}/restore")
def restore_job(job_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404)
    job.dismissed_at = None
    job.dismiss_reason = None
    session.commit()
    return RedirectResponse("/jobs?dismissed=1", status_code=303)


@app.get("/sources")
def show_sources(request: Request, message: str = "", session: Session = Depends(get_session)):
    sources = session.scalars(select(Source)).all()
    sources = sorted(sources, key=lambda s: list(ATS_NAMES).index(s.key) if s.key in ATS_NAMES else len(ATS_NAMES))
    company_counts = dict(session.execute(select(Company.ats, func.count()).group_by(Company.ats)).all())
    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "page": "sources",
            "message": message,
            "sources": sources,
            "names": ATS_NAMES,
            "company_counts": company_counts,
            "last_fetch": session.scalar(select(func.max(Company.last_fetched_at))),
        },
    )


@app.post("/sources/fetch")
def start_fetch(background: BackgroundTasks) -> RedirectResponse:
    background.add_task(run_fetch)
    return RedirectResponse("/sources?message=Fetch+started.+Refresh+in+a+minute+to+see+results.", status_code=303)


@app.post("/sources/{key}/toggle")
def toggle_source(key: str, session: Session = Depends(get_session)) -> RedirectResponse:
    source = session.get(Source, key)
    if source is None:
        raise HTTPException(404)
    source.enabled = not source.enabled
    session.commit()
    return RedirectResponse("/sources", status_code=303)


@app.get("/companies")
def show_companies(request: Request, message: str = "", session: Session = Depends(get_session)):
    sources = session.scalars(select(Source)).all()
    sources = sorted(sources, key=lambda s: list(ATS_NAMES).index(s.key) if s.key in ATS_NAMES else len(ATS_NAMES))
    return templates.TemplateResponse(
        request,
        "companies.html",
        {
            "page": "companies",
            "message": message,
            "sources": sources,
            "names": ATS_NAMES,
            "enabled_sources": {s.key: s.enabled for s in sources},
            "companies": session.scalars(select(Company).order_by(func.lower(Company.name))).all(),
        },
    )


@app.post("/companies")
def add_company(
    name: Annotated[str, Form()],
    ats: Annotated[str, Form()],
    slug: Annotated[str, Form()],
    session: Session = Depends(get_session),
) -> RedirectResponse:
    if ats not in ATS_NAMES:
        raise HTTPException(422, "Unknown ATS")
    session.add(Company(name=name.strip(), ats=ats, slug=slug.strip().strip("/")))
    try:
        session.commit()
    except IntegrityError:
        return RedirectResponse("/companies?message=That+board+is+already+in+the+list.", status_code=303)
    return RedirectResponse("/companies?message=Added.+It+will+be+fetched+on+the+next+run.", status_code=303)


@app.post("/companies/{company_id}/toggle")
def toggle_company(company_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(404)
    company.enabled = not company.enabled
    session.commit()
    return RedirectResponse("/companies", status_code=303)


@app.post("/companies/{company_id}/delete")
def delete_company(company_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    company = session.get(Company, company_id)
    if company is not None:
        session.delete(company)
        session.commit()
    return RedirectResponse("/companies", status_code=303)


@app.get("/profile")
def show_profile(request: Request, session: Session = Depends(get_session), saved: bool = False):
    return templates.TemplateResponse(
        request, "profile.html", {"page": "profile", "profile": get_profile(session), "saved": saved}
    )


@app.post("/profile")
async def save_profile(
    resume_text: Annotated[str, Form()] = "",
    resume_file: UploadFile | None = None,
    target_roles: Annotated[str, Form()] = "",
    title_keywords: Annotated[str, Form()] = "",
    title_terms: Annotated[str, Form()] = "",
    locations: Annotated[str, Form()] = "",
    remote_ok: Annotated[bool, Form()] = False,
    remote_regions: Annotated[str, Form()] = "",
    excluded_stacks: Annotated[str, Form()] = "",
    salary_floor: Annotated[str, Form()] = "",
    max_age_days: Annotated[int, Form(ge=1, le=365)] = 30,
    notes: Annotated[str, Form()] = "",
    session: Session = Depends(get_session),
):
    profile = get_profile(session)

    # An uploaded file replaces the pasted text.
    if resume_file is not None and resume_file.filename:
        resume_text = extract_resume_text(resume_file.filename, await resume_file.read())

    profile.resume_text = resume_text.strip()
    profile.target_roles = parse_list(target_roles)
    profile.title_keywords = parse_list(title_keywords)
    profile.title_terms = parse_list(title_terms)
    profile.locations = parse_list(locations)
    profile.remote_ok = remote_ok
    profile.remote_regions = parse_list(remote_regions)
    profile.excluded_stacks = parse_list(excluded_stacks)
    digits = "".join(c for c in salary_floor if c.isdigit())
    profile.salary_floor = int(digits) if digits else None
    profile.max_age_days = max_age_days
    profile.notes = notes.strip()
    session.commit()
    apply_filters(session, profile)

    return RedirectResponse("/profile?saved=1", status_code=303)


APPLICATION_SORTS = {
    "status": Application.status,
    "title": func.lower(Application.title),
    "company": func.lower(Application.company_name),
    "applied_on": Application.applied_on,
    "updated": Application.updated_at,
}


@app.get("/applications")
def list_applications(
    request: Request, status: str = "", sort: str = "applied_on", dir: str = "desc", session: Session = Depends(get_session)
):
    counts = dict(session.execute(select(Application.status, func.count()).group_by(Application.status)).all())
    if sort not in APPLICATION_SORTS:
        sort = "applied_on"
    if dir not in ("asc", "desc"):
        dir = "desc"
    column = APPLICATION_SORTS[sort]
    order = column.asc().nulls_last() if dir == "asc" else column.desc().nulls_last()
    query = select(Application).order_by(order, Application.updated_at.desc())
    if status:
        query = query.where(Application.status == status)
    return templates.TemplateResponse(
        request,
        "applications.html",
        {
            "page": "applications",
            "applications": session.scalars(query).all(),
            "statuses": APPLICATION_STATUSES,
            "counts": counts,
            "total": sum(counts.values()),
            "current": status,
            "sort": sort,
            "dir": dir,
            "blank": Application(status="Applied", applied_on=date.today(), company_name="", title="", url="", cover_letter="", notes=""),
            "next": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
        },
    )


def fill_application(
    application: Application,
    company_name: str,
    title: str,
    status: str,
    url: str,
    applied_on: str,
    cover_letter: str,
    notes: str,
) -> None:
    if status not in APPLICATION_STATUSES:
        raise HTTPException(422, "Unknown status")
    application.company_name = company_name.strip()
    application.title = title.strip()
    application.url = url.strip()
    application.status = status
    application.applied_on = date.fromisoformat(applied_on) if applied_on else None
    # Moving past "Preparing" without a date means it was applied today.
    if application.applied_on is None and status != "Preparing":
        application.applied_on = date.today()
    application.cover_letter = cover_letter.strip()
    application.notes = notes.strip()


@app.post("/applications")
def add_application(
    company_name: Annotated[str, Form()],
    title: Annotated[str, Form()],
    status: Annotated[str, Form()] = "Applied",
    url: Annotated[str, Form()] = "",
    applied_on: Annotated[str, Form()] = "",
    cover_letter: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    job_id: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
    session: Session = Depends(get_session),
) -> RedirectResponse:
    # Applying from the Jobs page links the job, reusing its application if one already exists.
    job = session.get(Job, int(job_id)) if job_id.isdigit() else None
    application = session.scalar(select(Application).where(Application.job_id == job.id)) if job else None
    if application is None:
        application = Application(job=job)
        session.add(application)
    fill_application(application, company_name, title, status, url, applied_on, cover_letter, notes)
    link_job(session, application)
    session.commit()
    if next.startswith(("/applications", "/jobs")):
        return RedirectResponse(next, status_code=303)
    return RedirectResponse(f"/applications/{application.id}", status_code=303)


@app.get("/applications/{application_id}")
def show_application(
    request: Request,
    application_id: int,
    saved: bool = False,
    partial: bool = False,
    next: str = "",
    session: Session = Depends(get_session),
):
    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(404)
    # partial=1 returns just the form, for the edit dialog on the Applications page.
    return templates.TemplateResponse(
        request,
        "_application_form.html" if partial else "application.html",
        {"page": "applications", "a": application, "statuses": APPLICATION_STATUSES, "saved": saved, "next": next},
    )


@app.post("/applications/{application_id}")
def save_application(
    application_id: int,
    company_name: Annotated[str, Form()],
    title: Annotated[str, Form()],
    status: Annotated[str, Form()],
    url: Annotated[str, Form()] = "",
    applied_on: Annotated[str, Form()] = "",
    cover_letter: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
    session: Session = Depends(get_session),
) -> RedirectResponse:
    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(404)
    fill_application(application, company_name, title, status, url, applied_on, cover_letter, notes)
    link_job(session, application)
    session.commit()
    if next.startswith("/applications"):
        return RedirectResponse(next, status_code=303)
    return RedirectResponse(f"/applications/{application_id}?saved=1", status_code=303)


@app.post("/applications/{application_id}/delete")
def delete_application(application_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    application = session.get(Application, application_id)
    if application is not None:
        session.delete(application)
        session.commit()
    return RedirectResponse("/applications", status_code=303)

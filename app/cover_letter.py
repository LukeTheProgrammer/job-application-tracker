"""Hand-off between the database and a Claude Code cover letter session (see .claude/skills/cover-letter).

    python -m app.cover_letter show JOB_ID                      # resume, past answers, the job's posting and its application, if any
    python -m app.cover_letter background                       # resume and past answers only, for a job not in the database
    python -m app.cover_letter save JOB_ID LETTER.txt           # save to the job's application, creating one if needed
    python -m app.cover_letter save-new COMPANY TITLE URL LETTER.txt   # for a job not in the database
"""

import pathlib
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.external import link_job
from app.models import Application, Job, Question
from app.profile import get_profile
from app.scoring import plain


def background(session) -> None:
    """Print the resume, then every saved application/interview answer, for the user's history and soft skills."""
    print(f"# RESUME\n{get_profile(session).resume_text}\n")
    questions = session.scalars(select(Question).where(Question.answer != "").order_by(Question.updated_at.desc())).all()
    print(f"# PAST ANSWERS ({len(questions)})")
    for q in questions:
        where = f" | {q.application.title} at {q.application.company_name}" if q.application else ""
        print(f"## {q.kind}{where}\nQ: {q.question}\nA: {q.answer}\n")


def show(job_id: int) -> None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            sys.exit(f"No job {job_id}")
        application = session.scalar(select(Application).where(Application.job_id == job_id))
        background(session)
        print(f"# JOB {job.id}\n{job.title} | {job.company_name} | {job.location} | {job.url}\n{plain(job.description)}\n")
        if application:
            print(f"# APPLICATION {application.id} | {application.status} | cover letter: {len((application.cover_letter or '').split())} words")
        else:
            print("# APPLICATION none yet")


def save(application: Application, session, letter_path: str) -> None:
    replaced = len((application.cover_letter or "").split())
    application.cover_letter = pathlib.Path(letter_path).read_text().strip()
    session.commit()
    print(f"Saved to application {application.id}" + (f" (replaced a {replaced}-word letter)" if replaced else ""))


def save_for_job(job_id: int, letter_path: str) -> None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            sys.exit(f"No job {job_id}")
        application = session.scalar(select(Application).where(Application.job_id == job_id))
        if application is None:
            # Not applied yet, so no applied date.
            application = Application(job=job, company_name=job.company_name, title=job.title, url=job.url, status="Preparing")
            session.add(application)
        save(application, session, letter_path)


def save_new(company_name: str, title: str, url: str, letter_path: str) -> None:
    with SessionLocal() as session:
        application = Application(company_name=company_name, title=title, url=url, status="Preparing")
        session.add(application)
        link_job(session, application)
        save(application, session, letter_path)
        if application.job_id:
            print(f"Linked to job {application.job_id}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "show":
        show(int(sys.argv[2]))
    elif len(sys.argv) == 2 and sys.argv[1] == "background":
        with SessionLocal() as session:
            background(session)
    elif len(sys.argv) == 4 and sys.argv[1] == "save":
        save_for_job(int(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) == 6 and sys.argv[1] == "save-new":
        save_new(*sys.argv[2:])
    else:
        sys.exit(__doc__)

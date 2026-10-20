"""Hand-off between the database and a Claude Code scoring session (see .claude/skills/score-jobs).

    python -m app.scoring export OUT.txt [--all]   # resume, preferences, feedback and unscored matches
    python -m app.scoring import SCORES.json       # {"<job id>": {"score": 0-100, "reason": "...", "red_flags": [...]}}
"""

import html
import json
import re
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Application, Job
from app.profile import get_profile


def plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text or ""))).strip()


def export(path: str, rescore: bool) -> None:
    session = SessionLocal()
    profile = get_profile(session)
    query = select(Job).where(Job.active, Job.filter_reason.is_(None), Job.dismissed_at.is_(None))
    if not rescore:
        query = query.where(Job.score.is_(None))
    jobs = session.scalars(query.order_by(Job.id)).all()
    dismissed = session.scalars(select(Job).where(Job.dismissed_at.is_not(None)).order_by(Job.dismissed_at.desc())).all()
    applied = session.scalars(select(Application).order_by(Application.updated_at.desc())).all()

    with open(path, "w") as out:
        out.write(f"# RESUME\n{' '.join(profile.resume_text.split())}\n\n# PREFERENCES\n")
        out.write(f"Target roles: {', '.join(profile.target_roles)}\n")
        out.write(f"Remote OK: {profile.remote_ok} ({', '.join(profile.remote_regions)})\n")
        out.write(f"Locations: {', '.join(profile.locations) or '-'}\nSalary floor: {profile.salary_floor or '-'}\n")
        out.write(f"Notes: {profile.notes or '-'}\n\n# DISMISSED (the user's feedback; learn from it)\n")
        for job in dismissed:
            out.write(f"- {job.title} | {job.company_name} | scored {job.score} | reason: {job.dismiss_reason or '(none given)'}\n")
        out.write("\n# APPLIED (roles the user chose to pursue)\n")
        for a in applied:
            out.write(f"- {a.title} | {a.company_name} | {a.status}\n")
        out.write(f"\n# JOBS TO SCORE ({len(jobs)})\n")
        for job in jobs:
            salary = f"{job.salary_min}-{job.salary_max} {job.salary_currency or ''}" if job.salary_max else "not listed"
            out.write(f"\n=== JOB {job.id} | {job.title} | {job.company_name} | {job.location} | salary {salary}\n")
            out.write(plain(job.description)[:6000] + "\n")
    print(f"{len(jobs)} jobs to score written to {path}")


def import_scores(path: str) -> None:
    with open(path) as f:
        scores = json.load(f)
    session = SessionLocal()
    for job_id, s in scores.items():
        job = session.get(Job, int(job_id))
        if job is None:
            print(f"skipping unknown job {job_id}")
            continue
        job.score = max(0, min(100, int(s["score"])))
        job.score_reason = s["reason"]
        job.red_flags = s.get("red_flags", [])
    session.commit()
    print(f"{len(scores)} scores saved")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "export":
        export(sys.argv[2], "--all" in sys.argv)
    elif len(sys.argv) == 3 and sys.argv[1] == "import":
        import_scores(sys.argv[2])
    else:
        sys.exit(__doc__)

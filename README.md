# jobs

A personal job-search tool. It pulls postings from company job boards, filters out the ones that don't fit, ranks the rest against your resume, and tracks the applications you send. It never applies for you.

## What it does

- **Fetches** postings from the public APIs of Greenhouse, Lever, Ashby, Workable and SmartRecruiters for a list of companies you choose, every few hours.
- **Filters** them with cheap hard rules from your profile: title keywords, location or remote region, excluded stacks, salary floor and posting age.
- **Scores** the matches 0–100 for fit against your resume, with a reason and red flags. For now this runs from a Claude Code session using the `score-jobs` skill in `.claude/skills/`; automatic scoring isn't built yet.
- **Tracks** applications with status, applied date, cover letter and notes, including jobs found elsewhere.

Everything is server-rendered (FastAPI + Jinja, styled with [Pico CSS](https://picocss.com)), with no JS build step.

## Setup

Needs Python 3.12+ and PostgreSQL.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env            # set DATABASE_URL
set -a; . ./.env; set +a
.venv/bin/uvicorn app.main:app --reload    # http://127.0.0.1:8000
.venv/bin/python -m app.fetch              # one fetch run; schedule this with cron or a systemd timer
```

Tables are created on startup. The starting company list comes from `companies.yaml` and is loaded once, when the companies table is empty; after that, manage companies on the Sources page. Paste or upload your resume and set your search preferences on the Profile page.

There's no authentication. Run it locally or put it behind something that restricts access.

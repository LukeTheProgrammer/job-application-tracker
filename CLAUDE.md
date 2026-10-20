# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

ATS fetchers, hard filters, application tracking, and the Jobs/Applications/Sources/Profile pages are built. Automatic LLM scoring, the remote boards, JobSpy and applied/not-interested feedback are not built yet. `index.html` is a leftover static placeholder for jobs.realm-of-tuba.com. The FastAPI app is meant to sit behind the web server as a reverse proxy.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # needs python3-venv
cp .env.example .env                                          # set DATABASE_URL (Postgres, psycopg driver)
set -a; . ./.env; set +a; .venv/bin/uvicorn app.main:app --reload   # http://127.0.0.1:8000
set -a; . ./.env; set +a; .venv/bin/python -m app.fetch              # one fetch run (what the timer runs)
```

Production: systemd unit `jobs.service` runs uvicorn on 127.0.0.1:8000 as `luke`, loading `.env`. nginx (`/etc/nginx/sites-available/jobs`) proxies everything to it. Don't serve the repo directory statically, because it contains `.env`. After code changes, run `sudo systemctl restart jobs`. Logs: `journalctl -u jobs`. `jobs-fetch.timer` runs `jobs-fetch.service` (`python -m app.fetch`) every 3 hours; logs: `journalctl -u jobs-fetch`.

`app/seed.py` creates tables with `create_all` at startup and on each fetch. There are no migrations: `create_all` adds new tables but never new columns, so a new column on an existing model also needs a manual `ALTER TABLE ... ADD COLUMN`, or Alembic once it's added.

## Layout

- `app/models.py`: `Profile` is a single-row table (id always 1) holding the resume text and every search preference. The filters and scorer must read their settings from it, not from config files. `Source` holds on/off switches keyed by ATS name. `Company` is one board (ats + slug). `Job` is one deduped posting.
- `app/sources/ats.py`: one async fetcher per ATS, each normalizing to `FetchedJob`. Add a new source by adding a fetcher to `ATS_FETCHERS` and a label to `ATS_NAMES`. `seed()` creates its `Source` row automatically.
- `app/fetch.py`: `run_fetch` fetches enabled companies of enabled sources concurrently. A failing board is recorded in `Company.last_error` and doesn't abort the run. Postings missing from a successful fetch are marked `active=False`. A Postgres advisory lock prevents overlapping runs, whether started by the timer or by the "Fetch now" button.
- `app/filters.py`: `apply_filters` re-evaluates every active job and stores the first failed rule in `Job.filter_reason` (`NULL` = match). It runs after every fetch and every profile save, so profile changes take effect immediately. Excluded stacks match titles only; descriptions are left to the LLM scorer.
- `Application` (in `app/models.py`): a tracked application with status (`APPLICATION_STATUSES`), applied date, cover letter and notes. It snapshots company/title/url and its `job_id` is `SET NULL` on delete, so it survives removal of the company or job and can also be a job found elsewhere.
- `Job.score` / `score_reason` / `red_flags`: fit against the resume (0-100). Without an API key they're filled in from a Claude Code session with the `score-jobs` skill, which uses `app/scoring.py` (export/import) and feeds in the user's dismissal reasons. Nothing scores new jobs automatically yet. `Job.dismissed_at`/`dismiss_reason` hide a job from Matches and record why. The Jobs page sorts by score.
- `companies.yaml`: the initial company list, loaded only when the companies table is empty. After that, companies are managed on the Sources page.
- `app/profile.py`: `get_profile` (creates the row on first access), list-field parsing, and resume text extraction from PDF/text uploads.
- `app/main.py` + `app/templates/`: server-rendered Jinja pages with plain form POSTs. There is no JS build step.

## Purpose

This app finds job postings worth applying to and ranks them. It never applies automatically. Applying stays manual.

## Agreed design

**Stack:** Python, httpx, PostgreSQL (not SQLite), the Anthropic SDK, and a FastAPI web UI. Results go to the web UI. There is no email digest. The fetch pipeline runs on cron several times a day, because applying within 48 hours of posting matters.

**Sources:**
- Company ATS boards through their public JSON APIs: Greenhouse, Lever, Ashby, Workable and SmartRecruiters. This is the primary source. SmartRecruiters' listing API returns no description.
- Remote boards: RemoteOK API, Remotive API, We Work Remotely RSS.
- JobSpy for LinkedIn and Indeed. Expect blocks and breakage, so it must fail soft and never stop the other sources from running.
- Deliberately excluded: Gmail alert parsing, HN "Who is Hiring", Adzuna and USAJobs. Don't add these back without asking.

**Pipeline:**
1. Fetch all sources.
2. Dedupe in Postgres, keyed on a hash of (company, title, location). Keep every posting already seen.
3. Apply cheap hard filters first: title regex (senior, staff, principal), location or remote, excluded stacks, salary floor when the posting lists one, posting age of 30 days or less.
4. Score only the postings that pass the filters with Claude, using the resume and the job description. Get back a fit score from 0 to 100, a reason, and red flags.
5. Show the results in the web UI, sorted by score, with links.
6. Let the user mark postings as applied or not interested. That feedback feeds back into the filters and the scoring prompt.

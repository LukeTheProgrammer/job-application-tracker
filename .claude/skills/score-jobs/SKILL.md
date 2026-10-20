---
name: score-jobs
description: Score unscored job matches in the jobs app against the user's resume and past feedback. Use when the user asks to score, rescore or review new jobs.
---

# Score jobs

Score each matched job 0-100 for how well it fits the user, then save the scores to the app's database. Never apply to jobs or contact anyone.

## Steps

1. Export what needs scoring (add `--all` only if the user asks to rescore everything):
   ```bash
   cd /var/www/html/jobs && set -a && . ./.env && set +a && .venv/bin/python -m app.scoring export "$SCRATCH/to-score.txt"
   ```
   Use the session scratchpad directory for `$SCRATCH`. If it reports 0 jobs, say so and stop.
2. Read the whole file: resume, preferences, the DISMISSED list with the user's reasons, the APPLIED list, then every job.
3. Before scoring, sum up in a sentence or two what the dismissals and applications say about what the user wants. Apply that to every score: a job resembling one the user dismissed scores lower for the stated reason.
4. Write `$SCRATCH/scores.json`:
   ```json
   {"2208": {"score": 70, "reason": "One sentence on the fit, naming the specific match or gap.", "red_flags": ["Short tag", "Short tag"]}}
   ```
5. Import: `.venv/bin/python -m app.scoring import "$SCRATCH/scores.json"`
6. Report the top few jobs (score, title, company), how many scored under 20, and any title words that keep producing non-engineering roles, which the user could add to "Exclude titles mentioning" on the Profile page.

## Scale

- **70-100** Strong: the role's core stack and level match the resume; worth applying soon.
- **45-69** Plausible: right kind of role with one real gap (a language, a domain, location).
- **20-44** Stretch: engineering role, but the main stack or focus differs.
- **0-19** Poor: not a software engineering role, or wrong platform entirely.

Weigh, in order: is it hands-on software engineering at senior+ level; overlap with the resume's stack (PHP/Laravel, Vue/React/TypeScript, MySQL, AWS); remote US eligibility; pay against the salary floor when listed; patterns in the user's dismissal reasons.

Reasons are one sentence, specific, in plain words. Red flags are 1-4 short tags, e.g. "Go backend", "Pay below floor", "On-call", "Travel 25%".

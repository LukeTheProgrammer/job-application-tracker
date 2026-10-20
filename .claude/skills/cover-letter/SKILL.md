---
name: cover-letter
description: Write a short 3-paragraph cover letter for a job, using the resume saved in the jobs app. Use when the user asks for a cover letter for a job ID, an application, a posting URL, or a pasted job description.
---

# Cover letter

Write a short cover letter for one job, grounded in the user's resume. Never send it, submit it or contact anyone.

## Steps

1. Get the resume and the job. Run from `/var/www/html/jobs` after `set -a && . ./.env && set +a`:
   ```bash
   .venv/bin/python -m app.cover_letter show JOB_ID
   ```
   This prints the resume, the posting and the job's application, if one exists. For an application ID, use its `job_id`. If the user pasted a description for a job that isn't in the database, use that, and get the resume with `.venv/bin/python -c "from app.db import SessionLocal; from app.profile import get_profile; print(get_profile(SessionLocal()).resume_text)"`. If the stored description is empty or a stub (SmartRecruiters jobs never have one), fetch the job's URL with WebFetch and say the description came from the live page. If that also returns no real description (pages built with JavaScript, such as Workday or Ashby, often don't), ask the user to paste it.
2. Pick the 2-3 requirements in the posting that the resume matches best, with a concrete achievement for each: a named employer, product or result. Don't claim skills the resume doesn't show. If the posting needs one the user lacks, leave it out rather than inventing experience.
3. Write the letter to `$SCRATCH/letter-JOB_ID.txt` (the session scratchpad) and show it to the user in full.
4. Always save it to the application, without asking:
   ```bash
   .venv/bin/python -m app.cover_letter save JOB_ID "$SCRATCH/letter-JOB_ID.txt"
   ```
   This saves to the job's application, or creates one with status Preparing if there isn't one. For a job not in the database, use `save-new COMPANY TITLE URL LETTER_FILE` instead; it also creates a Job from the posting URL and prints `Linked to job N` when that works. Tell the user which application it went to, and if the output says it replaced an earlier letter, say so.

## Format

Three paragraphs, 200-280 words in total. No address block, date or subject line. Start with "Dear COMPANY Hiring Team," using the company's name, e.g. "Dear Clear Street Hiring Team," (or the hiring manager's name if the posting gives one) and end with "Sincerely,\nLuke Henry".

1. **Opening:** the role and company by name, and one sentence on why this company or product specifically, drawn from the posting.
2. **Fit:** the 2-3 matched requirements, each backed by a specific achievement from the resume. This is the longest paragraph.
3. **Close:** what the user would bring to the team in the first months, and a brief line inviting a conversation.

Plain, confident and specific. Avoid filler like "I am writing to express my interest", "passionate", "synergy", "fast-paced environment" and "I believe I would be a great fit". Don't repeat the resume line by line. Don't use em dashes.

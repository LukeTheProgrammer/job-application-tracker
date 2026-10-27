"""Runs the cover-letter skill headless (`claude -p`) for an application, from the web UI.

Runs live in this process's memory, so a restart of the app forgets (and systemd kills) any in progress.
"""

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
# Nobody is there to approve a tool call, so everything the skill needs is allowed up front.
ALLOWED_TOOLS = "Bash(.venv/bin/python -m app.cover_letter *),Read,Write,WebFetch"
# A normal run takes a minute or two; anything past this is treated as hung and killed.
TIMEOUT = 600

PROMPT = """/cover-letter {job_id}

You're running unattended from the jobs web app, so nobody can answer questions. DATABASE_URL is already set, so run the app.cover_letter commands without sourcing .env. Write the letter with the Write tool to {letter_path} and save it from that exact path (no shell variables). If you can't get a real job description, don't write or save a letter; print one line saying why.{extra}"""

runs: dict[int, dict] = {}


def running(application_id: int) -> bool:
    run = runs.get(application_id)
    return run is not None and "result" not in run and run["proc"].poll() is None


def start(application_id: int, job_id: int, instructions: str, current_letter: str) -> None:
    if running(application_id):
        return
    extra = f"\n\nInstructions from the user for this letter:\n{instructions.strip()}" if instructions.strip() else ""
    # A headless run has no session scratchpad, so it gets its own folder for the letter file.
    folder = Path(tempfile.mkdtemp(prefix=f"letter-{application_id}-"))
    prompt = PROMPT.format(job_id=job_id, letter_path=folder / f"letter-{job_id}.txt", extra=extra)
    try:
        with open(folder / "claude.log", "w") as log:
            proc = subprocess.Popen(
                [CLAUDE, "-p", prompt, "--allowedTools", ALLOWED_TOOLS],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
    except OSError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        runs[application_id] = {"result": {"state": "failed", "output": f"Couldn't start Claude Code ({CLAUDE}): {exc}"}}
        return
    runs[application_id] = {"proc": proc, "folder": folder, "started": time.monotonic(), "before": current_letter}


def status(application_id: int, letter: str) -> dict:
    """State of the latest run: idle, running, done (letter saved) or failed, with Claude's output when it failed."""
    run = runs.get(application_id)
    if run is None:
        return {"state": "idle"}
    if "result" in run:
        return run["result"]
    proc = run["proc"]
    code = proc.poll()
    timed_out = code is None and time.monotonic() - run["started"] > TIMEOUT
    if code is None and not timed_out:
        return {"state": "running"}
    if timed_out:
        proc.kill()
        proc.wait()
    output = (run["folder"] / "claude.log").read_text(errors="replace").strip()
    shutil.rmtree(run["folder"], ignore_errors=True)
    if timed_out:
        result = {"state": "failed", "output": f"Timed out after {TIMEOUT // 60} minutes and was stopped.\n\n{output[-1000:]}".strip()}
    # Exiting cleanly without saving means the skill stopped early, e.g. it couldn't read the posting or had a question.
    elif code == 0 and letter != run["before"]:
        result = {"state": "done", "letter": letter}
    else:
        result = {"state": "failed", "output": output[-1500:] or f"claude exited with code {code}"}
    run["result"] = result
    return result

import io

from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.models import Profile


def get_profile(session: Session) -> Profile:
    profile = session.get(Profile, 1)
    if profile is None:
        profile = Profile(id=1, title_keywords=["senior", "staff", "principal"])
        session.add(profile)
        session.commit()
    return profile


def parse_list(raw: str) -> list[str]:
    """One item per line or comma-separated; blanks and duplicates dropped, order kept."""
    items = [part.strip() for line in raw.splitlines() for part in line.split(",")]
    return list(dict.fromkeys(item for item in items if item))


def extract_resume_text(filename: str, data: bytes) -> str:
    if filename.lower().endswith(".pdf"):
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    return data.decode("utf-8", errors="replace").strip()

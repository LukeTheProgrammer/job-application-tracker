from pathlib import Path

import yaml
from sqlalchemy import func, select

from app.db import Base, SessionLocal, engine
from app.models import Company, Source
from app.sources import ATS_NAMES

COMPANIES_FILE = Path(__file__).resolve().parent.parent / "companies.yaml"


def seed() -> None:
    """Create tables, make sure every known source has a row, and load companies.yaml once.

    companies.yaml is only an initial list: after the first load, companies are managed in the UI,
    so deleting one there doesn't bring it back on restart.
    """
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        known = set(session.scalars(select(Source.key)))
        session.add_all(Source(key=key, enabled=True) for key in ATS_NAMES if key not in known)
        session.flush()
        if session.scalar(select(func.count()).select_from(Company)) == 0 and COMPANIES_FILE.exists():
            for entry in yaml.safe_load(COMPANIES_FILE.read_text()) or []:
                session.add(Company(name=entry["name"], ats=entry["ats"], slug=entry["slug"]))
        session.commit()

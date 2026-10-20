from app.sources.ats import ATS_FETCHERS as _ATS_FETCHERS
from app.sources.ats import ATS_NAMES as _ATS_NAMES
from app.sources.ats import FetchedJob
from app.sources.boards import BOARD_DEFAULTS, BOARD_FETCHERS, BOARD_NAMES

# Every source the pipeline knows, keyed by Source.key: the ATS vendors, then the job boards.
ATS_FETCHERS = {**_ATS_FETCHERS, **BOARD_FETCHERS}
ATS_NAMES = {**_ATS_NAMES, **BOARD_NAMES}

__all__ = ["ATS_FETCHERS", "ATS_NAMES", "BOARD_DEFAULTS", "BOARD_NAMES", "FetchedJob"]

"""NSE trading calendar from NSE's own holiday list (nseindia.com holiday-master).

The list is saved to data/nse_holidays.json together with where and when it was fetched. If the
file is missing, or does not cover the year being asked about, the calendar says so
(`covers(year)` is False) and falls back to Monday-Friday only; callers must treat that as
UNVERIFIED. Entries marked with '*' by NSE (for example the Diwali muhurat session) are special
sessions with their own timings: they are reported as SPECIAL and never as regular trading days.
"""
import json
from datetime import date, datetime
from pathlib import Path

import requests

from config.settings import ROOT

PATH = ROOT / "data" / "nse_holidays.json"
HOME = "https://www.nseindia.com/"
API = "https://www.nseindia.com/api/holiday-master?type=trading"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


class CalendarError(Exception):
    pass


def fetch_from_nse(session=None, timeout=20) -> dict:
    """Download the official list. NSE needs a cookie from the home page first."""
    s = session or requests.Session()
    try:
        s.get(HOME, headers={"User-Agent": UA}, timeout=timeout)
        r = s.get(API, headers={"User-Agent": UA, "Accept": "application/json",
                                "Referer": "https://www.nseindia.com/resources/exchange-communication-holidays"},
                  timeout=timeout)
        r.raise_for_status()
        raw = r.json()
        rows = raw["FO"]
    except (requests.RequestException, ValueError, KeyError) as e:
        raise CalendarError(f"Could not fetch NSE holidays: {e}") from e
    days = {}
    for x in rows:
        d = datetime.strptime(x["tradingDate"], "%d-%b-%Y").date().isoformat()
        days[d] = {"name": x["description"].rstrip("*").strip(), "special": x["description"].endswith("*")}
    years = sorted({int(d[:4]) for d in days})
    if not days:
        raise CalendarError("NSE returned an empty holiday list")
    return {"source": API, "fetched_at": datetime.now().isoformat(timespec="seconds"), "years": years, "days": days}


def refresh(path: Path = PATH, session=None) -> dict:
    data = fetch_from_nse(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1))
    return data


class NSECalendar:
    def __init__(self, path: Path = PATH, data: dict = None):
        self.data = data
        if self.data is None and Path(path).exists():
            try:
                self.data = json.loads(Path(path).read_text())
            except (OSError, ValueError):
                self.data = None

    def covers(self, year: int) -> bool:
        return bool(self.data) and year in self.data.get("years", [])

    def day_type(self, d: date) -> str:
        """REGULAR / WEEKEND / HOLIDAY / SPECIAL (special session, timings differ)."""
        entry = (self.data or {}).get("days", {}).get(d.isoformat())
        if entry:
            return "SPECIAL" if entry["special"] else ("WEEKEND" if d.weekday() >= 5 else "HOLIDAY")
        return "WEEKEND" if d.weekday() >= 5 else "REGULAR"

    def is_trading_day(self, d: date) -> bool:
        return self.day_type(d) == "REGULAR"

    def holiday_name(self, d: date):
        e = (self.data or {}).get("days", {}).get(d.isoformat())
        return e["name"] if e else None

    def verified(self, d: date) -> bool:
        """True when the official list covers this date's year."""
        return self.covers(d.year)


_default = None


def default_calendar() -> NSECalendar:
    global _default
    if _default is None:
        _default = NSECalendar()
    return _default

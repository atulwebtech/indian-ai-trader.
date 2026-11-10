from datetime import date, datetime

import pandas as pd
import pytest
import requests

from config.settings import IST
from market_data import calendar as cal
from market_data.calendar import CalendarError, NSECalendar
from market_data.loader import data_status, market_state
from market_data.mock_adapter import MockAdapter
from risk_management.gate import SignalGate

DATA = {"years": [2026], "days": {"2026-10-20": {"name": "Dussehra", "special": False},
                                  "2026-11-08": {"name": "Diwali Laxmi Pujan", "special": True},
                                  "2026-01-26": {"name": "Republic Day", "special": False}}}
C = NSECalendar(data=DATA)


def at(s):
    return pd.Timestamp(s, tz=IST).to_pydatetime()


def test_day_types():
    assert C.day_type(date(2026, 10, 20)) == "HOLIDAY"
    assert C.day_type(date(2026, 11, 8)) == "SPECIAL" and not C.is_trading_day(date(2026, 11, 8))
    assert C.day_type(date(2026, 10, 31)) == "WEEKEND"
    assert C.is_trading_day(date(2026, 10, 30))
    assert C.holiday_name(date(2026, 10, 20)) == "Dussehra"


def test_unverified_when_year_not_covered_or_missing_file(tmp_path):
    assert C.verified(date(2026, 3, 3)) and not C.verified(date(2027, 3, 3))
    empty = NSECalendar(path=tmp_path / "none.json")
    assert not empty.verified(date(2026, 3, 3)) and empty.day_type(date(2026, 3, 4)) == "REGULAR"   # weekday fallback
    assert empty.day_type(date(2026, 3, 7)) == "WEEKEND"


def test_market_state_uses_calendar_and_clock():
    assert market_state(at("2026-10-30 10:00"), C) == "OPEN"
    assert market_state(at("2026-10-30 08:00"), C) == "PRE_MARKET"
    assert market_state(at("2026-10-30 15:30"), C) == "CLOSED"
    assert market_state(at("2026-10-20 10:00"), C) == "HOLIDAY"
    assert market_state(at("2026-11-08 10:00"), C) == "SPECIAL_SESSION"
    assert market_state(at("2026-10-31 10:00"), C) == "CLOSED"


def test_holiday_means_no_signals_and_no_stale_alarm():
    g = SignalGate(calendar=C)
    why = g.blocks("NIFTY", at("2026-10-20 10:30"))
    assert why and "holiday" in why[0].lower()
    assert any("special" in w.lower() for w in g.blocks("NIFTY", at("2026-11-08 10:30")))
    intra = MockAdapter().get_candles("NIFTY", "15m", 3)
    assert data_status(intra, at("2026-10-20 11:00"), C)["status"] == "CLOSED"   # holiday: old data is not 'stale'


def test_fetch_parses_nse_format_and_flags_special():
    class R:
        def __init__(self, js): self.js = js
        def raise_for_status(self): pass
        def json(self): return self.js
    class S:
        def get(self, url, **k):
            if "api" in url:
                return R({"FO": [{"tradingDate": "26-Jan-2026", "description": "Republic Day"},
                                 {"tradingDate": "08-Nov-2026", "description": "Diwali Laxmi Pujan*"}]})
            return R({})
    d = cal.fetch_from_nse(S())
    assert d["years"] == [2026] and d["days"]["2026-01-26"]["special"] is False
    assert d["days"]["2026-11-08"] == {"name": "Diwali Laxmi Pujan", "special": True}


def test_fetch_failure_is_a_clear_error():
    class S:
        def get(self, *a, **k): raise requests.ConnectionError("no net")
    with pytest.raises(CalendarError, match="Could not fetch"):
        cal.fetch_from_nse(S())
    class Empty:
        def get(self, url, **k):
            class R:
                def raise_for_status(s): pass
                def json(s): return {"FO": []}
            return R()
    with pytest.raises(CalendarError):
        cal.fetch_from_nse(Empty())


def test_unfinished_candle_is_never_analysed(monkeypatch):
    """The 10:00 candle is still forming at 10:02 and must not be returned; today's daily bar must not be used before the close."""
    from market_data import factory, loader
    from market_data.base import MarketDataAdapter

    base = MockAdapter().get_candles("NIFTY", "15m", 3)
    day = base.index[-1].date()
    intra = base[base.index.date == day]
    daily = MockAdapter().get_candles("NIFTY", "1d", 30)

    class Fake(MarketDataAdapter):
        source = "HISTORICAL"
        def get_candles(self, inst, tf, days):
            return (intra if tf == "15m" else daily).copy()
    monkeypatch.setattr(factory, "get_adapter", lambda mode: Fake())
    monkeypatch.setattr("market_data.cache.save_merged", lambda df, i, t: df)
    now = pd.Timestamp(f"{day} 10:02", tz=IST).to_pydatetime()
    got, d = loader.load_data("NIFTY", "HISTORICAL", now=now)
    assert got.index[-1] == pd.Timestamp(f"{day} 09:45", tz=IST)          # last CLOSED candle (09:45-10:00)
    assert (got.index + pd.Timedelta(minutes=15) <= now).all()
    assert d.index[-1].date() < day                                          # no 'today' daily bar mid-session
    after, d2 = loader.load_data("NIFTY", "HISTORICAL", now=pd.Timestamp(f"{day} 15:45", tz=IST).to_pydatetime())
    assert after.index[-1] == intra.index[-1] and d2.index[-1].date() == day

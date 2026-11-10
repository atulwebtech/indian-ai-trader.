"""Shared data loading for the CLI, Telegram bot and dashboard."""
from datetime import datetime, time, timedelta

from config.settings import IST, MARKET_CLOSE, MARKET_OPEN
from market_data.calendar import default_calendar

STALE_AFTER_MINUTES = 30   # a 15m bar older than this (plus its own length) during market hours is stale


def drop_unfinished(df, now: datetime, minutes: int):
    """Remove a candle that is still forming. Data feeds include the current, unfinished candle; analysing it as if it
    had closed would let signals use a price that can still change (and that the backtests never saw)."""
    return df[df.index + timedelta(minutes=minutes) <= now]


def load_data(instrument: str, mode: str, now: datetime = None):
    """Return (15m candles, daily candles), refreshing the local cache when using real data. Only CLOSED candles."""
    from market_data.cache import save_merged
    from market_data.factory import get_adapter
    ad = get_adapter(mode)
    intra = ad.get_candles(instrument, "15m", 59)
    daily = ad.get_candles(instrument, "1d", 400)
    attrs = dict(intra.attrs)
    if mode == "HISTORICAL":
        intra = save_merged(intra, instrument, "15m")
        daily = save_merged(daily, instrument, "1d")
    if mode != "MOCK":
        intra = drop_unfinished(intra, now or datetime.now(IST), 15)
        today = (now or datetime.now(IST)).date()
        daily = daily[daily.index.date < today] if (now or datetime.now(IST)).time() < MARKET_CLOSE else daily
    intra.attrs.update(attrs)
    intra.attrs.update(instrument=instrument, timeframe="15m", source=ad.source)
    return intra, daily


def market_state(now: datetime, calendar=None) -> str:
    """OPEN / PRE_MARKET / CLOSED / HOLIDAY / SPECIAL_SESSION, from the NSE calendar plus the clock.
    Without an official list covering this year the calendar only knows weekends (see calendar.verified)."""
    cal = calendar or default_calendar()
    kind = cal.day_type(now.date())
    if kind == "HOLIDAY":
        return "HOLIDAY"
    if kind == "SPECIAL":
        return "SPECIAL_SESSION"
    if kind == "WEEKEND":
        return "CLOSED"
    t = now.time()
    if MARKET_OPEN <= t < MARKET_CLOSE:
        return "OPEN"
    return "PRE_MARKET" if t < MARKET_OPEN else "CLOSED"


def data_status(intra, now: datetime, calendar=None) -> dict:
    """How fresh is the newest candle? FRESH / STALE (market open but data old) / CLOSED."""
    last_end = intra.index[-1] + timedelta(minutes=15)
    age = (now - last_end).total_seconds() / 60
    state = market_state(now, calendar)
    if state == "OPEN":
        status = "FRESH" if age <= STALE_AFTER_MINUTES else "STALE"
    else:
        status = "CLOSED"
    return {"status": status, "last_candle_end": last_end, "age_minutes": round(age, 1), "market": state,
            "has_today": intra.index[-1].date() == now.date()}

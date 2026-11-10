"""Historical base rates used to put numbers on targets. These describe the PAST frequency of a
move; they are not the probability that a particular target will be hit today."""
import numpy as np
import pandas as pd

from indicators.volatility import atr

MIN_SAMPLES = 30


def excursion_samples(daily: pd.DataFrame, session_date) -> tuple:
    """Per past day: (high-open)/ATR_prev and (open-low)/ATR_prev. ATR is from the day BEFORE,
    so no look-ahead. Only days before `session_date` are used."""
    d = daily[daily.index.date < session_date]
    prev_atr = atr(d).shift()
    up = ((d["high"] - d["open"]) / prev_atr).dropna().to_numpy()
    dn = ((d["open"] - d["low"]) / prev_atr).dropna().to_numpy()
    return up, dn


def reach_rate(samples: np.ndarray, z: float):
    """Share of past days whose excursion was >= z daily-ATRs. None if too little history."""
    if len(samples) < MIN_SAMPLES:
        return None
    return float((samples >= z).mean())


def _bucket(ts: pd.Timestamp) -> str:
    h = ts.hour
    return "09:15-10:00" if h == 9 else f"{h:02d}:00-{h + 1:02d}:00"


def time_of_extremes(intraday: pd.DataFrame, min_days: int = 10, full_day_share: float = 0.9):
    """Where in the session the day's high and low usually form (complete days only)."""
    step = {"5m": 5, "15m": 15, "1h": 60}.get(intraday.attrs.get("timeframe"))
    if step is None:
        return None
    expected = 375 / step
    highs, lows, days = [], [], 0
    for _, g in intraday.groupby(intraday.index.date):
        if len(g) < full_day_share * expected:
            continue
        days += 1
        highs.append(_bucket(g["high"].idxmax()))
        lows.append(_bucket(g["low"].idxmin()))
    if days < min_days:
        return None
    share = lambda xs: {k: round(v / days, 3) for k, v in pd.Series(xs).value_counts().items()}
    return {"days": days, "high_by_window": share(highs), "low_by_window": share(lows)}

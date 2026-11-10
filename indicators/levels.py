"""Key levels: previous-day levels, pivots, opening range, VWAP, support/resistance clusters."""
import numpy as np
import pandas as pd

from config.settings import OPENING_RANGE_MINUTES
from indicators.trend import swing_points
from indicators.volatility import atr
from indicators.volume import has_volume

BAR_MINUTES = {"5m": 5, "15m": 15, "1h": 60}


def previous_day_levels(daily: pd.DataFrame, session_date) -> dict:
    prev = daily[daily.index.date < session_date]
    if prev.empty:
        return {}
    r = prev.iloc[-1]
    return {"date": str(prev.index[-1].date()), "prev_high": float(r.high),
            "prev_low": float(r.low), "prev_close": float(r.close)}


def classic_pivots(high: float, low: float, close: float) -> dict:
    p = (high + low + close) / 3
    return {"P": p, "R1": 2 * p - low, "S1": 2 * p - high,
            "R2": p + (high - low), "S2": p - (high - low),
            "R3": high + 2 * (p - low), "S3": low - 2 * (high - p)}


def opening_range(day_df: pd.DataFrame, timeframe: str, minutes: int = OPENING_RANGE_MINUTES):
    """High/low of the first `minutes` of the session. None if the range isn't complete yet
    (so it can never be used before it is known)."""
    step = BAR_MINUTES.get(timeframe)
    if step is None or step > minutes or day_df.empty:
        return None
    need = minutes // step
    first = day_df.iloc[:need]
    if len(first) < need:
        return None
    return {"or_high": float(first.high.max()), "or_low": float(first.low.min()),
            "complete_at": str(first.index[-1] + pd.Timedelta(minutes=step))}


def session_vwap(day_df: pd.DataFrame) -> pd.Series:
    """True VWAP when volume exists; otherwise NaN (we do not substitute a fake)."""
    if not has_volume(day_df):
        return pd.Series(np.nan, index=day_df.index)
    tp = (day_df.high + day_df.low + day_df.close) / 3
    return (tp * day_df.volume).cumsum() / day_df.volume.cumsum()


def session_avg_price(day_df: pd.DataFrame) -> pd.Series:
    """Time-weighted average of typical price since the open. A PROXY for VWAP on volume-less
    indices; always labelled as a proxy."""
    tp = (day_df.high + day_df.low + day_df.close) / 3
    return tp.expanding().mean()


def support_resistance(df: pd.DataFrame, n: int = 2, tol_atr: float = 0.5, top: int = 4) -> dict:
    """Cluster confirmed swing prices within tol_atr*ATR; more touches = stronger level."""
    sw = swing_points(df, n)
    a = atr(df).iloc[-1]
    if sw.empty or np.isnan(a):
        return {"supports": [], "resistances": []}
    tol = a * tol_atr
    clusters = []
    for price in sorted(sw["price"]):
        if clusters and price - np.mean(clusters[-1]) <= tol:
            clusters[-1].append(price)
        else:
            clusters.append([price])
    px = df["close"].iloc[-1]
    levels = [{"price": float(np.mean(c)), "touches": len(c)} for c in clusters]
    sup = sorted([l for l in levels if l["price"] < px], key=lambda l: -l["price"])[:top]
    res = sorted([l for l in levels if l["price"] > px], key=lambda l: l["price"])[:top]
    return {"supports": sup, "resistances": res}

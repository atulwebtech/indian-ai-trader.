"""Deterministic synthetic market data for engine tests (NOT real data; used only inside tests)."""
from datetime import datetime

import numpy as np
import pandas as pd

from config.settings import IST


def make_1m(days, today_path, seed=3, base=24850.0, amp=40.0):
    """days: list of date strings (completed sessions, oscillating in a range). today_path: list of closes for today's minutes."""
    rng = np.random.default_rng(seed)
    frames = []
    for d in days:
        idx = pd.date_range(f"{d} 09:15", periods=375, freq="1min", tz=IST)
        c = base + amp * np.sin(np.arange(375) / 22.0) + rng.normal(0, 3, 375)
        frames.append(pd.DataFrame({"close": c}, index=idx))
    return frames, rng


def finish(frames_close, rng):
    df = pd.concat(frames_close)
    c = df["close"].to_numpy()
    o = np.r_[c[0], c[:-1]]
    spread = np.abs(rng.normal(0, 2.0, len(c))) + 1
    out = pd.DataFrame({"open": o, "high": np.maximum(o, c) + spread, "low": np.minimum(o, c) - spread, "close": c}, index=df.index)
    out["volume"] = rng.integers(800, 1200, len(c)).astype(float)
    return out


def resample(df1, tf):
    m = {"1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min"}[tf]
    if tf == "1m":
        return df1
    out = df1.resample(m, origin="start_day", offset="9h15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    return out


def closed_until(df, now, minutes):
    return df[df.index + pd.Timedelta(minutes=minutes) <= now]


def breakout_scenario(direction=1, seed=3):
    """5 quiet range-bound sessions, then today: flat open, then a fast high-volume move through the previous day's extreme."""
    days = ["2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
    frames, rng = make_1m(days, None, seed=seed)
    n = 375
    flat = np.sin(np.arange(65) / 5) * 8
    move = np.linspace(0, 75 * direction, 25)
    tail = 75 * direction + np.cumsum(rng.normal(0.25 * direction, 1.5, n - 90))
    idx = pd.date_range("2026-11-06 09:15", periods=n, freq="1min", tz=IST)
    frames.append(pd.DataFrame({"close": 24850 + np.r_[flat, np.zeros(0), move, tail]}, index=idx))
    df1 = finish(frames, rng)
    burst = (df1.index >= pd.Timestamp("2026-11-06 10:20", tz=IST)) & (df1.index < pd.Timestamp("2026-11-06 10:45", tz=IST))
    df1.loc[burst, "volume"] *= 2.6
    wide = df1.loc[burst]
    df1.loc[burst, "high"] = wide["high"] + 3
    df1.loc[burst, "low"] = wide["low"] - 3
    return df1

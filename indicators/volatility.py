"""Volatility: ATR (Wilder), range expansion/contraction, intraday realised volatility."""
import numpy as np
import pandas as pd


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift()
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def range_state(df: pd.DataFrame, short: int = 5, long: int = 20) -> str:
    """EXPANSION if recent average bar range is >1.25x the longer average, CONTRACTION if <0.75x."""
    rng = df["high"] - df["low"]
    if len(rng) < long:
        return "UNKNOWN"
    ratio = rng.tail(short).mean() / rng.tail(long).mean()
    return "EXPANSION" if ratio > 1.25 else "CONTRACTION" if ratio < 0.75 else "NORMAL"


def realised_vol_pct(df: pd.DataFrame, bars: int = 20) -> float:
    """Std-dev of bar-to-bar log returns over the last `bars` bars, in percent per bar."""
    r = np.log(df["close"]).diff().tail(bars)
    return float(r.std() * 100) if r.notna().sum() > 2 else float("nan")


def volatility_summary(df: pd.DataFrame, daily: pd.DataFrame = None) -> dict:
    a = atr(df).iloc[-1]
    out = {"atr": None if np.isnan(a) else float(a), "range_state": range_state(df),
           "realised_vol_pct_per_bar": realised_vol_pct(df)}
    if daily is not None and len(daily) > 15:
        da = atr(daily).iloc[-1]
        out["daily_atr"] = None if np.isnan(da) else float(da)
    return out

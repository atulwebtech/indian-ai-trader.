"""Additional indicators for the option signal engine: VWAP, EMA 200, Bollinger, volume MA, Supertrend.

Index spot has no volume. VWAP is only computed on days where volume exists (futures volume attached to the
candles); otherwise it is NaN and callers must treat it as UNAVAILABLE rather than guess.
"""
import numpy as np
import pandas as pd

from indicators.trend import ema
from indicators.volatility import atr


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP, reset every day. NaN for any day without traded volume."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].fillna(0)
    day = df.index.date
    pv = (tp * vol).groupby(day).cumsum()
    cv = vol.groupby(day).cumsum()
    return (pv / cv.replace(0, np.nan)).rename("vwap")


def ema_set(df: pd.DataFrame, spans=(9, 20, 50, 200)) -> pd.DataFrame:
    out = pd.DataFrame({f"ema{s}": ema(df["close"], s) for s in spans})
    for s in spans:                              # an EMA with too little history is not reliable
        out.loc[out.index[: max(s - 1, 0)], f"ema{s}"] = np.nan
    return out


def bollinger(close: pd.Series, period: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + k * sd, "lower": mid - k * sd,
                         "width_pct": 100 * (2 * k * sd) / mid})


def volume_ma(df: pd.DataFrame, window: int = 20) -> pd.Series:
    return df["volume"].rolling(window).mean()


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """Classic Supertrend. direction: +1 up-trend, -1 down-trend."""
    a = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2
    ub, lb = (hl2 + mult * a).to_numpy(), (hl2 - mult * a).to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    fub, flb, st, dr = ub.copy(), lb.copy(), np.full(n, np.nan), np.zeros(n, dtype=int)
    for i in range(1, n):
        if np.isnan(ub[i]) or np.isnan(ub[i - 1]):
            continue
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or close[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or close[i - 1] < flb[i - 1]) else flb[i - 1]
        prev = dr[i - 1] or (1 if close[i - 1] >= (fub[i - 1] + flb[i - 1]) / 2 else -1)
        dr[i] = 1 if (prev == -1 and close[i] > fub[i - 1]) else -1 if (prev == 1 and close[i] < flb[i - 1]) else prev
        st[i] = flb[i] if dr[i] == 1 else fub[i]
    return pd.DataFrame({"supertrend": st, "direction": dr}, index=df.index)

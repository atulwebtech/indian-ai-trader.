"""Momentum: RSI (Wilder), MACD, and swing-based RSI divergence."""
import numpy as np
import pandas as pd

from indicators.trend import ema, swing_points


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    out[(loss == 0) & (gain > 0)] = 100.0
    out[(loss == 0) & (gain == 0)] = 50.0
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def divergence(df: pd.DataFrame, n: int = 2, lookback_swings: int = 2) -> dict:
    """Regular RSI divergence on the last two confirmed swings.

    bullish: price lower low, RSI higher low.  bearish: price higher high, RSI lower high.
    """
    r = rsi(df["close"])
    sw = swing_points(df, n)
    res = {"bullish": False, "bearish": False}
    lows = sw[sw.kind == "low"].tail(lookback_swings)
    highs = sw[sw.kind == "high"].tail(lookback_swings)
    if len(lows) == 2:
        (p1, p2), (r1, r2) = lows["price"].to_numpy(), r.iloc[lows["pos"].to_numpy()].to_numpy()
        res["bullish"] = bool(p2 < p1 and r2 > r1 and not np.isnan(r1))
    if len(highs) == 2:
        (p1, p2), (r1, r2) = highs["price"].to_numpy(), r.iloc[highs["pos"].to_numpy()].to_numpy()
        res["bearish"] = bool(p2 > p1 and r2 < r1 and not np.isnan(r1))
    return res


def momentum_summary(df: pd.DataFrame) -> dict:
    r = rsi(df["close"]).iloc[-1]
    m = macd(df["close"]).iloc[-1]
    return {
        "rsi": None if np.isnan(r) else float(r),
        "rsi_zone": "UNKNOWN" if np.isnan(r) else ("OVERBOUGHT" if r >= 70 else "OVERSOLD" if r <= 30 else "NEUTRAL"),
        "macd": float(m["macd"]), "macd_signal": float(m["signal"]), "macd_hist": float(m["hist"]),
        "macd_bias": "BULLISH" if m["hist"] > 0 else "BEARISH" if m["hist"] < 0 else "NEUTRAL",
        "divergence": divergence(df),
    }

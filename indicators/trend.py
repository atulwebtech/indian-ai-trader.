"""Trend indicators: EMAs, swing points, market structure, ADX.

Swing points are fractals confirmed only `n` bars AFTER they occur, so using them at bar t
never peeks at the future. `confirmed_at` holds the index position where each became known.
"""
import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def ema_stack(df: pd.DataFrame, spans=(9, 20, 50)) -> pd.DataFrame:
    return pd.DataFrame({f"ema{s}": ema(df["close"], s) for s in spans})


def swing_points(df: pd.DataFrame, n: int = 2) -> pd.DataFrame:
    """Confirmed swing highs/lows. Columns: pos, price, kind ('high'|'low'), confirmed_at."""
    hi, lo = df["high"].to_numpy(), df["low"].to_numpy()
    rows = []
    for i in range(n, len(df) - n):
        win_h, win_l = hi[i - n:i + n + 1], lo[i - n:i + n + 1]
        if hi[i] == win_h.max() and (win_h == hi[i]).sum() == 1:
            rows.append((i, hi[i], "high", i + n))
        if lo[i] == win_l.min() and (win_l == lo[i]).sum() == 1:
            rows.append((i, lo[i], "low", i + n))
    return pd.DataFrame(rows, columns=["pos", "price", "kind", "confirmed_at"])


def market_structure(swings: pd.DataFrame) -> dict:
    """Compare the last two confirmed swing highs and lows."""
    highs = swings[swings.kind == "high"]["price"].to_numpy()
    lows = swings[swings.kind == "low"]["price"].to_numpy()
    out = {"higher_high": None, "higher_low": None, "lower_high": None, "lower_low": None,
           "structure": "UNDEFINED"}
    if len(highs) >= 2:
        out["higher_high"], out["lower_high"] = bool(highs[-1] > highs[-2]), bool(highs[-1] < highs[-2])
    if len(lows) >= 2:
        out["higher_low"], out["lower_low"] = bool(lows[-1] > lows[-2]), bool(lows[-1] < lows[-2])
    if out["higher_high"] and out["higher_low"]:
        out["structure"] = "UPTREND"
    elif out["lower_high"] and out["lower_low"]:
        out["structure"] = "DOWNTREND"
    elif out["higher_high"] is not None and out["higher_low"] is not None:
        out["structure"] = "MIXED"
    return out


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder ADX with +DI / -DI."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    prev_close = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    a = 1 / period
    atr_ = tr.ewm(alpha=a, adjust=False, min_periods=period).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=a, adjust=False, min_periods=period).mean() / atr_
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=a, adjust=False, min_periods=period).mean() / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return pd.DataFrame({"adx": dx.ewm(alpha=a, adjust=False, min_periods=period).mean(),
                         "plus_di": pdi, "minus_di": mdi})


def trend_summary(df: pd.DataFrame) -> dict:
    """EMA alignment + structure + ADX strength for the latest bar."""
    st = ema_stack(df)
    last = st.iloc[-1]
    px = df["close"].iloc[-1]
    if px > last.ema9 > last.ema20 > last.ema50:
        align = "BULLISH"
    elif px < last.ema9 < last.ema20 < last.ema50:
        align = "BEARISH"
    else:
        align = "MIXED"
    strength = adx(df)["adx"].iloc[-1]
    label = "UNKNOWN" if np.isnan(strength) else ("STRONG" if strength >= 25 else "WEAK" if strength < 20 else "MODERATE")
    return {"ema_alignment": align, "emas": {k: float(v) for k, v in last.items()},
            "adx": None if np.isnan(strength) else float(strength), "trend_strength": label,
            **market_structure(swing_points(df))}

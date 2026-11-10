"""Candle patterns and breakout logic on CLOSED candles only. Every function returns plain Python values."""
import numpy as np
import pandas as pd


def _parts(r):
    rng = max(float(r["high"] - r["low"]), 1e-9)
    body = abs(float(r["close"] - r["open"]))
    up = float(r["high"] - max(r["open"], r["close"]))
    lo = float(min(r["open"], r["close"]) - r["low"])
    return rng, body, up, lo


def candle_patterns(df: pd.DataFrame) -> list:
    """Patterns formed by the LAST candle (needs at least 2 candles)."""
    if len(df) < 2:
        return []
    cur, prev = df.iloc[-1], df.iloc[-2]
    rng, body, up, lo = _parts(cur)
    prng, pbody, _, _ = _parts(prev)
    out = []
    bull, bear = cur["close"] > cur["open"], cur["close"] < cur["open"]
    pbull, pbear = prev["close"] > prev["open"], prev["close"] < prev["open"]
    if bull and pbear and cur["close"] >= prev["open"] and cur["open"] <= prev["close"] and body > pbody:
        out.append("BULLISH_ENGULFING")
    if bear and pbull and cur["close"] <= prev["open"] and cur["open"] >= prev["close"] and body > pbody:
        out.append("BEARISH_ENGULFING")
    if body / rng <= 0.1:
        out.append("DOJI")
    if lo >= 2 * max(body, 1e-9) and up <= 0.3 * rng and body / rng > 0.1:
        out.append("HAMMER")
    if up >= 2 * max(body, 1e-9) and lo <= 0.3 * rng and body / rng > 0.1:
        out.append("SHOOTING_STAR")
    if lo >= 0.6 * rng and body / rng <= 0.3:
        out.append("BULLISH_PIN_BAR")
    if up >= 0.6 * rng and body / rng <= 0.3:
        out.append("BEARISH_PIN_BAR")
    if cur["high"] < prev["high"] and cur["low"] > prev["low"]:
        out.append("INSIDE_BAR")
    return out


def rejection_at(df: pd.DataFrame, level: float, side: str, tol: float) -> bool:
    """Last candle probed `level` and closed back on the right side with a wick: side 'SUPPORT' or 'RESISTANCE'."""
    r = df.iloc[-1]
    _, body, up, lo = _parts(r)
    if side == "SUPPORT":
        return bool(r["low"] <= level + tol and r["close"] > level and lo >= body)
    return bool(r["high"] >= level - tol and r["close"] < level and up >= body)


def breakout_close(df: pd.DataFrame, level: float, direction: int, max_age: int = 2):
    """Closed-candle breakout of `level` (+1 up, -1 down). Returns None or
    {'age': bars since the first close beyond the level, 'bar': that candle, 'extension': distance now}."""
    c = df["close"].to_numpy()
    beyond = c > level if direction > 0 else c < level
    if not beyond[-1]:
        return None
    first = len(c) - 1
    while first - 1 >= 0 and beyond[first - 1]:
        first -= 1
    age = len(c) - 1 - first
    if age > max_age:
        return None
    return {"age": age, "bar": df.iloc[first], "extension": float(direction * (c[-1] - level))}


def false_breakout(df: pd.DataFrame, level: float, direction: int, lookback: int = 4) -> bool:
    """A recent bar poked beyond `level` (direction +1: above) but the last close is back inside: a trap against `direction`."""
    seg = df.iloc[-lookback:]
    poked = (seg["high"] > level).any() if direction > 0 else (seg["low"] < level).any()
    back = df["close"].iloc[-1] < level if direction > 0 else df["close"].iloc[-1] > level
    closed_beyond = ((seg["close"] > level).any() if direction > 0 else (seg["close"] < level).any())
    return bool(poked and back and closed_beyond)


def retest_hold(df: pd.DataFrame, level: float, direction: int, tol: float, window: int = 8) -> bool:
    """Broke `level` earlier, pulled back to within `tol` of it, and the last candle closed back beyond it as a bounce."""
    if len(df) < 3:
        return False
    seg = df.iloc[-window:]
    c = seg["close"].to_numpy()
    beyond = c > level if direction > 0 else c < level
    if not beyond.any() or not beyond[-1]:
        return False
    first = int(np.argmax(beyond))
    after = seg.iloc[first + 1:]
    if len(after) < 2:
        return False
    touched = (after["low"] <= level + tol) if direction > 0 else (after["high"] >= level - tol)
    failed = (after["close"] < level - tol) if direction > 0 else (after["close"] > level + tol)
    last = seg.iloc[-1]
    bounce = last["close"] > last["open"] if direction > 0 else last["close"] < last["open"]
    return bool(touched.any() and not failed.any() and bounce)


# ---------- multi-candle reversal patterns, divergence, liquidity sweep, structure (BOS / CHoCH) ----------
def star_patterns(df: pd.DataFrame) -> list:
    """Morning / evening star on the last three CLOSED candles."""
    if len(df) < 3:
        return []
    a, b, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    ra, rb = max(a["high"] - a["low"], 1e-9), max(b["high"] - b["low"], 1e-9)
    big_a = abs(a["close"] - a["open"]) / ra >= 0.5
    small_b = abs(b["close"] - b["open"]) / rb <= 0.3
    mid_a = (a["open"] + a["close"]) / 2
    out = []
    if a["close"] < a["open"] and big_a and small_b and c["close"] > c["open"] and c["close"] > mid_a:
        out.append("MORNING_STAR")
    if a["close"] > a["open"] and big_a and small_b and c["close"] < c["open"] and c["close"] < mid_a:
        out.append("EVENING_STAR")
    return out


def rsi_divergence(df: pd.DataFrame, rsi: pd.Series, n: int = 2) -> str:
    """BULLISH / BEARISH / NONE from the last two CONFIRMED swing lows/highs (price vs RSI). No look-ahead."""
    from indicators.trend import swing_points
    sw = swing_points(df, n)
    if sw.empty:
        return "NONE"
    lows = sw[sw.kind == "low"].tail(2)
    highs = sw[sw.kind == "high"].tail(2)
    if len(lows) == 2:
        (i0, p0), (i1, p1) = [(int(x.pos), float(x.price)) for x in lows.itertuples()]
        if p1 < p0 and rsi.iloc[i1] > rsi.iloc[i0] and len(df) - 1 - i1 <= 12:
            return "BULLISH"
    if len(highs) == 2:
        (i0, p0), (i1, p1) = [(int(x.pos), float(x.price)) for x in highs.itertuples()]
        if p1 > p0 and rsi.iloc[i1] < rsi.iloc[i0] and len(df) - 1 - i1 <= 12:
            return "BEARISH"
    return "NONE"


def liquidity_sweep(df: pd.DataFrame, lookback: int = 20):
    """Last candle took out the prior swing extreme but closed back inside it: stop-run then reversal.
    Returns 'BULLISH_SWEEP' (swept lows, closed above), 'BEARISH_SWEEP', or None."""
    if len(df) < lookback + 1:
        return None
    prior, last = df.iloc[-lookback - 1:-1], df.iloc[-1]
    lo, hi = prior["low"].min(), prior["high"].max()
    if last["low"] < lo and last["close"] > lo:
        return "BULLISH_SWEEP"
    if last["high"] > hi and last["close"] < hi:
        return "BEARISH_SWEEP"
    return None


def structure_events(df: pd.DataFrame, n: int = 2) -> dict:
    """BOS (break of structure, trend continuation) and CHoCH (change of character, first break against the trend)
    from CONFIRMED swings and the last close. Returns {'trend', 'event', 'level'}."""
    from indicators.trend import swing_points
    sw = swing_points(df, n)
    highs, lows = sw[sw.kind == "high"], sw[sw.kind == "low"]
    out = {"trend": "UNDEFINED", "event": None, "level": None}
    if len(highs) < 2 or len(lows) < 2:
        return out
    h1, h0 = float(highs.iloc[-1].price), float(highs.iloc[-2].price)
    l1, l0 = float(lows.iloc[-1].price), float(lows.iloc[-2].price)
    close = float(df["close"].iloc[-1])
    trend = "UP" if (h1 > h0 and l1 > l0) else "DOWN" if (h1 < h0 and l1 < l0) else "MIXED"
    out["trend"] = trend
    if trend == "UP":
        if close > h1:
            out.update(event="BOS_UP", level=h1)
        elif close < l1:
            out.update(event="CHOCH_DOWN", level=l1)
    elif trend == "DOWN":
        if close < l1:
            out.update(event="BOS_DOWN", level=l1)
        elif close > h1:
            out.update(event="CHOCH_UP", level=h1)
    return out


def consolidation_breakout(df: pd.DataFrame, bars: int = 12, max_range_atr: float = 2.5):
    """Tight range over the previous `bars` candles (excluding the last), broken on a close by the last candle.
    Returns (+1/-1, level) or None."""
    from indicators.volatility import atr
    if len(df) < bars + 15:
        return None
    a = atr(df, 14).iloc[-2]
    box = df.iloc[-bars - 1:-1]
    hi, lo = box["high"].max(), box["low"].min()
    if a != a or (hi - lo) > max_range_atr * a:
        return None
    c = df["close"].iloc[-1]
    return (1, float(hi)) if c > hi else (-1, float(lo)) if c < lo else None


def exhaustion(df: pd.DataFrame, rsi: pd.Series, ema20: pd.Series, atr_value: float) -> str:
    """Trend exhaustion: price stretched far from EMA20 with extreme RSI. 'UP' / 'DOWN' / 'NONE'."""
    c, e, r = df["close"].iloc[-1], ema20.iloc[-1], rsi.iloc[-1]
    if atr_value and abs(c - e) > 2.5 * atr_value:
        if c > e and r > 75:
            return "UP"
        if c < e and r < 25:
            return "DOWN"
    return "NONE"

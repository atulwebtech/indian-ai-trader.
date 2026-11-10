"""Price action: candle anatomy, rejection candles, breakouts, failed breakouts, structure shifts."""
import numpy as np
import pandas as pd

from indicators.trend import market_structure, swing_points


def candle_anatomy(row) -> dict:
    rng = row.high - row.low
    body = abs(row.close - row.open)
    if rng <= 0:
        return {"range": 0.0, "body_pct": 0.0, "upper_wick_pct": 0.0, "lower_wick_pct": 0.0, "direction": "FLAT"}
    return {"range": float(rng), "body_pct": float(body / rng),
            "upper_wick_pct": float((row.high - max(row.open, row.close)) / rng),
            "lower_wick_pct": float((min(row.open, row.close) - row.low) / rng),
            "direction": "UP" if row.close > row.open else "DOWN" if row.close < row.open else "FLAT"}


def rejection(row) -> str:
    """BEARISH_REJECTION: long upper wick (>=55% of range, small body). BULLISH_REJECTION: mirror."""
    a = candle_anatomy(row)
    if a["upper_wick_pct"] >= 0.55 and a["body_pct"] <= 0.30:
        return "BEARISH_REJECTION"
    if a["lower_wick_pct"] >= 0.55 and a["body_pct"] <= 0.30:
        return "BULLISH_REJECTION"
    return "NONE"


def engulfing(prev, cur) -> str:
    if prev.close < prev.open and cur.close > cur.open and cur.close >= prev.open and cur.open <= prev.close:
        return "BULLISH_ENGULFING"
    if prev.close > prev.open and cur.close < cur.open and cur.close <= prev.open and cur.open >= prev.close:
        return "BEARISH_ENGULFING"
    return "NONE"


def breakout_state(df: pd.DataFrame, lookback: int = 20, confirm_bars: int = 3) -> dict:
    """Level = highest high / lowest low of the `lookback` bars before the recent window.

    breakout_up: latest CLOSE above the prior high.   failed_breakout_up: a close above it within
    the last `confirm_bars` bars, but the latest close is back below. Mirror for the downside.
    """
    if len(df) < lookback + confirm_bars + 1:
        return {"breakout": "UNKNOWN"}
    ref = df.iloc[-(lookback + confirm_bars):-confirm_bars]
    recent = df.iloc[-confirm_bars:]
    hi, lo = ref.high.max(), ref.low.min()
    last = df.close.iloc[-1]
    state = "NONE"
    if last > hi:
        state = "BREAKOUT_UP"
    elif last < lo:
        state = "BREAKDOWN"
    elif (recent.close.iloc[:-1] > hi).any():
        state = "FAILED_BREAKOUT_UP"
    elif (recent.close.iloc[:-1] < lo).any():
        state = "FAILED_BREAKDOWN"
    return {"breakout": state, "level_high": float(hi), "level_low": float(lo)}


def structure_shift(df: pd.DataFrame, n: int = 2) -> str:
    """BEARISH_MSS: latest close below the last confirmed swing low while structure was UPTREND.
    BULLISH_MSS: close above the last confirmed swing high while structure was DOWNTREND."""
    sw = swing_points(df, n)
    ms = market_structure(sw)
    close = df.close.iloc[-1]
    lows, highs = sw[sw.kind == "low"], sw[sw.kind == "high"]
    if ms["structure"] == "UPTREND" and not lows.empty and close < lows["price"].iloc[-1]:
        return "BEARISH_MSS"
    if ms["structure"] == "DOWNTREND" and not highs.empty and close > highs["price"].iloc[-1]:
        return "BULLISH_MSS"
    return "NONE"


def price_action_summary(df: pd.DataFrame) -> dict:
    last, prev = df.iloc[-1], df.iloc[-2]
    return {"candle": candle_anatomy(last), "rejection": rejection(last), "engulfing": engulfing(prev, last),
            **breakout_state(df), "structure_shift": structure_shift(df)}

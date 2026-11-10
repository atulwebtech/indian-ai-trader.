"""Previous-session analysis and today's comparison. Uses only data passed in; unavailable pieces say UNAVAILABLE."""
import pandas as pd

from indicators import trend as T
from indicators.extra import vwap

UNAVAILABLE = "UNAVAILABLE"
GAP_FLAT_PCT = 0.15


def _swings(df15: pd.DataFrame):
    sw = T.swing_points(df15, 2)
    highs = [round(float(r.price), 2) for r in sw.itertuples() if r.kind == "high"]
    lows = [round(float(r.price), 2) for r in sw.itertuples() if r.kind == "low"]
    return highs, lows


def analyze_previous_day(intraday: pd.DataFrame, daily: pd.DataFrame, today) -> dict:
    """intraday: 5m (or finer) candles incl. several past sessions (volume = futures volume where it exists).
    daily: daily candles. today: datetime.date of the current session. Returns {} -> {'available': False, ...} if no data."""
    past_days = sorted({d for d in intraday.index.date if d < today})
    if not past_days:
        if daily is not None and len(daily):
            d = daily[daily.index.date < today]
            if len(d):
                r = d.iloc[-1]
                return {"available": True, "date": str(d.index[-1].date()), "open": float(r.open), "high": float(r.high),
                        "low": float(r.low), "close": float(r.close), "volume": UNAVAILABLE, "vwap": UNAVAILABLE,
                        "trend": "UP" if r.close > r.open else "DOWN", "intraday_detail": UNAVAILABLE}
        return {"available": False, "reason": "No previous session in the data"}
    pd_ = past_days[-1]
    p = intraday[intraday.index.date == pd_]
    o, h, l, c = float(p.iloc[0].open), float(p.high.max()), float(p.low.min()), float(p.iloc[-1].close)
    vol = p["volume"].fillna(0).sum()
    vw = vwap(p)
    vwv = float(vw.dropna().iloc[-1]) if vw.notna().any() else UNAVAILABLE
    p15 = p.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    highs, lows = _swings(p15) if len(p15) >= 5 else ([], [])
    rng = h - l
    body_pct = (c - o) / o * 100
    close_pos = (c - l) / rng if rng else 0.5
    if abs(c - o) < 0.2 * rng:
        trend = "SIDEWAYS"
    else:
        trend = "UP" if c > o else "DOWN"
    last_hour = p.iloc[-12:] if len(p) >= 12 else p
    mom = float(last_hour.iloc[-1].close - last_hour.iloc[0].open)
    bulls, bears = int((p.close > p.open).sum()), int((p.close < p.open).sum())
    return {"available": True, "date": str(pd_), "open": o, "high": h, "low": l, "close": c, "range": round(rng, 2),
            "change_pct": round(body_pct, 2), "close_position_in_range": round(close_pos, 2),
            "volume": float(vol) if vol > 0 else UNAVAILABLE, "vwap": round(vwv, 2) if vwv != UNAVAILABLE else UNAVAILABLE,
            "trend": trend, "closing_momentum": "UP" if mom > 0.1 * rng else "DOWN" if mom < -0.1 * rng else "FLAT",
            "closing_hour_move": round(mom, 2), "bullish_candles": bulls, "bearish_candles": bears,
            "resistance_levels": sorted(set(highs))[-3:], "support_levels": sorted(set(lows))[:3],
            "breakout_level": h, "breakdown_level": l, "intraday_detail": "5m candles"}


def compare_with_today(prev: dict, today_df: pd.DataFrame, price: float) -> dict:
    """Gap, PDH/PDL position and whether today confirms yesterday's trend. today_df = today's closed candles (may be empty)."""
    if not prev.get("available") or today_df is None or today_df.empty:
        return {"available": False}
    t_open = float(today_df.iloc[0].open)
    gap = (t_open - prev["close"]) / prev["close"] * 100
    gtype = "GAP_UP" if gap > GAP_FLAT_PCT else "GAP_DOWN" if gap < -GAP_FLAT_PCT else "FLAT_OPEN"
    pos = "ABOVE_PDH" if price > prev["high"] else "BELOW_PDL" if price < prev["low"] else "INSIDE_PREV_RANGE"
    t_hi, t_lo = float(today_df.high.max()), float(today_df.low.min())
    today_dir = "UP" if price > t_open else "DOWN" if price < t_open else "FLAT"
    if prev["trend"] == "SIDEWAYS":
        verdict = "NEUTRAL (previous day sideways)"
    else:
        verdict = "CONFIRMS" if prev["trend"] == today_dir else "REJECTS" if today_dir != "FLAT" else "NEUTRAL"
    return {"available": True, "today_open": t_open, "gap_pct": round(gap, 2), "gap_type": gtype, "position": pos,
            "pdh_broken": t_hi > prev["high"], "pdl_broken": t_lo < prev["low"], "today_high": t_hi, "today_low": t_lo,
            "today_direction": today_dir, "vs_previous_trend": verdict, "price": price}

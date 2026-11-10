"""Level map for the option engine: CPR, pivots R1-R3/S1-S3, Fibonacci (secondary), per-session summaries for the last N
sessions, and repeated support/resistance zones. Everything is computed from candles passed in; nothing is invented."""
import pandas as pd

from indicators.levels import classic_pivots, support_resistance
from indicators.volatility import atr
from indicators.extra import vwap


def cpr_and_pivots(prev_high: float, prev_low: float, prev_close: float) -> dict:
    p = (prev_high + prev_low + prev_close) / 3
    bc = (prev_high + prev_low) / 2
    tc = 2 * p - bc
    rng = prev_high - prev_low
    piv = classic_pivots(prev_high, prev_low, prev_close)
    top, bot = max(tc, bc), min(tc, bc)
    return {"pivot": p, "cpr_top": top, "cpr_bottom": bot, "cpr_width_pct": round(100 * (top - bot) / p, 3),
            "pivots": {k: round(float(v), 2) for k, v in piv.items()}}


def fibonacci(swing_low: float, swing_high: float, direction: str) -> dict:
    """Retracement levels of the last swing. SECONDARY confluence only. direction = direction of the swing ('UP'/'DOWN')."""
    rng = swing_high - swing_low
    ratios = (0.382, 0.5, 0.618)
    if direction == "UP":
        return {f"fib_{r}": round(swing_high - r * rng, 2) for r in ratios}
    return {f"fib_{r}": round(swing_low + r * rng, 2) for r in ratios}


def session_summaries(intraday: pd.DataFrame, n: int = 5, today=None) -> list:
    """Summary of each of the last n COMPLETED sessions present in the candles (weekends/holidays are simply absent)."""
    days = sorted({d for d in intraday.index.date if today is None or d < today})[-n:]
    out, prev_close = [], None
    for d in days:
        s = intraday[intraday.index.date == d]
        o, h, l, c = float(s.iloc[0].open), float(s.high.max()), float(s.low.min()), float(s.iloc[-1].close)
        vol = float(s["volume"].fillna(0).sum())
        vw = vwap(s)
        a = atr(s, 14)
        rng = h - l
        out.append({"date": str(d), "open": o, "high": h, "low": l, "close": c, "range": round(rng, 2),
                    "range_pct": round(100 * rng / o, 2), "volume": vol if vol > 0 else "UNAVAILABLE",
                    "vwap": round(float(vw.dropna().iloc[-1]), 2) if vw.notna().any() else "UNAVAILABLE",
                    "gap_pct": round(100 * (o - prev_close) / prev_close, 2) if prev_close else None,
                    "trend": "SIDEWAYS" if abs(c - o) < 0.2 * rng else "UP" if c > o else "DOWN",
                    "close_in_range": round((c - l) / rng, 2) if rng else 0.5,
                    "atr": round(float(a.dropna().iloc[-1]), 2) if a.notna().any() else None,
                    "bars": len(s)})
        prev_close = c
    return out


def repeated_zones(intraday: pd.DataFrame, price: float, atr_value: float, sessions: int = 20, min_touches: int = 3) -> dict:
    """Support/resistance zones that price rejected at least `min_touches` times over the last `sessions` sessions.
    Uses confirmed swings on 15m candles (no look-ahead). Zones are ranked by touches."""
    days = sorted(set(intraday.index.date))[-sessions:]
    d = intraday[intraday.index.date >= days[0]] if days else intraday
    d15 = d.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    if len(d15) < 30:
        return {"supports": [], "resistances": [], "note": "not enough history"}
    sr = support_resistance(d15, n=2, tol_atr=0.5, top=8)
    keep = lambda z: z.get("touches", 0) >= min_touches
    sup = [z for z in sr["supports"] if keep(z) and z["price"] < price]
    res = [z for z in sr["resistances"] if keep(z) and z["price"] > price]
    return {"supports": sorted(sup, key=lambda z: -z["touches"]), "resistances": sorted(res, key=lambda z: -z["touches"]),
            "sessions_used": len(days)}

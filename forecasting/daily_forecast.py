"""Full-day forecast: bias, three scenarios, target zones, reversal zones, invalidation.

All outputs are conditional estimates built from measured data. Nothing is guaranteed.
"""
import math

import pandas as pd

from config.settings import IST, MARKET_CLOSE, MARKET_OPEN
from forecasting import base_rates, ladder
from indicators import levels
from indicators.analysis import analyze
from indicators.volatility import atr

SESSION_MINUTES = 375
BAR_MINUTES = {"5m": 5, "15m": 15, "1h": 60}
MIN_FRAC = 0.10


def _remaining_fraction(today: pd.DataFrame, timeframe: str) -> float:
    if today.empty:
        return 1.0
    end = today.index[-1] + pd.Timedelta(minutes=BAR_MINUTES.get(timeframe, 0))
    close = pd.Timestamp.combine(end.date(), MARKET_CLOSE).tz_localize(IST)
    return max(MIN_FRAC, min(1.0, (close - end).total_seconds() / 60 / SESSION_MINUTES))


def _bias(snap, ref, pivots, or_, proxy):
    score, used, why = 0, 0, []

    def vote(v, text):
        nonlocal score, used
        used += 1
        score += v
        why.append((("+" if v > 0 else "-" if v < 0 else "0"), text))

    t, m = snap["trend"], snap["momentum"]
    vote({"BULLISH": 1, "BEARISH": -1}.get(t["ema_alignment"], 0), f"EMA 9/20/50 alignment is {t['ema_alignment']}")
    vote({"UPTREND": 1, "DOWNTREND": -1}.get(t["structure"], 0), f"Swing structure is {t['structure']}")
    vote({"BULLISH": 1, "BEARISH": -1}.get(m["macd_bias"], 0), f"MACD histogram is {m['macd_bias']}")
    if pivots:
        vote(1 if ref > pivots["P"] else -1, f"Price {'above' if ref > pivots['P'] else 'below'} central pivot {pivots['P']:.0f}")
    if proxy is not None:
        vote(1 if ref > proxy else -1, f"Price {'above' if ref > proxy else 'below'} session average {proxy:.0f}")
    if or_:
        if ref > or_["or_high"]:
            vote(1, f"Price above opening-range high {or_['or_high']:.0f}")
        elif ref < or_["or_low"]:
            vote(-1, f"Price below opening-range low {or_['or_low']:.0f}")
        else:
            vote(0, "Price inside the opening range")
    bias = "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "NEUTRAL"
    ratio = abs(score) / used if used else 0
    return {"bias": bias, "score": score, "of": used, "conviction": "HIGH" if ratio >= 0.7 else "MODERATE" if ratio >= 0.4 else "LOW",
            "reasons": why}


def generate_forecast(instrument: str, intraday: pd.DataFrame, daily: pd.DataFrame, session_date=None) -> dict:
    tf = intraday.attrs.get("timeframe", "15m")
    session_date = session_date or intraday.index[-1].date()
    today = intraday[intraday.index.date == session_date]
    intraday_mode = not today.empty
    hist_intra = intraday[intraday.index.date <= session_date]

    snap = analyze(hist_intra, daily[daily.index.date < session_date]) if len(hist_intra) > 60 else None
    if snap is None:
        raise ValueError("Not enough intraday history for a forecast (need > 60 bars)")
    prev = levels.previous_day_levels(daily, session_date)
    if not prev:
        raise ValueError("No previous-day data available")
    pivots = levels.classic_pivots(prev["prev_high"], prev["prev_low"], prev["prev_close"])
    or_ = levels.opening_range(today, tf) if intraday_mode else None
    ref = float(today.close.iloc[-1]) if intraday_mode else prev["prev_close"]
    frac = _remaining_fraction(today, tf)

    d_hist = daily[daily.index.date < session_date]
    daily_atr = float(atr(d_hist).iloc[-1])
    intra_atr = snap["volatility"]["atr"]
    u = daily_atr * math.sqrt(frac)           # expected remaining range unit
    band_half = round(0.25 * intra_atr, 2)

    proxy = float(levels.session_avg_price(today).iloc[-1]) if intraday_mode else None
    bias = _bias(snap, ref, pivots, or_, proxy)

    sess_hl = (float(today.high.max()), float(today.low.min())) if intraday_mode else None
    cands = ladder.collect_candidates(prev, pivots, or_, snap["support_resistance"], sess_hl)
    merged = ladder.merge(cands, tol=0.15 * intra_atr * 2, instrument=instrument)
    ups = ladder.build_ladder(merged, ref, +1, u, band_half)
    downs = ladder.build_ladder(merged, ref, -1, u, band_half)

    up_s, dn_s = base_rates.excursion_samples(daily, session_date)
    for t in ups:
        t["historical_reach_rate"] = base_rates.reach_rate(up_s, t["distance_in_u"])
    for t in downs:
        t["historical_reach_rate"] = base_rates.reach_rate(dn_s, t["distance_in_u"])

    sup_below = [m for m in merged if m["price"] < ref - 0.15 * intra_atr and m["strength"] >= 1.5]
    res_above = [m for m in merged if m["price"] > ref + 0.15 * intra_atr and m["strength"] >= 1.5]
    near_sup = max(sup_below, key=lambda m: m["price"])["price"] if sup_below else ref - 0.5 * u
    near_res = min(res_above, key=lambda m: m["price"])["price"] if res_above else ref + 0.5 * u
    near_sup, near_res = round(near_sup, 2), round(near_res, 2)

    bull_trigger = or_["or_high"] if or_ else None
    bear_trigger = or_["or_low"] if or_ else None
    trig_txt_up = (f"{tf} candle CLOSES above {bull_trigger:.0f} (opening-range high)" if or_ else
                   f"After 09:30, a {tf} candle CLOSES above the opening-range high (not known yet)")
    trig_txt_dn = (f"{tf} candle CLOSES below {bear_trigger:.0f} (opening-range low)" if or_ else
                   f"After 09:30, a {tf} candle CLOSES below the opening-range low (not known yet)")

    def conditions(side, ladder_):
        prior, out = trig_txt_up if side > 0 else trig_txt_dn, []
        inval = near_sup if side > 0 else near_res
        for i, t in enumerate(ladder_):
            need = [f"Trigger met: {prior}" if i == 0 else f"{ladder_[i - 1]['label']} zone reached and held on a closing basis",
                    f"{tf} closes stay {'above' if side > 0 else 'below'} {inval:.0f}"]
            if i == 2:
                need.append("Range expansion (bars larger than recent average) rather than a slow drift")
            out.append({"target": t["label"], "level": t["level"], "needs": need})
        return out

    bull = {"name": "BULLISH", "trigger": trig_txt_up, "trigger_level": bull_trigger, "targets": ups,
            "conditions": conditions(+1, ups), "invalidation_level": near_sup,
            "invalidation": f"A {tf} close below {near_sup:.0f} cancels the bullish scenario"}
    bear = {"name": "BEARISH", "trigger": trig_txt_dn, "trigger_level": bear_trigger, "targets": downs,
            "conditions": conditions(-1, downs), "invalidation_level": near_res,
            "invalidation": f"A {tf} close above {near_res:.0f} cancels the bearish scenario"}
    side = {"name": "SIDEWAYS", "range": [near_sup, near_res],
            "trigger": f"Price keeps rotating between {near_sup:.0f} and {near_res:.0f} without a {tf} close beyond either",
            "invalidation": f"A {tf} close outside {near_sup:.0f}-{near_res:.0f} with follow-through ends the range view"}
    primary = {"BULLISH": "BULLISH", "BEARISH": "BEARISH"}.get(bias["bias"], "SIDEWAYS")
    for sc in (bull, bear, side):
        sc["role"] = "PRIMARY" if sc["name"] == primary else "ALTERNATE"

    reversal = []
    for t in ups[:2]:
        reversal.append({"type": "POTENTIAL_TOP", "zone": [t["zone_low"], t["zone_high"]], "level": t["level"],
                         "confluence": t["sources"],
                         "needs": ["Bearish rejection candle (long upper wick) or bearish engulfing in the zone",
                                   f"{tf} close back below {t['zone_low']:.0f}", "RSI/momentum weakening or bearish divergence"]})
    for t in downs[:2]:
        reversal.append({"type": "POTENTIAL_BOTTOM", "zone": [t["zone_low"], t["zone_high"]], "level": t["level"],
                         "confluence": t["sources"],
                         "needs": ["Bullish rejection candle (long lower wick) or bullish engulfing in the zone",
                                   f"{tf} close back above {t['zone_high']:.0f}", "RSI/momentum improving or bullish divergence"]})

    return {
        "instrument": instrument, "source": intraday.attrs.get("source"), "timeframe": tf,
        "mode": "INTRADAY_UPDATE" if intraday_mode else "PRE_MARKET",
        "session_date": str(session_date), "data_as_of": snap["as_of"],
        "reference_price": round(ref, 2),
        "reference_note": "Last traded bar close" if intraday_mode else "Previous day close (pre-market reference)",
        "bias": bias, "scenarios": {"bullish": bull, "bearish": bear, "sideways": side},
        "support": near_sup, "resistance": near_res,
        "key_levels": {"previous_day": prev, "pivots": {k: round(v, 2) for k, v in pivots.items()}, "opening_range": or_},
        "opening_range_analysis": (
            {"status": "COMPLETE", **or_, "width": round(or_["or_high"] - or_["or_low"], 2),
             "width_vs_daily_atr": round((or_["or_high"] - or_["or_low"]) / daily_atr, 2),
             "price_vs_range": "ABOVE" if ref > or_["or_high"] else "BELOW" if ref < or_["or_low"] else "INSIDE"}
            if or_ else {"status": "NOT_AVAILABLE_YET"}),
        "reversal_zones": reversal,
        "volatility": {"daily_atr": round(daily_atr, 2), "intraday_atr": round(intra_atr, 2),
                       "range_state": snap["volatility"]["range_state"],
                       "expected_remaining_range": round(u, 2), "session_fraction_left": round(frac, 2),
                       "expected_range_low": round(ref - 0.5 * u, 2), "expected_range_high": round(ref + 0.5 * u, 2)},
        "typical_extreme_windows": base_rates.time_of_extremes(intraday[intraday.index.date < session_date]),
        "volume": snap["volume"], "momentum": {k: snap["momentum"][k] for k in ("rsi", "rsi_zone", "macd_bias", "divergence")},
        "global_invalidation": [
            f"Bullish view is void on a {tf} close below {near_sup:.0f}; bearish view is void on a close above {near_res:.0f}.",
            "A large unexpected news/event gap makes all levels unreliable until a new opening range forms.",
            "Targets expire at the 15:30 close; nothing carries to the next session."],
        "disclaimer": "Conditional estimates from historical data, not predictions or advice. No target is guaranteed. "
                      "'historical_reach_rate' is how often past days travelled that far, not today's probability.",
    }

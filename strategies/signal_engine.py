"""Smart signal filter: BUY / SELL / WAIT / NO TRADE.

Two setups are recognised: a fresh opening-range breakout in the direction of the evidence
("ORB") and a CONFIRMED reversal from Phase 7 ("REVERSAL"). A setup becomes BUY/SELL only when
it passes the gate, has an acceptable stop and reward:risk, and its checklist score is high
enough with several independent components agreeing.

The quality score ranks how many independent boxes are ticked. It is NOT a probability of winning.
"""
import numpy as np
import pandas as pd

from forecasting.daily_forecast import BAR_MINUTES, generate_forecast
from reversal_detection import detector as rev
from risk_management.gate import SignalGate

DISCLAIMER = ("Quality score = checklist strength, not a win probability. Paper/test signal only; "
              "no order is ever placed by this system.")


def _label(f):
    return "LIVE DATA - PAPER TRADING ONLY" if f["source"] == "LIVE" else f"PAPER MODE - {f['source']} DATA"


def _comp(name, earned, mx, note, available=True):
    return {"name": name, "earned": float(earned), "max": float(mx), "note": note, "available": available}


def _plan(s, entry_ref, stop_raw, targets, atr, p):
    """Return (plan, reject_reason). s=+1 long, -1 short."""
    risk = max(s * (entry_ref - stop_raw), p.min_risk_atr * atr)
    if risk > p.max_risk_atr * atr:
        return None, f"Stop too wide ({risk / atr:.1f} ATR > {p.max_risk_atr})"
    ahead = [t for t in targets if s * (t["level"] - entry_ref) > 0]
    if p.skip_near_targets:
        ahead = [t for t in ahead if s * (t["level"] - entry_ref) >= p.min_rr_t1 * risk]
    if len(ahead) < 2:
        return None, "Fewer than two target zones ahead of price"
    t1, t2 = ahead[0], ahead[1]
    rr1, rr2 = s * (t1["level"] - entry_ref) / risk, s * (t2["level"] - entry_ref) / risk
    if rr1 < p.min_rr_t1 or rr2 < p.min_rr_t2:
        return None, f"Reward:risk too low (T1 {rr1:.2f}, T2 {rr2:.2f}; need {p.min_rr_t1}/{p.min_rr_t2})"
    return {"stop": round(entry_ref - s * risk, 2), "risk": risk, "rr1": rr1, "rr2": rr2,
            "targets": [{"label": t1["label"], "level": t1["level"], "rr": round(rr1, 2)},
                        {"label": t2["label"], "level": t2["level"], "rr": round(rr2, 2)}]}, None


def _shared_components(s, f, plan, decision_time, p):
    t, m, vol = f["scenarios"], f["momentum"], f["volume"]
    bias = f["bias"]["bias"]
    want = "BULLISH" if s > 0 else "BEARISH"
    c = []
    c.append(_comp("bias_agreement", 15 if bias == want else 5 if bias == "NEUTRAL" else 0, 15,
                   f"Forecast bias is {bias}"))
    macd_ok = m["macd_bias"] == want
    rsi = m["rsi"]
    rsi_ok = rsi is not None and ((50 <= rsi <= 75) if s > 0 else (25 <= rsi <= 50))
    c.append(_comp("momentum", 8 * macd_ok + 7 * rsi_ok, 15, f"MACD {m['macd_bias']}, RSI {rsi:.0f}" if rsi else "RSI n/a"))
    rr_pts = 15 if plan["rr2"] >= 3 else 10 if plan["rr2"] >= 2.2 else 5
    c.append(_comp("reward_risk", rr_pts, 15, f"R:R {plan['rr1']:.2f} to T1, {plan['rr2']:.2f} to T2"))
    vs = f["volatility"]["range_state"]
    c.append(_comp("volatility", 5 * (vs in ("EXPANSION", "NORMAL")), 10, f"Range {vs}"))
    if vol.get("available"):
        c.append(_comp("volume", 10 if vol["volume_breakout"] else 0, 10, f"Relative volume {vol['relative_volume']}"))
    else:
        c.append(_comp("volume", 0, 10, "Not evaluated: spot index has no volume", available=False))
    tm = decision_time.time()
    c.append(_comp("timing", 5 if tm.hour < 13 or (tm.hour == 13 and tm.minute < 30) else 2, 5, f"Decision at {tm:%H:%M}"))
    return c


def _score(components, p):
    avail = [c for c in components if c["available"]]
    mx = sum(c["max"] for c in avail)
    score = 100 * sum(c["earned"] for c in avail) / mx if mx else 0.0
    strong = sum(1 for c in avail if c["earned"] >= 0.5 * c["max"])
    return round(score, 1), strong


def _orb_candidate(f, intraday, day, decision_time, atr, p):
    o = f["key_levels"]["opening_range"]
    if not o:
        return None, None
    close = day["close"].iloc[-1]
    after = day[day.index >= pd.Timestamp(o["complete_at"])]
    if after.empty:
        return None, "Opening range not complete"
    for s, level, tgt_key in ((+1, o["or_high"], "bullish"), (-1, o["or_low"], "bearish")):
        broke = after["close"] > level if s > 0 else after["close"] < level
        if not broke.iloc[-1]:
            continue
        first = int(np.argmax(broke.to_numpy()))
        age = len(after) - 1 - first
        if age > p.orb_max_age_bars:
            return None, f"Opening-range break is stale (older than {p.orb_max_age_bars + 1} bars)"
        if s * (close - level) > p.max_chase_atr * atr:
            return None, f"Price already {s * (close - level) / atr:.1f} ATR beyond the break: do not chase"
        brk = after.iloc[first]
        stop_raw = min(brk["low"], level - 0.5 * atr) if s > 0 else max(brk["high"], level + 0.5 * atr)
        plan, why = _plan(s, close, stop_raw, f["scenarios"][tgt_key]["targets"], atr, p)
        if plan is None:
            return None, why
        body = abs(brk["close"] - brk["open"]) / max(brk["high"] - brk["low"], 1e-9)
        width = o["width_vs_daily_atr"] if "width_vs_daily_atr" in o else f["opening_range_analysis"].get("width_vs_daily_atr", 0)
        comps = _shared_components(s, f, plan, decision_time, p)
        tr = f["scenarios"]
        ema_ok = f.get("_ema_alignment") == ("BULLISH" if s > 0 else "BEARISH")
        comps.insert(0, _comp("trend_alignment", 10 * ema_ok + 10 * (f.get("_structure") == ("UPTREND" if s > 0 else "DOWNTREND")),
                              20, f"EMA {f.get('_ema_alignment')}, structure {f.get('_structure')}"))
        comps.append(_comp("breakout_quality", 8 * (body >= 0.5) + 7 * (0.15 <= width <= 0.6), 15,
                           f"Break candle body {body:.0%}; opening range {width:.2f} of daily ATR"))
        zone = [round(level, 2), round(level + s * p.max_chase_atr * atr, 2)]
        return {"setup": "ORB", "s": s, "plan": plan, "components": comps, "entry_ref": float(close),
                "entry_zone": sorted(zone), "trigger": level,
                "reasons": [f"{'Up' if s > 0 else 'Down'}side break of opening range at {level:.0f} confirmed by a {f['timeframe']} close",
                            f"Break is {age + 1} bar(s) old"],
                "invalidation": f"A {f['timeframe']} close back {'below' if s > 0 else 'above'} {plan['stop']:.0f} (stop) cancels the idea"}, None
    return None, None


def _retest_candidate(f, intraday, day, decision_time, atr, p):
    """Break of the opening range, a pullback that TOUCHES the broken level and holds, then a bounce bar.
    Needs no look-ahead: everything is read from closed bars up to now."""
    o = f["key_levels"]["opening_range"]
    if not o:
        return None, None
    after = day[day.index >= pd.Timestamp(o["complete_at"])]
    if len(after) < 3:
        return None, None
    last = after.iloc[-1]
    for s, level, tgt_key in ((+1, o["or_high"], "bullish"), (-1, o["or_low"], "bearish")):
        broke = (after["close"] > level) if s > 0 else (after["close"] < level)
        if not broke.any():
            continue
        first = int(np.argmax(broke.to_numpy()))
        n_after = len(after) - 1 - first                      # bars since the break bar
        if n_after < 1 or n_after > p.retest_max_bars:
            continue
        seg = after.iloc[first + 1:]
        tol = p.retest_touch_atr * atr
        wrong_close = (seg["close"] < level - 0.2 * atr) if s > 0 else (seg["close"] > level + 0.2 * atr)
        if wrong_close.any():
            continue                                          # level failed: not a clean retest
        touched = (seg["low"] <= level + tol) if s > 0 else (seg["high"] >= level - tol)
        if not touched.any():
            continue
        recent = seg.iloc[-3:]
        if not (touched.iloc[-3:].any()):
            continue                                          # the touch must be fresh
        bounce = last["close"] > last["open"] and last["close"] > level if s > 0 else last["close"] < last["open"] and last["close"] < level
        if not bounce or s * (last["close"] - level) > 0.8 * atr:
            continue
        close = float(last["close"])
        ext = seg["low"].min() if s > 0 else seg["high"].max()
        stop_raw = min(ext, level) - 0.3 * atr if s > 0 else max(ext, level) + 0.3 * atr
        plan, why = _plan(s, close, stop_raw, f["scenarios"][tgt_key]["targets"], atr, p)
        if plan is None:
            return None, why
        comps = _shared_components(s, f, plan, decision_time, p)
        ema_ok = f.get("_ema_alignment") == ("BULLISH" if s > 0 else "BEARISH")
        comps.insert(0, _comp("trend_alignment", 10 * ema_ok + 10 * (f.get("_structure") == ("UPTREND" if s > 0 else "DOWNTREND")),
                              20, f"EMA {f.get('_ema_alignment')}, structure {f.get('_structure')}"))
        body = abs(last["close"] - last["open"]) / max(last["high"] - last["low"], 1e-9)
        comps.append(_comp("retest_quality", 8 * (body >= 0.4) + 7, 15,
                           f"Level held on the pullback; bounce bar body {body:.0%}"))
        return {"setup": "RETEST", "s": s, "plan": plan, "components": comps, "entry_ref": close,
                "entry_zone": sorted([round(level, 2), round(close + s * 0.3 * atr, 2)]), "trigger": level,
                "reasons": [f"Opening range {'high' if s > 0 else 'low'} {level:.0f} broke {n_after} bar(s) ago, was retested and held",
                            f"Bounce bar closed {'above' if s > 0 else 'below'} the level on the {f['timeframe']} chart"],
                "invalidation": f"A {f['timeframe']} close back {'below' if s > 0 else 'above'} {plan['stop']:.0f} (stop) cancels the idea"}, None
    return None, None


def _reversal_candidate(f, intraday, decision_time, atr, p):
    levels = rev.levels_from_forecast(f)
    r = rev.detect_reversal(intraday, levels)
    pr = r["primary"]
    if pr["state"] != rev.CONFIRMED:
        return None, pr
    s = -1 if pr["direction"] == "BEARISH" else +1
    close = float(intraday["close"].iloc[-1])
    stop_raw = pr["extreme"] + s * -1 * rev.TOUCH_TOL_ATR * atr   # beyond the extreme
    tgt_key = "bearish" if s < 0 else "bullish"
    plan, why = _plan(s, close, stop_raw, f["scenarios"][tgt_key]["targets"], atr, p)
    if plan is None:
        return None, why
    frac = pr["evidence_met"] / max(pr["evidence_available"], 1)
    comps = _shared_components(s, f, plan, decision_time, p)
    comps.insert(0, _comp("confirmation_depth", round(25 * frac, 1) + 0, 25,
                          f"{pr['evidence_met']}/{pr['evidence_available']} confirmations + structural break"))
    comps.append(_comp("level_quality", 10 if pr["level"] else 0, 10,
                       f"Level: {pr['level']['label'] if pr['level'] else 'none'}"))
    comps.append(_comp("prior_move", 10 if (f["momentum"]["rsi_zone"] != "NEUTRAL" or
                                             f["bias"]["bias"] != ("BULLISH" if s < 0 else "BEARISH")) else 0, 10,
                       "Something to reverse (extended move)"))
    zone = [round(close - 0.25 * atr, 2), round(close + 0.25 * atr, 2)]
    return {"setup": "REVERSAL", "s": s, "plan": plan, "components": comps, "entry_ref": close, "entry_zone": zone,
            "trigger": pr["level"]["price"] if pr["level"] else None,
            "reasons": [f"Confirmed {pr['direction'].lower()} reversal at {pr['level']['label'] if pr['level'] else 'level'}"] +
                       [e["detail"] for e in pr["evidence"] + pr["structural"] if e["met"]],
            "invalidation": pr["invalidation"]}, None


def evaluate(instrument: str, intraday: pd.DataFrame, daily: pd.DataFrame, gate: SignalGate) -> dict:
    """Decision as of the last CLOSED bar of `intraday`."""
    p = gate.p
    tf = intraday.attrs.get("timeframe", "15m")
    decision_time = intraday.index[-1] + pd.Timedelta(minutes=BAR_MINUTES[tf])
    base = {"instrument": instrument, "time": str(decision_time), "disclaimer": DISCLAIMER,
            "mode_label": "PAPER MODE - " + str(intraday.attrs.get("source")) + " DATA"
            if intraday.attrs.get("source") != "LIVE" else "LIVE DATA - PAPER TRADING ONLY"}

    blocks = gate.blocks(instrument, decision_time)
    if blocks:
        return {**base, "action": "NO TRADE", "blocked_by": blocks, "reasons": blocks}
    try:
        f = generate_forecast(instrument, intraday, daily)
    except ValueError as e:
        return {**base, "action": "NO TRADE", "blocked_by": [str(e)], "reasons": [str(e)]}
    day = intraday[intraday.index.date == intraday.index[-1].date()]
    atr = f["volatility"]["intraday_atr"]
    from indicators import trend as _t
    tsum = _t.trend_summary(intraday)
    f["_ema_alignment"], f["_structure"] = tsum["ema_alignment"], tsum["structure"]
    base.update(mode_label=_label(f), price=f["reference_price"], bias=f["bias"]["bias"])

    # regime filter: choppy, low-energy tape
    if tsum["adx"] is not None and tsum["adx"] < 15 and f["volatility"]["range_state"] == "CONTRACTION":
        return {**base, "action": "NO TRADE", "blocked_by": ["Choppy market: ADX < 15 and range contracting"],
                "reasons": ["Choppy market: ADX < 15 and range contracting"]}

    cands, rejects = [], []
    c, why = _orb_candidate(f, intraday, day, decision_time, atr, p)
    (cands.append(c) if c else rejects.append(why)) if (c or why) else None
    if p.enable_retest:
        c, why = _retest_candidate(f, intraday, day, decision_time, atr, p)
        (cands.append(c) if c else rejects.append(why)) if (c or why) else None
    c, info = _reversal_candidate(f, intraday, decision_time, atr, p)
    if c:
        cands.append(c)
    elif isinstance(info, str):
        rejects.append(info)

    scored = []
    for c in cands:
        score, strong = _score(c["components"], p)
        scored.append((score, strong, c))
    scored.sort(key=lambda x: -x[0])

    if not scored:
        waiting = [f"Bullish: {f['scenarios']['bullish']['trigger']}", f"Bearish: {f['scenarios']['bearish']['trigger']}"]
        return {**base, "action": "WAIT", "reasons": rejects or ["No confirmed setup yet"], "waiting_for": waiting,
                "blocked_by": []}

    score, strong, c = scored[0]
    direction = "BUY" if c["s"] > 0 else "SELL"
    detail = {"setup": c["setup"], "direction": direction, "entry_ref": round(c["entry_ref"], 2),
              "entry_zone": c["entry_zone"], "stop": c["plan"]["stop"], "targets": c["plan"]["targets"],
              "rr_t1": round(c["plan"]["rr1"], 2), "rr_t2": round(c["plan"]["rr2"], 2),
              "quality_score": score, "strong_components": strong, "score_components": c["components"],
              "reasons": c["reasons"], "invalidation": c["invalidation"],
              "valid_for_bars": p.signal_valid_bars, "status": "ACTIVE"}
    if score < p.min_quality or strong < p.min_strong_components:
        return {**base, **detail, "action": "WAIT",
                "reasons": [f"{c['setup']} setup present but quality {score:.0f} < {p.min_quality:.0f} or only {strong} "
                            f"strong components (< {p.min_strong_components})"] + c["reasons"],
                "waiting_for": ["Stronger agreement across trend, momentum, bias and reward:risk"], "blocked_by": []}
    if gate.is_duplicate(instrument, direction, c["setup"], c["entry_ref"], atr):
        return {**base, "action": "NO TRADE", "blocked_by": ["Duplicate of an active signal"],
                "reasons": ["Duplicate of an active signal"]}
    sig = {**base, **detail, "action": direction, "blocked_by": [],
           "id": f"{instrument}-{decision_time:%Y%m%d%H%M}-{direction}-{c['setup']}"}
    gate.register(sig, decision_time)
    return sig


def update_signal(sig: dict, bars: pd.DataFrame) -> str:
    """Status of a BUY/SELL signal given the bars that came AFTER it was issued.
    Conservative: if stop and a target are touched in the same bar, the stop is assumed first."""
    s = 1 if sig["direction"] == "BUY" else -1
    t1, t2 = sig["targets"][0]["level"], sig["targets"][1]["level"]
    status = "ACTIVE"
    for _, b in bars.iterrows():
        stop_hit = b["low"] <= sig["stop"] if s > 0 else b["high"] >= sig["stop"]
        if stop_hit:
            return "STOPPED"
        t2_hit = b["high"] >= t2 if s > 0 else b["low"] <= t2
        t1_hit = b["high"] >= t1 if s > 0 else b["low"] <= t1
        if t2_hit:
            return "TARGET2_HIT"
        if t1_hit:
            status = "TARGET1_HIT"
    if status == "ACTIVE" and len(bars) >= sig["valid_for_bars"]:
        return "EXPIRED"
    return status

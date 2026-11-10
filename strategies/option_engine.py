"""Option-buying decision engine: BUY CALL / BUY PUT / WAIT FOR CONFIRMATION / NO TRADE (and EXIT / INVALIDATED via
advance_signal). Paper signals only, never places orders.

Everything is evaluated on CLOSED candles available at `now`. Underlying (index) levels and option PREMIUM levels are
kept strictly separate. The score is a strategy-quality rating, NOT a probability of profit.
Premium targets / stop are ESTIMATES from the contract's delta and gamma (and a theta drag on targets); they ignore IV changes.
"""
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from indicators import patterns as P
from indicators import trend as T
from indicators.extra import bollinger, ema_set, vwap
from indicators.momentum import macd, rsi
from indicators.volatility import atr
from indicators.volume import relative_volume
from option_chain.chain import UNAVAILABLE
from risk_management.option_risk import OptionParams, OptionRiskGate
from strategies.regime import classify_regime

DISCLAIMER = "PAPER SIGNAL. Score = strategy-quality rating, not a win probability. Premium levels are estimates. No order is placed."
TFS = ("1m", "3m", "5m", "15m", "30m")
MIN_BARS = {"1m": 25, "3m": 25, "5m": 40, "15m": 30, "30m": 30}
BULL_PATTERNS = {"BULLISH_ENGULFING", "HAMMER", "BULLISH_PIN_BAR", "MORNING_STAR"}
BEAR_PATTERNS = {"BEARISH_ENGULFING", "SHOOTING_STAR", "BEARISH_PIN_BAR", "EVENING_STAR"}


def _f(x):
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _comp(name, earned, mx, note, available=True):
    return {"name": name, "earned": round(float(earned), 2), "max": float(mx), "note": note, "available": available}


def classify_score(score: float, p: OptionParams) -> str:
    return ("HIGH_QUALITY" if score >= p.high_quality_score else "GOOD" if score >= p.min_score
            else "WEAK_WAIT" if score >= p.wait_score else "NO_TRADE")


def analyse_frame(df: pd.DataFrame) -> dict:
    """Indicators for one timeframe (last closed bar)."""
    e = ema_set(df)
    last = df.iloc[-1]
    ad = T.adx(df)
    m = macd(df["close"])
    vw = vwap(df)
    bb = bollinger(df["close"])
    a = atr(df, 14)
    rv = relative_volume(df) if (df["volume"].fillna(0) > 0).any() else pd.Series(np.nan, index=df.index)
    e9, e20, e50, e200 = (_f(e[c].iloc[-1]) for c in ("ema9", "ema20", "ema50", "ema200"))
    stack = ("BULLISH" if e9 and e20 and e50 and e9 > e20 > e50 else "BEARISH" if e9 and e20 and e50 and e9 < e20 < e50 else "MIXED")
    sw = T.swing_points(df, 2)
    return {"close": float(last["close"]), "ema9": e9, "ema20": e20, "ema50": e50, "ema200": e200, "stack": stack,
            "rsi": _f(rsi(df["close"]).iloc[-1]), "macd_hist": _f(m["hist"].iloc[-1]), "macd_hist_prev": _f(m["hist"].iloc[-2]),
            "adx": _f(ad["adx"].iloc[-1]), "plus_di": _f(ad["plus_di"].iloc[-1]), "minus_di": _f(ad["minus_di"].iloc[-1]),
            "atr": _f(a.iloc[-1]), "atr_long": _f(a.rolling(30, min_periods=10).mean().iloc[-1]), "vwap": _f(vw.iloc[-1]),
            "rvol": _f(rv.iloc[-1]), "bb_width": _f(bb["width_pct"].iloc[-1]), "bb_width_median": _f(bb["width_pct"].median()),
            "structure": T.market_structure(sw)["structure"], "swings": sw, "series_rsi": rsi(df["close"]), "series_ema20": e["ema20"]}


def tf_trend(a: dict, price: float) -> str:
    """BULLISH / BEARISH / NEUTRAL for one timeframe (EMA stack + price vs EMA20 + structure; needs 2 of 3)."""
    bull = (a["stack"] == "BULLISH") + (a["ema20"] is not None and price > a["ema20"]) + (a["structure"] == "UPTREND")
    bear = (a["stack"] == "BEARISH") + (a["ema20"] is not None and price < a["ema20"]) + (a["structure"] == "DOWNTREND")
    return "BULLISH" if bull >= 2 and bull > bear else "BEARISH" if bear >= 2 and bear > bull else "NEUTRAL"


def build_levels(today_5m: pd.DataFrame, prev: dict, an5: dict, chain_an: dict, zones: dict, cpr: dict) -> dict:
    """Named levels from real price structure. Triggers = levels that can be broken; obstacles = everything."""
    lv = {}
    if prev.get("available"):
        lv.update(PDH=prev["high"], PDL=prev["low"], PDC=prev["close"])
        for i, x in enumerate(prev.get("resistance_levels", [])):
            lv[f"PREV_RES{i + 1}"] = x
        for i, x in enumerate(prev.get("support_levels", [])):
            lv[f"PREV_SUP{i + 1}"] = x
    if len(today_5m) >= 3:
        first = today_5m.iloc[:3]
        lv.update(OR_HIGH=float(first.high.max()), OR_LOW=float(first.low.min()))
        lv.update(TODAY_HIGH=float(today_5m.high.max()), TODAY_LOW=float(today_5m.low.min()))
    sw = an5["swings"]
    if not sw.empty:
        h, l = sw[sw.kind == "high"], sw[sw.kind == "low"]
        if len(h):
            lv["SWING_HIGH"] = float(h.iloc[-1].price)
        if len(l):
            lv["SWING_LOW"] = float(l.iloc[-1].price)
    if cpr:
        lv.update(CPR_TOP=cpr["cpr_top"], CPR_BOTTOM=cpr["cpr_bottom"], PIVOT=cpr["pivot"])
        for k in ("R1", "R2", "S1", "S2"):
            lv[k] = cpr["pivots"][k]
    for i, z in enumerate((zones or {}).get("resistances", [])[:2]):
        lv[f"ZONE_RES{i + 1}"] = z["price"]
    for i, z in enumerate((zones or {}).get("supports", [])[:2]):
        lv[f"ZONE_SUP{i + 1}"] = z["price"]
    if chain_an and chain_an.get("resistance_oi"):
        lv["OI_CALL_WALL"] = chain_an["resistance_oi"]["strike"]
    if chain_an and chain_an.get("support_oi"):
        lv["OI_PUT_WALL"] = chain_an["support_oi"]["strike"]
    return {k: float(v) for k, v in lv.items() if v is not None}


def find_trigger(s, f5, an5, levels, price, atr5, p, regime, prev=None) -> dict:
    """Setup trigger on the last closed 5m candle for direction s. Returns {'found', 'type', ...} or {'found': False, 'why': [...]}."""
    why = []
    up_names = ("PDH", "OR_HIGH", "SWING_HIGH", "ZONE_RES1", "PREV_RES1", "R1")
    dn_names = ("PDL", "OR_LOW", "SWING_LOW", "ZONE_SUP1", "PREV_SUP1", "S1")
    names = up_names if s > 0 else dn_names
    tol = 0.3 * atr5
    allowed = regime["allowed_setups"]
    last = f5.iloc[-1]
    for nm in names:
        lvl = levels.get(nm)
        if lvl is None:
            continue
        if P.false_breakout(f5, lvl, s):
            why.append(f"{nm} {lvl:.0f}: failed breakout (trap) - not traded")
            continue
        if "BREAKOUT" in allowed:
            b = P.breakout_close(f5, lvl, s, p.breakout_max_age_bars)
            if b:
                if b["extension"] > p.max_chase_atr * atr5:
                    why.append(f"{nm}: price already {b['extension'] / atr5:.1f} ATR beyond the level - do not chase")
                    continue
                stop_ref = min(float(b["bar"]["low"]), lvl - 0.3 * atr5) if s > 0 else max(float(b["bar"]["high"]), lvl + 0.3 * atr5)
                return {"found": True, "type": "BREAKOUT", "name": f"{'Resistance' if s > 0 else 'Support'} break: {nm}", "level": lvl,
                        "level_name": nm, "stop_ref": stop_ref, "age": b["age"], "bar": b["bar"]}
        if "RETEST" in allowed and P.retest_hold(f5, lvl, s, tol):
            seg = f5.iloc[-4:]
            stop_ref = (min(float(seg.low.min()), lvl) - 0.3 * atr5) if s > 0 else (max(float(seg.high.max()), lvl) + 0.3 * atr5)
            return {"found": True, "type": "RETEST", "name": f"Breakout retest held: {nm}", "level": lvl, "level_name": nm,
                    "stop_ref": stop_ref, "age": 0, "bar": last}
    cb = P.consolidation_breakout(f5)
    if cb and cb[0] == s and "BREAKOUT" in allowed and s * (price - cb[1]) <= p.max_chase_atr * atr5:
        stop_ref = (cb[1] - 0.5 * atr5) if s > 0 else (cb[1] + 0.5 * atr5)
        return {"found": True, "type": "BREAKOUT", "name": "Consolidation range breakout", "level": cb[1], "level_name": "RANGE",
                "stop_ref": stop_ref, "age": 0, "bar": last}
    # rejection / reversal at a real level (only if regime allows)
    if "REJECTION" in allowed or "REVERSAL" in allowed or "REJECTION_WITH_TREND" in allowed:
        pats = set(P.candle_patterns(f5)) | set(P.star_patterns(f5))
        sweep = P.liquidity_sweep(f5)
        want = BULL_PATTERNS if s > 0 else BEAR_PATTERNS
        hit = pats & want
        if sweep == ("BULLISH_SWEEP" if s > 0 else "BEARISH_SWEEP"):
            hit.add(sweep)
        div = P.rsi_divergence(f5, an5["series_rsi"])
        if div == ("BULLISH" if s > 0 else "BEARISH"):
            hit.add(f"RSI_{div}_DIVERGENCE")
        if hit:
            area = ("PDL", "OR_LOW", "SWING_LOW", "ZONE_SUP1", "ZONE_SUP2", "PREV_SUP1", "S1", "S2", "OI_PUT_WALL") if s > 0 else \
                   ("PDH", "OR_HIGH", "SWING_HIGH", "ZONE_RES1", "ZONE_RES2", "PREV_RES1", "R1", "R2", "OI_CALL_WALL")
            ext = float(f5["low"].iloc[-1]) if s > 0 else float(f5["high"].iloc[-1])
            for nm in area:
                lvl = levels.get(nm)
                if lvl is not None and abs(ext - lvl) <= 0.5 * atr5:
                    stop_ref = ext - 0.3 * atr5 if s > 0 else ext + 0.3 * atr5
                    state = reversal_state(s, f5, an5, hit, lvl)
                    if state["state"] == "REVERSAL_CONFIRMED" or "REJECTION_WITH_TREND" in allowed:
                        return {"found": True, "type": "REVERSAL" if regime["regime"] == "REVERSAL" else "REJECTION",
                                "name": f"{', '.join(sorted(hit))} at {nm} {lvl:.0f}", "level": lvl, "level_name": nm,
                                "stop_ref": stop_ref, "age": 0, "bar": last, "reversal": state}
                    why.append(f"{', '.join(sorted(hit))} at {nm}: reversal is {state['state']} - waiting for confirmation")
                    break
    return {"found": False, "why": why or ["No breakout, retest or confirmed rejection on the last closed 5m candle"]}


def reversal_state(s, f5, an5, patterns, level) -> dict:
    """POTENTIAL -> DEVELOPING -> CONFIRMED, or INVALIDATED if price closed beyond the level against the reversal."""
    close = float(f5["close"].iloc[-1])
    if (s > 0 and close < level - 0.3 * (an5["atr"] or 0)) or (s < 0 and close > level + 0.3 * (an5["atr"] or 0)):
        return {"state": "REVERSAL_INVALIDATED", "evidence": []}
    ev = []
    ev.append("candle pattern: " + ", ".join(sorted(patterns)))
    ev_n = 0
    if an5["macd_hist"] is not None and an5["macd_hist_prev"] is not None and s * (an5["macd_hist"] - an5["macd_hist_prev"]) > 0:
        ev.append("MACD histogram turning"); ev_n += 1
    if an5["vwap"] is not None and s * (close - an5["vwap"]) > 0:
        ev.append("price on the right side of VWAP"); ev_n += 1
    if an5["ema9"] is not None and s * (close - an5["ema9"]) > 0:
        ev.append("closed beyond EMA9"); ev_n += 1
    se = P.structure_events(f5)
    if se["event"] == ("CHOCH_UP" if s > 0 else "CHOCH_DOWN"):
        ev.append("change of character"); ev_n += 2
    if an5["rvol"] is not None and an5["rvol"] >= 1.2:
        ev.append("volume above average"); ev_n += 1
    state = "REVERSAL_CONFIRMED" if ev_n >= 3 else "REVERSAL_DEVELOPING" if ev_n >= 1 else "POTENTIAL_REVERSAL"
    return {"state": state, "evidence": ev, "evidence_points": ev_n}


def underlying_plan(s, price, trig, atr5, levels, an5, p) -> tuple:
    """(plan, reject). Stop beyond the trigger structure; targets from the next real levels, else R-multiple projections."""
    stop = trig["stop_ref"]
    risk = s * (price - stop)
    risk = max(risk, p.min_risk_atr * atr5)
    if risk > p.max_risk_atr * atr5:
        return None, f"Underlying stop too wide ({risk / atr5:.1f} ATR > {p.max_risk_atr})"
    stop = price - s * risk
    ahead = sorted([(s * (v - price), k, v) for k, v in levels.items() if s * (v - price) >= 0.8 * risk and k not in ("PDC", "PIVOT")])
    t, used = [], set()

    def pick(min_r, fallback_r, label_fb):
        for d, k, v in ahead:
            if d / risk >= min_r and k not in used and d / risk <= 4.5:
                used.add(k)
                return {"level": round(v, 2), "basis": k, "rr": round(d / risk, 2)}
        return {"level": round(price + s * fallback_r * risk, 2), "basis": label_fb, "rr": fallback_r}
    t1 = pick(p.min_rr_t1, 1.5, "R-multiple projection")
    t2 = pick(max(p.min_rr_t2, t1["rr"] + 0.4), 2.5, "R-multiple projection")
    out = [t1, t2]
    if an5["adx"] is not None and an5["adx"] >= 25 and an5["stack"] == ("BULLISH" if s > 0 else "BEARISH"):
        out.append(pick(t2["rr"] + 0.5, 3.5, "R-multiple projection (strong trend)"))
    return {"entry": round(price, 2), "stop": round(stop, 2), "risk_points": round(risk, 2), "targets": out}, None


def premium_at(opt: dict, d_under: float) -> float:
    """Estimated premium after the underlying moves by d_under: delta and gamma terms only (ignores IV change and time)."""
    return opt["ltp"] + opt["delta"] * d_under + 0.5 * (opt["gamma"] or 0) * d_under ** 2


def select_contract(s, price, chain_df, expiry, quote_fn, p, now, expiry_today, lot_size=1) -> tuple:
    """Pick ATM/near-ATM contract by delta, liquidity, theta and real spread. Returns (contract, reject_reason)."""
    side = "ce" if s > 0 else "pe"
    df = chain_df
    near = df.iloc[(df["strike"] - price).abs().argsort()[:5]]
    cands, rejected = [], []
    for r in near.itertuples():
        g = lambda c: getattr(r, f"{side}_{c}")
        ltp, delta, oi, vol, theta = _f(g("ltp")), _f(g("delta")), _f(g("oi")), _f(g("vol")), _f(g("theta"))
        sym = g("symbol")
        if not sym or ltp is None or delta is None:
            continue
        if ltp < p.min_premium:
            rejected.append(f"{sym}: premium {ltp} < {p.min_premium}"); continue
        if not (p.delta_min <= abs(delta) <= p.delta_max):
            rejected.append(f"{sym}: |delta| {abs(delta):.2f} outside {p.delta_min}-{p.delta_max}"); continue
        if oi is None or vol is None or oi / lot_size < p.min_oi_lots or vol / lot_size < p.min_option_volume_lots:
            rejected.append(f"{sym}: illiquid (OI {oi} = {0 if oi is None else oi / lot_size:.0f} lots, volume {vol})"); continue
        theta_pct = 100 * abs(theta) / ltp if theta is not None else None
        if theta_pct is not None and theta_pct > p.max_theta_pct:
            rejected.append(f"{sym}: theta {theta_pct:.1f}%/day of premium > {p.max_theta_pct}%"); continue
        cands.append({"symbol": sym, "strike": float(r.strike), "type": "CE" if s > 0 else "PE", "ltp": ltp, "delta": delta,
                      "gamma": _f(g("gamma")) or 0.0, "theta": theta, "vega": _f(g("vega")), "iv": _f(g("iv")), "oi": int(oi),
                      "volume": int(vol), "theta_pct_per_day": round(theta_pct, 2) if theta_pct is not None else None,
                      "expiry": expiry, "expiry_today": expiry_today})
    if not cands:
        return None, "No liquid contract near ATM: " + ("; ".join(rejected[:3]) or "chain empty")
    cands.sort(key=lambda c: abs(abs(c["delta"]) - 0.55))
    for c in cands:
        q = quote_fn(c["symbol"]) if quote_fn else None
        if not q or q.get("bid") is None or q.get("ask") is None:
            rejected.append(f"{c['symbol']}: bid/ask unavailable"); continue
        if q["spread_pct"] is None or q["spread_pct"] > p.max_spread_pct:
            rejected.append(f"{c['symbol']}: spread {q['spread_pct']}% > {p.max_spread_pct}%"); continue
        c["quote"] = q
        return c, None
    return None, "Spread/liquidity check failed: " + "; ".join(rejected[-3:])


def option_plan(s, contract, uplan, p, gate, lot_size, price, hold_minutes) -> tuple:
    """Premium entry / stop / targets, risk and quantity. Returns (plan, reject_reason)."""
    q = contract["quote"]
    entry = q["ask"]
    base = {"ltp": entry, "delta": contract["delta"], "gamma": contract["gamma"]}
    drag = abs(contract["theta"] or 0) * hold_minutes / 375.0
    sl = premium_at(base, uplan["stop"] - price)
    risk_unit = entry - sl
    if risk_unit <= 0.05 * entry * 0.1 or sl <= 0:
        return None, "Premium stop cannot be placed sensibly (non-positive risk or stop at zero)"
    if 100 * risk_unit / entry > p.max_premium_loss_pct:
        return None, f"Premium stop too wide ({100 * risk_unit / entry:.0f}% > {p.max_premium_loss_pct:.0f}%)"
    if drag > p.max_theta_drag_risk * risk_unit:
        return None, (f"Theta over the {hold_minutes}-minute hold ({drag:.2f}) would eat {100 * drag / risk_unit:.0f}% of the premium risk "
                      f"(limit {100 * p.max_theta_drag_risk:.0f}%)")
    tg = []
    for t in uplan["targets"]:
        prem = premium_at(base, t["level"] - price) - drag
        rr = (prem - entry) / risk_unit
        tg.append({"premium": round(prem, 2), "rr": round(rr, 2), "underlying_level": t["level"], "basis": t["basis"]})
    if tg[0]["rr"] < p.min_rr_t1 or tg[1]["rr"] < p.min_rr_t2:
        return None, f"Premium reward:risk too low (T1 {tg[0]['rr']}, T2 {tg[1]['rr']}; need {p.min_rr_t1}/{p.min_rr_t2})"
    budget = min(gate.max_risk_rupees, gate.remaining_daily_loss())
    per_lot = risk_unit * lot_size
    lots = int(budget // per_lot)
    cost_cap = int((p.capital * p.max_capital_per_trade_pct / 100) // (entry * lot_size))
    lots = min(lots, cost_cap)
    if lots < 1:
        return None, (f"1 lot risks Rs {per_lot:.0f} but allowed risk is Rs {budget:.0f} "
                      f"(or premium for 1 lot exceeds the capital cap): no sensible position size")
    return {"entry": round(entry, 2), "entry_zone": [round(q["bid"] + (q["ask"] - q["bid"]) / 2, 2), round(entry, 2)],
            "stop": round(sl, 2), "targets": tg, "risk_per_unit": round(risk_unit, 2), "lot_size": lot_size, "lots": lots,
            "quantity": lots * lot_size, "max_loss_rupees": round(lots * per_lot, 2), "premium_cost_rupees": round(lots * lot_size * entry, 2),
            "estimate_note": "Premium stop/targets are delta-gamma estimates (theta drag on targets); IV changes are not modelled."}, None


def score_side(s, an: dict, price, trig, levels, chain_an, oi_change, prev, cmp_, p) -> tuple:
    """Return (score 0-100 normalised over AVAILABLE components, components). Correlated indicators are grouped, not double counted."""
    want = "BULLISH" if s > 0 else "BEARISH"
    a5, a15, a30, a3 = an["5m"], an["15m"], an["30m"], an["3m"]
    atr5 = a5["atr"]
    c = []
    htf = sum(2.5 * (tf_trend(a, price) == want) + 1.25 * (tf_trend(a, price) == "NEUTRAL") for a in (a30, a15))
    c.append(_comp("htf_trend", htf, 10, f"30m {tf_trend(a30, price)}, 15m {tf_trend(a15, price)}"))
    aligned = sum(tf_trend(an[t], price) == want for t in ("30m", "15m", "5m", "3m"))
    c.append(_comp("mtf_alignment", {4: 12, 3: 8, 2: 3}.get(aligned, 0), 12, f"{aligned}/4 timeframes {want}"))
    se = P.structure_events(an["_f5"])
    pats = (set(P.candle_patterns(an["_f5"])) | set(P.star_patterns(an["_f5"]))) & (BULL_PATTERNS if s > 0 else BEAR_PATTERNS)
    if trig["type"] == "NONE":                       # no setup on this side: price action earns nothing (no bonus points either)
        pa = 0
    else:
        pa = 8 if trig["type"] in ("BREAKOUT", "RETEST") else 6 if trig["type"] == "REVERSAL" else 5
        if se["event"] == ("BOS_UP" if s > 0 else "BOS_DOWN") or (trig["type"] == "REVERSAL" and se["event"] == ("CHOCH_UP" if s > 0 else "CHOCH_DOWN")):
            pa += 4
        pa += 3 if pats else 0
    c.append(_comp("price_action", min(pa, 15), 15, f"{trig['name']}; structure event {se['event']}; patterns {sorted(pats) or 'none'}"))
    if a5["vwap"] is not None:
        d = s * (price - a5["vwap"])
        c.append(_comp("vwap", 8 if d > 0.1 * atr5 else 4 if d > -0.1 * atr5 else 0, 8, f"price {'above' if price > a5['vwap'] else 'below'} VWAP {a5['vwap']:.1f}"))
    else:
        c.append(_comp("vwap", 0, 8, "VWAP unavailable (no volume)", available=False))
    if a5["rvol"] is not None:
        c.append(_comp("volume", 8 if a5["rvol"] >= 1.5 else 4 if a5["rvol"] >= 1.0 else 0, 8, f"relative volume {a5['rvol']:.2f} (futures volume)"))
    else:
        c.append(_comp("volume", 0, 8, "volume unavailable", available=False))
    mom = 0.0
    r = a5["rsi"]
    if r is not None:
        mom += 2 if ((52 <= r <= 72) if s > 0 else (28 <= r <= 48)) else 0.5 if ((45 <= r < 52) if s > 0 else (48 < r <= 55)) else 0
    if a5["macd_hist"] is not None and a5["macd_hist_prev"] is not None:
        mom += 2 if (s * a5["macd_hist"] > 0 and s * (a5["macd_hist"] - a5["macd_hist_prev"]) >= 0) else 0
    if a5["adx"] is not None:
        di = (a5["plus_di"] - a5["minus_di"]) * s > 0
        mom += 3 if (a5["adx"] >= 25 and di) else 1.5 if (a5["adx"] >= 20 and di) else 0
    c.append(_comp("momentum", mom, 7, f"RSI {r and round(r)}, MACD hist {a5['macd_hist'] and round(a5['macd_hist'], 2)}, ADX {a5['adx'] and round(a5['adx'])} (one grouped factor)"))
    # room to the next obstacle in the trade direction
    ahead = [abs(v - price) for k, v in levels.items() if s * (v - price) > 0.2 * atr5]
    room = (min(ahead) / atr5) if ahead else 3.0
    c.append(_comp("room_to_target", 8 if room >= 1.5 else 4 if room >= 1.0 else 0, 8, f"nearest obstacle {room:.1f} ATR away"))
    vol_ok = a5["atr"] is not None and a5["atr_long"] is not None and a5["atr"] >= 0.7 * a5["atr_long"]
    c.append(_comp("volatility", 5 if vol_ok else 0, 5, "ATR not contracting" if vol_ok else "ATR contracting"))
    if cmp_.get("available"):
        ok = (cmp_["position"] == ("ABOVE_PDH" if s > 0 else "BELOW_PDL")) or cmp_["vs_previous_trend"].startswith("CONFIRMS")
        c.append(_comp("previous_session", 6 if ok else 2 if cmp_["vs_previous_trend"].startswith("NEUTRAL") else 0, 6,
                       f"{cmp_['gap_type']}, {cmp_['position']}, today {cmp_['vs_previous_trend']} yesterday"))
    else:
        c.append(_comp("previous_session", 0, 6, "previous session unavailable", available=False))
    if chain_an:
        oc = 0.0
        mx = 7.0
        oc += 3 if chain_an["pcr_lean"] == want else 1 if chain_an["pcr_lean"] == "NEUTRAL" else 0
        wall = chain_an["resistance_oi"] if s > 0 else chain_an["support_oi"]
        oc += 4 if (wall is None or abs(wall["strike"] - price) >= 1.0 * atr5) else 0
        note = f"PCR near ATM {chain_an['pcr_near_atm']} ({chain_an['pcr_lean']}); OI wall {wall['strike'] if wall else 'none'}"
        if oi_change not in (None, UNAVAILABLE):
            mx += 3
            oc += 3 if oi_change["lean"] == want else 0
            note += f"; OI change lean {oi_change['lean']}"
        else:
            note += "; change in OI UNAVAILABLE"
        c.append(_comp("option_chain", oc, mx, note))
    else:
        c.append(_comp("option_chain", 0, 10, "option chain unavailable", available=False))
    avail = [x for x in c if x["available"]]
    mx = sum(x["max"] for x in avail)
    return round(100 * sum(x["earned"] for x in avail) / mx, 1) if mx else 0.0, c


def evaluate_option_signal(instrument, now: datetime, data_state: dict, frames: dict, prev: dict, cmp_: dict, chain,
                           expiry: str, lot_size: int, gate: OptionRiskGate, quote_fn=None, cpr=None, zones=None,
                           oi_change=None, p: OptionParams = None) -> dict:
    """One decision for `instrument` as of `now`. chain = (DataFrame, underlying_ltp, analysis) or None."""
    p = p or gate.p
    base = {"instrument": instrument, "time": now.isoformat(timespec="seconds"), "disclaimer": DISCLAIMER, "mode": "PAPER SIGNAL",
            "data": {k: data_state.get(k) for k in ("state", "label", "tick_age_s", "latency_s", "data_time_ist", "reason")},
            "source": "Groww live feed" if data_state.get("live") else str(data_state.get("state"))}

    def no(action, reasons, **extra):
        return {**base, "action": action, "reasons": reasons, "why_no_trade": reasons, **extra}

    live = bool(data_state.get("live"))
    if not live and not (frames and chain is not None and data_state.get("state") == "MARKET_CLOSED"):
        return no("NO TRADE", [f"DATA UNAVAILABLE: {data_state.get('reason')}"], status="DATA_UNAVAILABLE")
    # Market closed: the analysis of the last session is still computed for the dashboard, but a decision is never a trade.
    if chain is None:
        return no("NO TRADE", ["Option chain unavailable: cannot select a contract"], status="NO_OPTION_DATA")
    chain_df, price, chain_an = chain
    for tf in TFS:
        if tf not in frames or len(frames[tf]) < MIN_BARS[tf]:
            return no("NO TRADE", [f"Not enough closed {tf} candles ({len(frames.get(tf, []))}/{MIN_BARS[tf]})"], status="INSUFFICIENT_HISTORY")
    expiry_today = str(expiry) == str(now.date())
    blocks = gate.blocks(instrument, now, expiry_today)       # applied below, after the analysis, so the dashboard still shows it

    an = {tf: analyse_frame(frames[tf]) for tf in TFS}
    f5 = frames["5m"]
    an["_f5"] = f5
    a5 = an["5m"]
    atr5 = a5["atr"]
    if not atr5:
        return no("NO TRADE", ["5m ATR not available"], status="INSUFFICIENT_HISTORY")
    today_5m = f5[f5.index.date == now.date()]
    levels = build_levels(today_5m, prev, a5, chain_an, zones, cpr)
    mtf = {tf: tf_trend(an[tf], price) for tf in ("30m", "15m", "5m", "3m")}
    e1 = an["1m"]
    mtf_report = dict(mtf)
    if "1h" in frames and len(frames["1h"]) >= 30:           # 1-hour is shown for context, it is not part of the score
        mtf_report = {"1h": tf_trend(analyse_frame(frames["1h"]), price), **mtf_report}
    indicators = {tf: {k: (round(v, 2) if isinstance(v, float) else v) for k, v in an[tf].items()
                       if k in ("close", "stack", "rsi", "macd_hist", "adx", "atr", "vwap", "rvol", "ema9", "ema20", "ema50", "ema200", "structure")}
                  for tf in TFS}

    results = {}
    for name, s in (("CALL", 1), ("PUT", -1)):
        broke = None
        for nm in (("PDH", "OR_HIGH") if s > 0 else ("PDL", "OR_LOW")):
            if nm in levels and P.breakout_close(f5, levels[nm], s, p.breakout_max_age_bars):
                broke = "UP" if s > 0 else "DOWN"
        reg_pat = (set(P.candle_patterns(f5)) | set(P.star_patterns(f5))) & (BULL_PATTERNS if s > 0 else BEAR_PATTERNS)
        regime = classify_regime(a5["adx"] if a5["adx"] is not None else float("nan"), a5["plus_di"] or 0, a5["minus_di"] or 0, a5["stack"],
                                 a5["structure"], (atr5 / a5["atr_long"]) if a5["atr_long"] else 1.0, a5["bb_width"] or 0,
                                 a5["bb_width_median"] or 0, broke, bool(reg_pat) and a5["adx"] is not None and a5["adx"] < 22)
        trig = find_trigger(s, f5, a5, levels, price, atr5, p, regime)
        r = {"side": name, "regime": regime, "trigger": trig, "score": 0.0, "classification": "NO_TRADE", "components": [],
             "missing": [], "mtf": mtf}
        if not trig["found"]:
            r["missing"] = trig["why"]
            # still score using a neutral placeholder trigger so the dashboard can compare sides
            ph = {"type": "NONE", "name": "no trigger yet"}
            r["score"], r["components"] = score_side(s, an, price, ph, levels, chain_an, oi_change, prev, cmp_, p)
            r["classification"] = classify_score(min(r["score"], p.min_score - 0.1), p)
        else:
            r["score"], r["components"] = score_side(s, an, price, trig, levels, chain_an, oi_change, prev, cmp_, p)
            r["classification"] = classify_score(r["score"], p)
            # 1-minute entry timing
            timing_ok = s * (e1["close"] - (e1["ema9"] or e1["close"])) > 0
            r["entry_timing"] = "READY" if timing_ok else "WAITING FOR ENTRY"
            if not timing_ok:
                r["missing"].append("1m entry timing: 1m close not yet beyond 1m EMA9 in trade direction")
            if r["score"] < p.min_score:
                r["missing"].append(f"Score {r['score']} below minimum {p.min_score}")
            opp = "BEARISH" if s > 0 else "BULLISH"
            if mtf["30m"] == opp and mtf["15m"] == opp:
                r["missing"].append(f"30m and 15m both {opp}: timeframes conflict")
            ex = P.exhaustion(f5, a5["series_rsi"], a5["series_ema20"], atr5)
            if ex == ("UP" if s > 0 else "DOWN") and trig["type"] != "REVERSAL":
                r["missing"].append("Trend exhaustion (price stretched from EMA20 with extreme RSI): not chasing")
        results[name] = r

    ready = [r for r in results.values() if r["trigger"]["found"] and not r["missing"]]
    top_scores = sorted(results.values(), key=lambda r: -r["score"])
    out_sides = {k: {"score": v["score"], "classification": v["classification"], "regime": v["regime"]["regime"],
                     "trigger": (v["trigger"]["name"] if v["trigger"]["found"] else None), "missing": v["missing"],
                     "entry_timing": v.get("entry_timing"), "components": v["components"]} for k, v in results.items()}
    e1_ready = {1: results["CALL"].get("entry_timing"), -1: results["PUT"].get("entry_timing")}
    mtf_report["1m"] = "WAITING FOR ENTRY" if "WAITING FOR ENTRY" in e1_ready.values() else ("READY" if "READY" in e1_ready.values() else "NO TRIGGER")
    ctx = {"sides": out_sides, "levels": {k: round(v, 2) for k, v in levels.items()}, "mtf": mtf_report, "underlying_price": price,
           "regime": top_scores[0]["regime"]["regime"], "regime_detail": top_scores[0]["regime"], "chain": chain_an, "indicators": indicators}
    if not live:
        return {**base, **ctx, "action": "NO TRADE", "status": "DATA_UNAVAILABLE", "reasons": [f"DATA UNAVAILABLE: {data_state.get('reason')}"],
                "why_no_trade": [f"DATA UNAVAILABLE: {data_state.get('reason')} (showing the analysis of the last session, not a live signal)"]}
    if blocks:
        extra = [f"(analysis) {k}: {m}" for k, v in results.items() for m in v["missing"][:2]]
        return {**base, **ctx, "action": "NO TRADE", "status": "RISK_BLOCKED", "reasons": blocks, "why_no_trade": blocks + extra}
    if not ready:
        near = [r for r in results.values() if r["trigger"]["found"] and r["score"] >= p.wait_score]
        if near:
            r = max(near, key=lambda x: x["score"])
            return {**base, **ctx, "action": "WAIT FOR CONFIRMATION", "status": "WAIT_FOR_ENTRY_CONFIRMATION",
                    "wait_for": r["missing"], "reasons": r["missing"], "why_no_trade": r["missing"],
                    "candidate": "BUY CALL" if r["side"] == "CALL" else "BUY PUT"}
        why = []
        for k, v in results.items():
            why += [f"{k}: {m}" for m in (v["missing"] or [f"score {v['score']} < {p.wait_score}"])]
        return {**base, **ctx, "action": "NO TRADE", "status": "NO_SETUP", "reasons": why, "why_no_trade": why}
    if len(ready) == 2 and abs(ready[0]["score"] - ready[1]["score"]) < 5:
        return {**base, **ctx, "action": "NO TRADE", "status": "CONFLICT", "reasons": ["CALL and PUT setups both qualify with similar scores: conflicting evidence"],
                "why_no_trade": ["CALL and PUT setups both qualify with similar scores: conflicting evidence"]}
    best = max(ready, key=lambda r: r["score"])
    s = 1 if best["side"] == "CALL" else -1
    trig = best["trigger"]
    uplan, why = underlying_plan(s, price, trig, atr5, levels, a5, p)
    if uplan is None:
        return {**base, **ctx, "action": "NO TRADE", "status": "RISK_REJECTED", "reasons": [why], "why_no_trade": [why]}
    contract, why = select_contract(s, price, chain_df, expiry, quote_fn, p, now, expiry_today, lot_size)
    if contract is None:
        return {**base, **ctx, "action": "NO TRADE", "status": "NO_CONTRACT", "reasons": [why], "why_no_trade": [why]}
    oplan, why = option_plan(s, contract, uplan, p, gate, lot_size, price, p.hold_minutes)
    if oplan is None:
        return {**base, **ctx, "action": "NO TRADE", "status": "RISK_REJECTED", "reasons": [why], "why_no_trade": [why]}
    direction = "BUY CALL" if s > 0 else "BUY PUT"
    key = f"{instrument}-{now.date()}-{direction}-{trig['level_name']}-{round(trig['level'])}"
    if gate.is_duplicate(key):
        return {**base, **ctx, "action": "NO TRADE", "status": "DUPLICATE", "reasons": ["Duplicate of a signal already issued for this setup"],
                "why_no_trade": ["Duplicate of a signal already issued for this setup"]}
    sig_id = f"{instrument}-{now:%Y%m%d%H%M%S}-{contract['type']}-{int(contract['strike'])}"
    why_trade = [f"Regime: {best['regime']['regime']}", f"Setup: {trig['name']}"] + \
                [f"{c['name']}: {c['note']}" for c in best["components"] if c["available"] and c["earned"] >= 0.5 * c["max"]]
    sig = {**base, **ctx, "action": direction, "status": "CONFIRMED", "lifecycle": "CONFIRMED", "id": sig_id, "setup_key": key,
           "option": {k: contract[k] for k in ("symbol", "type", "strike", "expiry", "ltp", "delta", "gamma", "theta", "vega", "iv", "oi",
                                              "volume", "theta_pct_per_day", "expiry_today")},
           "quote": contract["quote"], "underlying": uplan, "premium": oplan,
           "score": best["score"], "classification": best["classification"], "pattern": trig["name"], "setup_type": trig["type"],
           "regime_name": best["regime"]["regime"], "components": best["components"], "why_trade": why_trade,
           "reversal": trig.get("reversal"),
           "invalidation": f"Underlying 5m close {'below' if s > 0 else 'above'} {uplan['stop']} or premium at/below {oplan['stop']}",
           "invalidation_underlying": uplan["stop"], "entry_valid_until": (now + timedelta(minutes=p.entry_valid_minutes)).isoformat(timespec="seconds"),
           "expires_at": (now + timedelta(minutes=p.max_hold_minutes)).isoformat(timespec="seconds"),
           "created_at": now.isoformat(timespec="seconds"), "sl_current": oplan["stop"]}
    gate.register(sig, now)
    return sig


def advance_signal(sig: dict, premium_ltp: float, underlying: float, now: datetime, p: OptionParams = None) -> dict:
    """Lifecycle step for a paper signal. Returns {'status', 'event', 'pnl_per_unit', 'final'}.
    CONFIRMED -> ACTIVE (entry triggered) -> TARGET1_HIT -> TARGET2_HIT/TARGET3_HIT | STOP_LOSS | INVALIDATED | EXPIRED.
    A signal that never entered is EXPIRED and is NOT counted as a trade (pnl None). After T1 the stop moves to entry."""
    p = p or OptionParams()
    st = sig.get("lifecycle", "CONFIRMED")
    entry = sig["premium"]["entry"]
    tg = [t["premium"] for t in sig["premium"]["targets"]]
    s = 1 if sig["action"] == "BUY CALL" else -1
    res = lambda status, event, final=False, pnl=None: {"status": status, "event": event, "final": final, "pnl_per_unit": pnl}
    if st in ("TARGET2_HIT", "TARGET3_HIT", "STOP_LOSS", "INVALIDATED", "EXPIRED"):
        return res(st, None, True, sig.get("pnl_per_unit"))
    if st == "CONFIRMED":
        if now > datetime.fromisoformat(sig["entry_valid_until"]) and premium_ltp > sig["premium"]["entry_zone"][1] * 1.02:
            return res("EXPIRED", "Entry window passed without entry (not counted as a trade)", True, None)
        if s * (underlying - sig["invalidation_underlying"]) <= 0:
            return res("INVALIDATED", "Invalidated before entry (not counted as a trade)", True, None)
        if premium_ltp <= sig["premium"]["entry_zone"][1]:
            sig["lifecycle"] = "ACTIVE"
            return res("ACTIVE", "ENTRY TRIGGERED")
        return res("CONFIRMED", None)
    sl = sig.get("sl_current", sig["premium"]["stop"])
    if premium_ltp <= sl:
        pnl = sl - entry
        return res("STOP_LOSS", "Stop loss hit" if sl < entry else "Stopped at entry after Target 1", True, pnl)
    if s * (underlying - sig["invalidation_underlying"]) <= 0:
        return res("INVALIDATED", "EXIT: underlying invalidation level broken", True, premium_ltp - entry)
    last_t = len(tg)
    if premium_ltp >= tg[-1] and last_t >= 2:
        return res("TARGET3_HIT" if last_t == 3 else "TARGET2_HIT", f"Target {last_t} reached", True, tg[-1] - entry)
    if last_t == 3 and premium_ltp >= tg[1] and st != "TARGET2_HIT_OPEN":
        sig["lifecycle"] = "TARGET2_HIT_OPEN"
        sig["sl_current"] = max(sl, tg[0])
        return res("ACTIVE", "Target 2 reached; stop moved to Target 1, running to Target 3")
    if premium_ltp >= tg[1] and last_t == 2:
        return res("TARGET2_HIT", "Target 2 reached", True, tg[1] - entry)
    if premium_ltp >= tg[0] and st == "ACTIVE":
        sig["lifecycle"] = "TARGET1_HIT"
        if p.trail_to_entry_after_t1:
            sig["sl_current"] = max(sl, entry)
        return res("TARGET1_HIT", "Target 1 reached" + ("; stop moved to entry" if p.trail_to_entry_after_t1 else ""))
    if now.time() >= p.square_off or now > datetime.fromisoformat(sig["expires_at"]):
        return res("EXPIRED", "Time exit (square-off / max hold)", True, premium_ltp - entry)
    return res(st if st != "TARGET2_HIT_OPEN" else "ACTIVE", None)

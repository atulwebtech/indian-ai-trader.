"""Reversal classifier.

States (never a trade signal by itself):
  CONFIRMED_REVERSAL  - at a level, a structural break (market-structure shift or failed breakout)
                        PLUS at least two other independent confirmations
  POTENTIAL_REVERSAL  - at a level with 2+ confirmations (or one structural break), but not enough for CONFIRMED
  FALSE_BREAKOUT      - price broke a range extreme and closed back inside (trap), little else yet
  TREND_CONTINUATION  - price pushed through the level in the trend direction with no reversal evidence
  NO_CONFIRMATION     - touching a level (or nothing) is not enough

Only data up to the last bar is used. Volume evidence is skipped (and listed as not evaluated)
when the data has no volume, which is the case for spot indices.
"""
import numpy as np
import pandas as pd

from indicators import levels as lv
from indicators import momentum, price_action, trend, volatility, volume

CONFIRMED, POTENTIAL, FALSE_BO, CONTINUATION, NONE = (
    "CONFIRMED_REVERSAL", "POTENTIAL_REVERSAL", "FALSE_BREAKOUT", "TREND_CONTINUATION", "NO_CONFIRMATION")
RANK = {CONFIRMED: 5, POTENTIAL: 4, FALSE_BO: 3, CONTINUATION: 2, NONE: 1}

TOUCH_TOL_ATR = 0.35
DISPLACEMENT_ATR = 0.75
RECENT = 6   # bars in which the level touch and the confirmations must fall (90 min on 15m)


def levels_from_forecast(f: dict) -> list:
    """Collect numeric levels from a Phase 6 forecast: [{'price', 'label'}]."""
    out = []
    for key in ("bullish", "bearish"):
        for t in f["scenarios"][key]["targets"]:
            out.append({"price": t["level"], "label": f"{key} {t['label']}"})
    out += [{"price": f["support"], "label": "Support"}, {"price": f["resistance"], "label": "Resistance"}]
    kl = f["key_levels"]
    if kl.get("previous_day"):
        out += [{"price": kl["previous_day"]["prev_high"], "label": "Prev day high"},
                {"price": kl["previous_day"]["prev_low"], "label": "Prev day low"}]
    out += [{"price": v, "label": f"Pivot {k}"} for k, v in kl.get("pivots", {}).items()]
    if kl.get("opening_range"):
        out += [{"price": kl["opening_range"]["or_high"], "label": "Opening range high"},
                {"price": kl["opening_range"]["or_low"], "label": "Opening range low"}]
    return out


def _ev(name, met, detail):
    return {"name": name, "met": bool(met), "detail": detail}


def _evaluate(df, today, side, level, atr_, ema9, rsi_, avg_price, vol_ok, snap_pa):
    """side = +1 evaluates a TOP (bearish) reversal, -1 a BOTTOM (bullish) reversal."""
    recent = df.tail(RECENT)
    last = df.iloc[-1]
    ext = recent["high"].max() if side > 0 else recent["low"].min()
    word = ("bearish", "bullish")[side < 0]
    evid, not_eval = [], []

    # --- others (independent confirmations) ---
    win = df.tail(RECENT + 1)
    rej = [price_action.rejection(r) for _, r in win.iloc[1:].iterrows()]
    engs = [price_action.engulfing(win.iloc[i - 1], win.iloc[i]) for i in range(1, len(win))]
    want_rej = "BEARISH_REJECTION" if side > 0 else "BULLISH_REJECTION"
    want_eng = "BEARISH_ENGULFING" if side > 0 else "BULLISH_ENGULFING"
    evid.append(_ev("rejection_candle", want_rej in rej or want_eng in engs,
                    f"{word} rejection/engulfing candle in the last {RECENT} bars"))

    if avg_price is not None:
        was = ((df["close"].tail(RECENT + 1).iloc[:-1] > avg_price.tail(RECENT + 1).iloc[:-1]) if side > 0
               else (df["close"].tail(RECENT + 1).iloc[:-1] < avg_price.tail(RECENT + 1).iloc[:-1])).any()
        now = last["close"] < avg_price.iloc[-1] if side > 0 else last["close"] > avg_price.iloc[-1]
        evid.append(_ev("session_average_cross", was and now,
                        f"Close crossed {'below' if side > 0 else 'above'} the session average price"))
    else:
        not_eval.append("session_average_cross")

    div = momentum.divergence(df)["bearish" if side > 0 else "bullish"]
    r_now = rsi_.iloc[-1]
    if side > 0:
        weak = rsi_.tail(6).max() >= 65 and r_now <= rsi_.tail(6).max() - 8
    else:
        weak = rsi_.tail(6).min() <= 35 and r_now >= rsi_.tail(6).min() + 8
    evid.append(_ev("rsi_turn", div or weak, f"RSI {'divergence' if div else 'rolling over from extreme'} ({r_now:.0f})"))

    slope_ok = ema9.iloc[-1] < ema9.iloc[-2] if side > 0 else ema9.iloc[-1] > ema9.iloc[-2]
    side_ok = last["close"] < ema9.iloc[-1] if side > 0 else last["close"] > ema9.iloc[-1]
    evid.append(_ev("ema9_loss", slope_ok and side_ok, f"Close {'below' if side > 0 else 'above'} a turning EMA 9"))

    disp = (ext - last["close"]) if side > 0 else (last["close"] - ext)
    evid.append(_ev("displacement", disp >= DISPLACEMENT_ATR * atr_,
                    f"Moved {disp / atr_:.2f} ATR away from the extreme (needs {DISPLACEMENT_ATR})"))

    if vol_ok:
        rv = volume.relative_volume(df).iloc[-RECENT:].max()
        evid.append(_ev("volume_surge", bool(pd.notna(rv) and rv >= 1.5), f"Relative volume {rv:.1f}"))
    else:
        not_eval.append("volume_surge")

    # --- structural ---
    mss = snap_pa["structure_shift"] == ("BEARISH_MSS" if side > 0 else "BULLISH_MSS")
    fb = snap_pa["breakout"] == ("FAILED_BREAKOUT_UP" if side > 0 else "FAILED_BREAKDOWN")
    structural = [_ev("market_structure_shift", mss, "Close beyond the last confirmed swing"),
                  _ev("failed_breakout", fb, "Broke the recent range and closed back inside")]
    return evid, structural, not_eval, ext, fb


def _classify(side, df, at_level, evid, structural, snap, level, ext, atr_):
    others = sum(e["met"] for e in evid)
    struct = any(s["met"] for s in structural)
    fb = structural[1]["met"]
    t = snap["trend"]
    aligned = t["ema_alignment"] == ("BULLISH" if side > 0 else "BEARISH")
    beyond = df["close"].iloc[-1] > level if side > 0 else df["close"].iloc[-1] < level
    if (at_level or fb) and struct and others >= 2:
        return CONFIRMED
    if fb and others < 2:
        return FALSE_BO
    if (at_level or fb) and (others >= 2 or struct):
        return POTENTIAL
    if at_level and aligned and beyond and others == 0:
        return CONTINUATION
    return NONE


def detect_reversal(intraday: pd.DataFrame, level_list: list, tol_atr: float = TOUCH_TOL_ATR) -> dict:
    """Evaluate top and bottom reversals at the nearest known level. `intraday` must end at the
    bar being evaluated."""
    if len(intraday) < 60:
        raise ValueError("Need at least 60 bars")
    df = intraday
    atr_ = float(volatility.atr(df).iloc[-1])
    ema9 = trend.ema(df["close"], 9)
    rsi_ = momentum.rsi(df["close"])
    day = df[df.index.date == df.index[-1].date()]
    avg = lv.session_vwap(day) if volume.has_volume(day) else lv.session_avg_price(day)
    avg_price = avg.reindex(df.index).dropna() if len(day) >= RECENT + 1 else None
    if avg_price is not None:
        avg_price = avg_price.reindex(df.index).ffill()
        if avg_price.tail(RECENT + 1).isna().any():
            avg_price = None
    snap_pa = price_action.price_action_summary(df)
    snap = {"trend": trend.trend_summary(df)}
    vol_ok = volume.has_volume(df)

    results = {}
    for side, name in ((+1, "top"), (-1, "bottom")):
        recent = df.tail(RECENT)
        ext = recent["high"].max() if side > 0 else recent["low"].min()
        cand = [l for l in level_list if (l["price"] >= df["close"].iloc[-1] - atr_ if side > 0
                                          else l["price"] <= df["close"].iloc[-1] + atr_)]
        near = min(cand, key=lambda l: abs(l["price"] - ext)) if cand else None
        at_level = bool(near and abs(near["price"] - ext) <= tol_atr * atr_)
        evid, structural, not_eval, ext, fb = _evaluate(df, day, side, near["price"] if near else ext, atr_, ema9,
                                                         rsi_, avg_price, vol_ok, snap_pa)
        level = near["price"] if near else None
        state = _classify(side, df, at_level, evid, structural, snap, level if level is not None else ext, ext, atr_)
        missing = [e["name"] for e in evid if not e["met"]] + [s["name"] for s in structural if not s["met"]]
        inval = (ext + tol_atr * atr_) if side > 0 else (ext - tol_atr * atr_)
        results[name] = {
            "state": state, "direction": "BEARISH" if side > 0 else "BULLISH",
            "level": None if near is None else {"price": near["price"], "label": near["label"]},
            "at_level": at_level, "extreme": float(ext),
            "evidence": evid, "structural": structural, "not_evaluated": not_eval,
            "evidence_met": sum(e["met"] for e in evid), "evidence_available": len(evid),
            "still_needed": missing if state in (POTENTIAL, NONE, FALSE_BO) else [],
            "invalidation": f"A close {'above' if side > 0 else 'below'} {inval:.2f} cancels this {('bearish', 'bullish')[side < 0]} reversal idea",
        }
    best = max(results.values(), key=lambda r: (RANK[r["state"]], r["evidence_met"]))
    return {"as_of": str(df.index[-1]), "primary": best, "top": results["top"], "bottom": results["bottom"],
            "note": "A reversal state is context for a signal, not a signal. A touch of a level alone never qualifies."}

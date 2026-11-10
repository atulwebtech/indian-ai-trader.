"""End-of-day scorecard: how did today's pre-market forecast and signals do, and how accurate has the tool
been so far? Everything is computed from data and recorded results; nothing is estimated by hand.

Definitions (plain language):
  * Direction check  - pre-market bias BULLISH/BEARISH vs whether the day closed above/below its open
                       (NEUTRAL days are not scored).
  * Level check      - did the day's high/low reach each forecast target zone? Compared with the history rate
                       the forecast printed next to it.
  * Signal results   - paper trades, in R, before costs.
  * Hindsight swings - the biggest moves of the day found AFTER the fact. They show what was possible,
                       NOT what could have been known in advance.
"""
import json
import math
from datetime import time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from config.settings import ROOT
from forecasting.daily_forecast import generate_forecast

MIN_DAYS_FOR_VERDICT = 20
SWING_ATR_FRACTION = 0.35


def zigzag(closes, thr: float) -> list:
    """Indices of swing turning points in `closes` where each reversal is at least `thr`."""
    c = list(closes)
    n = len(c)
    if n < 2:
        return []
    piv, d, hi_i, lo_i, ext = [], 0, 0, 0, 0
    for i in range(1, n):
        if d == 0:
            hi_i, lo_i = (i if c[i] > c[hi_i] else hi_i), (i if c[i] < c[lo_i] else lo_i)
            if c[hi_i] - c[lo_i] >= thr:
                if hi_i > lo_i:
                    piv, d, ext = [lo_i], 1, hi_i
                else:
                    piv, d, ext = [hi_i], -1, lo_i
        elif d == 1:
            if c[i] > c[ext]:
                ext = i
            elif c[ext] - c[i] >= thr:
                piv.append(ext)
                d, ext = -1, i
        else:
            if c[i] < c[ext]:
                ext = i
            elif c[i] - c[ext] >= thr:
                piv.append(ext)
                d, ext = 1, i
    if piv:
        piv.append(ext)
    return piv


def best_swings(day: pd.DataFrame, thr: float, signals: list, top: int = 3) -> list:
    """The biggest up/down legs of the day (hindsight) and whether a same-direction signal fell inside each."""
    piv = zigzag(day["close"].to_numpy(), thr)
    legs = []
    for a, b in zip(piv, piv[1:]):
        p0, p1 = float(day["close"].iloc[a]), float(day["close"].iloc[b])
        legs.append({"direction": "UP" if p1 > p0 else "DOWN", "start_time": str(day.index[a])[11:16], "start": p0,
                     "end_time": str(day.index[b])[11:16], "end": p1, "points": abs(p1 - p0),
                     "_t0": day.index[a], "_t1": day.index[b] + timedelta(minutes=15)})
    legs.sort(key=lambda x: -x["points"])
    out = []
    for g in legs[:top]:
        caught = [s for s in signals if g["_t0"] <= pd.Timestamp(s["time"]) <= g["_t1"]
                  and (s["action"] == "BUY") == (g["direction"] == "UP")]
        g["caught"] = bool(caught)
        g["points"] = round(g["points"], 1)
        out.append({k: v for k, v in g.items() if not k.startswith("_")})
    return out


def day_scorecard(instrument, intra, daily, day, ledger_items) -> dict:
    """Score one finished (or partly finished) session. The forecast is rebuilt from data BEFORE the day."""
    pre = intra[intra.index.date < day]
    pre.attrs.update(intra.attrs)
    f = generate_forecast(instrument, pre, daily, session_date=day)
    d = intra[intra.index.date == day]
    if d.empty:
        raise ValueError("no candles for this day")
    o, h, l, c = float(d.open.iloc[0]), float(d.high.max()), float(d.low.min()), float(d.close.iloc[-1])
    last_end = d.index[-1] + timedelta(minutes=15)
    complete = last_end.time() >= dtime(15, 30)
    bias = f["bias"]["bias"]
    right = None if bias == "NEUTRAL" else ((bias == "BULLISH") == (c > o))
    targets = []
    for side, key, hit in (("up", "bullish", lambda lv: h >= lv), ("down", "bearish", lambda lv: l <= lv)):
        for t in f["scenarios"][key]["targets"]:
            targets.append({"side": side, "label": t["label"], "level": t["level"], "stated": t["historical_reach_rate"],
                            "reached": bool(hit(t["level"]))})
    orr = f["opening_range_analysis"]
    or_text = None
    after = d[d.index >= d.index[0] + timedelta(minutes=15)]
    if len(d) >= 2 and not after.empty:
        o_hi, o_lo = float(d.high.iloc[0]), float(d.low.iloc[0])
        up = after[after.close > o_hi]
        dn = after[after.close < o_lo]
        first_up, first_dn = (up.index[0] if len(up) else None), (dn.index[0] if len(dn) else None)
        if first_up is not None and (first_dn is None or first_up < first_dn):
            or_text = f"broke UP at {str(first_up)[11:16]}; day closed {'above' if c > o_hi else 'back inside/below'} the range"
        elif first_dn is not None:
            or_text = f"broke DOWN at {str(first_dn)[11:16]}; day closed {'below' if c < o_lo else 'back inside/above'} the range"
        else:
            or_text = "never closed outside the first 15-minute range"
    sigs = [i for i in ledger_items if i["signal"]["instrument"] == instrument and str(i["signal"]["time"])[:10] == str(day)]
    sig_dicts = [i["signal"] for i in sigs]
    atr_d = f["volatility"]["daily_atr"]
    return {"date": str(day), "instrument": instrument, "complete": complete, "last_candle_end": str(last_end)[11:16],
            "open": o, "high": h, "low": l, "close": c, "change_pct": 100 * (c - o) / o, "bias": bias,
            "bias_score": f["bias"]["score"], "bias_right": right,
            "targets": targets, "actual_range": h - l, "expected_range": atr_d,
            "expected_remaining_range": f["volatility"]["expected_remaining_range"],
            "opening_range_outcome": or_text,
            "signals": [{"time": str(i["signal"]["time"])[11:16], "action": i["signal"]["action"], "setup": i["signal"]["setup"],
                         "entry": i["signal"]["entry_ref"], "stop": i["signal"]["stop"], "status": i["status"], "r": i["r"]} for i in sigs],
            "swings": best_swings(d, SWING_ATR_FRACTION * atr_d, sig_dicts)}


class ScoreStore:
    def __init__(self, path):
        self.path = Path(path)
        self.items = {}
        if self.path.exists():
            try:
                self.items = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self.items = {}

    def add(self, sc: dict):
        """Only complete days are stored, so partial reports never pollute the totals."""
        if not sc["complete"]:
            return False
        self.items[f"{sc['date']}:{sc['instrument']}"] = sc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, indent=1, default=str))
        return True

    def records(self):
        return sorted(self.items.values(), key=lambda r: (r["date"], r["instrument"]))


def cumulative(records: list) -> dict:
    n_days = len({r["date"] for r in records})
    scored = [r for r in records if r["bias_right"] is not None]
    right = sum(1 for r in scored if r["bias_right"])
    t = {}
    for r in records:
        for x in r["targets"]:
            if x["stated"] is None:
                continue
            k = f"{x['side']} {x['label']}"
            t.setdefault(k, []).append((x["stated"], x["reached"]))
    targets = {k: {"n": len(v), "stated": float(np.mean([a for a, _ in v])), "actual": float(np.mean([b for _, b in v]))} for k, v in t.items()}
    sig = [s for r in records for s in r["signals"] if s["r"] is not None]
    rs = [s["r"] for s in sig]
    sw = [g for r in records for g in r["swings"]]
    return {"days": n_days, "records": len(records),
            "bias": {"scored": len(scored), "right": right, "rate": (right / len(scored)) if scored else None,
                     "margin": (1.96 * math.sqrt(0.25 / len(scored))) if scored else None,
                     "up_close_rate": float(np.mean([r["change_pct"] > 0 for r in records])) if records else None},
            "targets": targets,
            "signals": {"closed": len(rs), "wins": sum(1 for x in rs if x > 0), "total_r": float(sum(rs)),
                        "expectancy": float(np.mean(rs)) if rs else None},
            "swings": {"n": len(sw), "caught": sum(1 for g in sw if g["caught"])}}


def backtest_reference(instrument: str, root=ROOT):
    """Headline numbers from the saved 2024-2026 forecast backtest, for comparison with live results."""
    p = Path(root) / "reports" / f"forecast_backtest_{instrument}.csv"
    if not p.exists():
        return None
    d = pd.read_csv(p)
    sc = d[d.bias != "NEUTRAL"]
    right = ((sc.bias == "BULLISH") == (sc.close > sc.open)).mean()
    return {"days": len(d), "bias_right": float(right), "up1": (float(d.up1_rate.mean()), float(d.up1_hit.mean())),
            "dn1": (float(d.dn1_rate.mean()), float(d.dn1_hit.mean()))}


def verdict(cum: dict, lang: str = "en") -> list:
    hi = lang == "hi"
    out = []
    n = cum["days"]
    if n < MIN_DAYS_FOR_VERDICT:
        out.append(f"Sirf {n} din ka record hai. In percentages ka matlab nikalne ke liye kam se kam {MIN_DAYS_FOR_VERDICT} din chahiye; "
                   f"ise abhi diary samjhein, nateeja nahi." if hi else
                   f"Only {n} day(s) recorded. At least {MIN_DAYS_FOR_VERDICT} days are needed before these percentages mean anything; "
                   f"treat them as a diary, not a verdict.")
    b = cum["bias"]
    if b["rate"] is not None and n >= MIN_DAYS_FOR_VERDICT:
        lo, up = b["rate"] - b["margin"], b["rate"] + b["margin"]
        rng = f"{b['rate']:.0%} (95% range {lo:.0%}-{up:.0%})"
        if lo > 0.5:
            out.append(f"Disha ki hit rate {rng} ab tak sikke (50%) se upar hai." if hi else f"Direction hit rate {rng} is above a coin flip so far.")
        elif up < 0.5:
            out.append(f"Disha ki hit rate {rng} ab tak sikke (50%) se NEECHE hai." if hi else f"Direction hit rate {rng} is BELOW a coin flip so far.")
        else:
            out.append(f"Disha ki hit rate {rng} abhi sikke (50%) se alag nahi kahi ja sakti." if hi else
                       f"Direction hit rate {rng} cannot be told apart from a coin flip yet.")
    s = cum["signals"]
    if s["closed"] and s["closed"] < 30:
        out.append(f"Sirf {s['closed']} band paper signal: signal ki quality parakhne ke liye bahut kam (100+ chahiye)." if hi else
                   f"Only {s['closed']} closed paper signal(s): far too few to judge signal quality (aim for 100+).")
    return out


H_STATUS = {"ACTIVE": "chalu", "TARGET1 HIT": "pehla target (T1) mila", "TARGET2 HIT": "dusra target (T2) mila",
            "STOPPED": "stop loss laga", "EXPIRED": "samay khatam"}


def render_eod(cards: list, cum: dict, refs: dict, label: str, stamp: str, partial_note: str = "", lang: str = "en") -> str:
    hi = lang == "hi"
    tt = lambda en, h: h if hi else en
    L = [f"[{label}]", tt("END-OF-DAY REPORT", "DIN KI REPORT") + f" | {stamp}"]
    if partial_note:
        L.append(partial_note)
    bias_hi = {"BULLISH": "BULLISH (tezi)", "BEARISH": "BEARISH (mandi)", "NEUTRAL": "NEUTRAL"}
    for sc in cards:
        up = [t for t in sc["targets"] if t["side"] == "up"]
        dn = [t for t in sc["targets"] if t["side"] == "down"]
        def mark(t):
            hist = ""
            if t["stated"] is not None:
                hist = tt(f" (history said {t['stated']:.0%})", f" (history ke hisaab se {t['stated']:.0%})")
            return f"{t['label']} {t['level']:.0f} " + tt("REACHED" if t["reached"] else "not reached", "MILA" if t["reached"] else "nahi mila") + hist
        above = sc["close"] > sc["open"]
        if sc["bias_right"] is None:
            view = tt("NEUTRAL (no direction was called, not scored)", "NEUTRAL (koi disha nahi boli gayi, score nahi hua)")
        else:
            view = (f"{bias_hi[sc['bias']] if hi else sc['bias']} -> " + tt("RIGHT" if sc["bias_right"] else "WRONG", "SAHI" if sc["bias_right"] else "GALAT")
                    + tt(f" (day closed {'above' if above else 'below'} its open)", f" (din open se {'upar' if above else 'neeche'} band hua)"))
        L += ["", f"=== {sc['instrument']} ===",
              f"Open {sc['open']:.0f} | High {sc['high']:.0f} | Low {sc['low']:.0f} | Close {sc['close']:.0f} ({sc['change_pct']:+.2f}% " + tt("from open", "open se") + ")",
              tt("Pre-market view: ", "Subah ka view: ") + view,
              tt("Upside zones: ", "Upar ke zones: ") + "; ".join(mark(t) for t in up),
              tt("Downside zones: ", "Neeche ke zones: ") + "; ".join(mark(t) for t in dn),
              tt(f"Day range {sc['actual_range']:.0f} pts vs typical daily range {sc['expected_range']:.0f} pts ({sc['actual_range'] / sc['expected_range']:.1f}x)",
                 f"Din ki range {sc['actual_range']:.0f} pts, aam daily range {sc['expected_range']:.0f} pts ({sc['actual_range'] / sc['expected_range']:.1f}x)")]
        if sc["opening_range_outcome"]:
            o = sc["opening_range_outcome"]
            if hi:
                o = (o.replace("broke UP at", "UPAR toota,").replace("broke DOWN at", "NEECHE toota,").replace("day closed above the range", "din range ke upar band hua")
                      .replace("day closed below the range", "din range ke neeche band hua").replace("day closed back inside/below the range", "din wapas range ke andar/neeche band hua")
                      .replace("day closed back inside/above the range", "din wapas range ke andar/upar band hua")
                      .replace("never closed outside the first 15-minute range", "pehli 15 minute ki range ke bahar kabhi close nahi hua"))
            L.append(tt("Opening range: ", "Opening range: ") + o)
        if sc["signals"]:
            L.append(tt("Paper signals:", "Paper signals:"))
            for s in sc["signals"]:
                word = (("CALL (CE)" if s["action"] == "BUY" else "PUT (PE)") if hi else s["action"])
                status = s["status"].replace("_", " ")
                status = H_STATUS.get(status, status) if hi else status
                L.append(f"  {s['time']} {word} {s['setup']} entry~{s['entry']:.0f} stop {s['stop']:.0f} -> {status}"
                         + (f" ({s['r']:+.2f}R)" if s["r"] is not None else ""))
        else:
            L.append(tt("Paper signals: none today (no setup passed every filter)", "Paper signals: aaj koi nahi (koi setup saare filter pass nahi kar paaya)"))
        L.append(tt("Biggest moves of the day (found in hindsight, NOT knowable in advance):", "Din ke sabse bade moves (baad mein dekhe gaye, pehle se jaanna mumkin nahi tha):"))
        for k, g in enumerate(sc["swings"], 1):
            d = g["direction"] if not hi else ("UPAR" if g["direction"] == "UP" else "NEECHE")
            L.append(f"  {k}. {d} {g['start']:.0f} ({g['start_time']}) -> {g['end']:.0f} ({g['end_time']}): {g['points']:.0f} pts"
                     + tt(f" | a signal in that direction: {'YES' if g['caught'] else 'NO'}", f" | us disha mein signal tha: {'HAAN' if g['caught'] else 'NAHI'}"))
    L += ["", tt(f"=== HOW ACCURATE HAS THE TOOL BEEN? ({cum['days']} day(s) recorded so far) ===",
                 f"=== TOOL KITNA ACCURATE RAHA? (ab tak {cum['days']} din ka record) ===")]
    b = cum["bias"]
    if b["scored"]:
        L.append(tt(f"Direction: right {b['right']} of {b['scored']} scored calls ({b['rate']:.0%}, +/- {b['margin']:.0%}). Days that closed above open overall: {b['up_close_rate']:.0%}.",
                    f"Disha: {b['scored']} mein se {b['right']} sahi ({b['rate']:.0%}, +/- {b['margin']:.0%}). Kul dinon mein se open ke upar band hue: {b['up_close_rate']:.0%}."))
    for k, v in cum["targets"].items():
        L.append(tt(f"Level {k}: history said {v['stated']:.0%}, actually reached {v['actual']:.0%} (n={v['n']})",
                    f"Level {k}: history {v['stated']:.0%} kehti thi, asal mein {v['actual']:.0%} mila (n={v['n']})"))
    s = cum["signals"]
    L.append(tt(f"Signals: {s['closed']} closed", f"Signals: {s['closed']} band") + (
        tt(f", {s['wins']} winners, total {s['total_r']:+.2f}R, average {s['expectancy']:+.2f}R per trade (before costs)",
           f", {s['wins']} jeete, kul {s['total_r']:+.2f}R, har trade ka average {s['expectancy']:+.2f}R (kharch se pehle)") if s["closed"] else ""))
    if cum["swings"]["n"]:
        L.append(tt(f"Big moves caught by a signal: {cum['swings']['caught']} of {cum['swings']['n']}",
                    f"Bade moves jo signal ne pakde: {cum['swings']['n']} mein se {cum['swings']['caught']}"))
    L += verdict(cum, lang)
    for inst, r in refs.items():
        if r:
            L.append(tt(f"Reference, 2.9-year backtest {inst}: direction right {r['bias_right']:.0%} of scored days; "
                        f"bull T1 history {r['up1'][0]:.0%} vs reached {r['up1'][1]:.0%}; bear T1 {r['dn1'][0]:.0%} vs {r['dn1'][1]:.0%}",
                        f"Reference, 2.9 saal ka backtest {inst}: disha {r['bias_right']:.0%} dinon mein sahi; "
                        f"bull T1 history {r['up1'][0]:.0%}, mila {r['up1'][1]:.0%}; bear T1 {r['dn1'][0]:.0%}, mila {r['dn1'][1]:.0%}"))
    L += ["", tt("No target or direction is guaranteed. Backtests found no proven trading edge. Paper/test information only, not advice.",
                 "Koi target ya disha guaranteed nahi. Backtest mein koi pakka trading edge nahi mila. Sirf paper/test jaankari, salaah nahi.")]
    return "\n".join(L)

"""Check a trade idea that the USER typed, e.g. `nifty call 23300 sl 23270 target 23360`.

It does not approve or reject a trade. It runs the same checklist the signal engine uses and says which boxes the idea ticks,
which it fails and why. The answer is a checklist, never a prediction.
"""
import re

import pandas as pd

from indicators import trend as _trend
from telegram_bot import hindi as H

INST = {"nifty": "NIFTY", "nifty50": "NIFTY", "banknifty": "BANKNIFTY", "bank": "BANKNIFTY", "bn": "BANKNIFTY", "banknity": "BANKNIFTY"}
SIDE = {"call": 1, "ce": 1, "buy": 1, "long": 1, "put": -1, "pe": -1, "sell": -1, "short": -1}
KEYS = {"sl": "stop", "stop": "stop", "stoploss": "stop", "t": "target", "t1": "target", "target": "target", "tgt": "target",
        "entry": "entry", "at": "entry", "@": "entry"}


def parse(text: str) -> dict:
    """-> {instrument, side, entry, stop, target} (numbers may be None). Returns {} when it does not look like a trade idea."""
    toks = re.sub(r"[,:=/]", " ", text.lower().replace("/check", " ")).split()
    out = {"instrument": None, "side": None, "entry": None, "stop": None, "target": None}
    key = None
    for t in toks:
        if t in INST:
            out["instrument"] = INST[t]
        elif t in SIDE:
            out["side"] = SIDE[t]
        elif t in KEYS:
            key = KEYS[t]
        else:
            try:
                v = float(t)
            except ValueError:
                continue
            if key and out[key] is None:
                out[key] = v
            elif out["entry"] is None:
                out["entry"] = v
            key = None
    return out if out["instrument"] and out["side"] else {}


def run(idea: dict, f: dict, intraday: pd.DataFrame, now, lang="hi") -> str:
    hi = lang == "hi"
    s, inst = idea["side"], idea["instrument"]
    px = float(intraday["close"].iloc[-1])
    atr = f["volatility"]["intraday_atr"]
    entry = idea["entry"] or px
    side_word = "CALL (CE)" if s > 0 else "PUT (PE)"
    sc = f["scenarios"]["bullish" if s > 0 else "bearish"]
    lvl = sc["trigger_level"]
    default_stop = round(lvl - 0.5 * atr if s > 0 else lvl + 0.5 * atr, 0)
    stop = idea["stop"]
    ahead = [t for t in sc["targets"] if s * (t["level"] - entry) > 0]
    target = idea["target"] or (ahead[0]["level"] if ahead else None)
    rows = []   # (mark, text)

    def add(ok, en, hin, warn=False):
        rows.append(("✅" if ok else "⚠️" if warn else "❌", hin if hi else en))

    # 1 stop
    if stop is None:
        rows.append(("ℹ️", f"Aapne stop nahi diya. Chart ke hisaab se suggested stop {default_stop:.0f} (level se 0.5 ATR door)." if hi else
                     f"No stop given. Suggested stop from the chart: {default_stop:.0f} (0.5 ATR beyond the level)."))
        stop = default_stop
    risk = s * (entry - stop)
    if risk <= 0:
        add(False, f"Stop {stop:.0f} is on the wrong side of entry {entry:.0f} for a {side_word}.",
            f"Stop {stop:.0f} galat taraf hai: {side_word} ke liye stop entry {entry:.0f} ke {'neeche' if s > 0 else 'upar'} hona chahiye.")
    else:
        r_atr = risk / atr
        add(0.8 <= r_atr <= 2.5, f"Risk {risk:.0f} points = {r_atr:.1f} ATR (sensible: 0.8 to 2.5).",
            f"Risk {risk:.0f} points = {r_atr:.1f} ATR (theek range: 0.8 se 2.5)." + (" Stop bahut paas, shor mein lag sakta hai." if r_atr < 0.8 else " Stop bahut door." if r_atr > 2.5 else ""))
    # 2 reward:risk
    if target and risk > 0:
        rr = s * (target - entry) / risk
        add(rr >= 1.2, f"Reward:risk to your target {target:.0f} is {rr:.2f} (engine wants 1.2+).",
            f"Aapke target {target:.0f} tak reward:risk {rr:.2f} hai (engine 1.2 ya zyada chahta hai).", warn=rr >= 1.0)
        if s * (target - entry) <= 0:
            add(False, "Target is on the wrong side of entry.", "Target galat taraf hai.")
    # 3 levels in the way
    if target:
        lv = [(t["level"], t["label"]) for t in sc["targets"]] + [(f["resistance"], "Resistance")] * (s > 0) + [(f["support"], "Support")] * (s < 0)
        way = sorted([x for x in lv if s * (x[0] - entry) > 0 and s * (target - x[0]) > 0.15 * atr], key=lambda x: s * (x[0] - entry))
        if way:
            add(False, f"Levels between entry and target: " + ", ".join(f"{n} {v:.0f}" for v, n in way[:3]) + ". Price can stall there.",
                "Entry aur target ke beech level aate hain: " + ", ".join(f"{n} {v:.0f}" for v, n in way[:3]) + ". Wahan price ruk sakta hai.", warn=True)
        else:
            add(True, "No measured level between entry and your target.", "Entry aur target ke beech koi naapa hua level nahi.")
    # 4 bias / trend / momentum
    want = "BULLISH" if s > 0 else "BEARISH"
    b = f["bias"]["bias"]
    add(b == want, f"Day bias is {b} ({f['bias']['score']:+d}/{f['bias']['of']}).",
        f"Din ka bias {H.BIAS.get(b, b)} hai ({f['bias']['score']:+d}/{f['bias']['of']}). " + ("Aapki taraf se mel khata hai." if b == want else "Aapki taraf ke ulta ya bina disha ka hai."), warn=b == "NEUTRAL")
    t = _trend.trend_summary(intraday)
    ema_ok = t["ema_alignment"] == want
    add(ema_ok, f"EMA trend is {t['ema_alignment']}, structure {t['structure']}.", f"EMA trend {t['ema_alignment']}, structure {t['structure']}.", warn=t["ema_alignment"] == "MIXED")
    m = f["momentum"]
    add(m["macd_bias"] == want, f"MACD is {m['macd_bias']}, RSI {m['rsi']:.0f}." if m["rsi"] else f"MACD is {m['macd_bias']}.",
        f"MACD {m['macd_bias']}, RSI {m['rsi']:.0f}." if m["rsi"] else f"MACD {m['macd_bias']}.")
    if t["adx"] is not None and t["adx"] < 15:
        add(False, f"Choppy tape (ADX {t['adx']:.0f} < 15): breakouts fail more often.", f"Market choppy hai (ADX {t['adx']:.0f} < 15): breakout zyada fail hote hain.", warn=True)
    # 5 chasing + clock
    past = s * (px - lvl)
    if past > 0.5 * atr:
        add(False, f"Price is already {past:.0f} points beyond the trigger {lvl:.0f}: this is chasing.",
            f"Price trigger {lvl:.0f} se {past:.0f} points aage nikal chuka hai: ye peeche bhaagna hai. Retest ka intezaar behtar.", warn=True)
    tm = now.time()
    add(tm.hour * 60 + tm.minute <= 14 * 60 + 45 and tm.hour * 60 + tm.minute >= 9 * 60 + 30, f"Time {now:%H:%M}: inside the 09:30-14:45 entry window." if 570 <= tm.hour * 60 + tm.minute <= 885 else f"Time {now:%H:%M} is outside the 09:30-14:45 window.",
        f"Samay {now:%H:%M}: entry window (09:30-14:45) ke andar." if 570 <= tm.hour * 60 + tm.minute <= 885 else f"Samay {now:%H:%M}: entry window (09:30-14:45) ke bahar.")
    ok = sum(1 for m_, _ in rows if m_ == "✅")
    n = sum(1 for m_, _ in rows if m_ in ("✅", "⚠️", "❌"))
    head = (f"🔍 {inst} | {side_word} IDEA CHECK | {now:%d %b %Y %H:%M IST}" if not hi else f"🔍 {inst} | {side_word} IDEA KI JAANCH | {now:%d %b %Y %H:%M IST}")
    ent = f"Entry {entry:.0f} | Stop {stop:.0f}" + (f" | Target {target:.0f}" if target else "") + f" | Price abhi {px:.0f}"
    verdict = (f"Checklist: {ok}/{n} theek. " + ("Zyadatar boxes tick hain, par ye jeetne ki guarantee nahi." if ok >= n - 1 else
               "Kaafi boxes khaali hain: is idea mein risk zyada hai." if ok <= n / 2 else "Mixed tasveer: upar ke ⚠️/❌ dekh lein.")) if hi else \
        (f"Checklist: {ok}/{n} ticked. " + ("Most boxes ticked, but that is not a promise of profit." if ok >= n - 1 else
         "Many boxes are empty: this idea carries more risk." if ok <= n / 2 else "Mixed picture: read the ⚠️/❌ lines above."))
    tail = ("Yeh sirf checklist hai, 'haan/na' ka faisla nahi. System signal tab hi deta hai jab uske sakht rules poore hon. "
            "Strike, expiry aur premium aap khud chunein. Yeh paper/test jaankari hai, salaah nahi.") if hi else \
        ("This is a checklist, not a yes/no verdict. The engine only issues a signal when its strict rules pass. "
         "Strike, expiry and premium are yours to choose. Paper/test information only, not advice.")
    return "\n".join([head, ent, ""] + [f"{a} {b_}" for a, b_ in rows] + ["", verdict, "", tail])

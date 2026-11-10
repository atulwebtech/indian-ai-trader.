"""Telegram message text. Plain text only. Every message carries the mode label and a timestamp.

Every builder takes lang="en" or "hi" (Hinglish). Numbers and levels are identical in both; only the words differ.
"""
from datetime import datetime

from telegram_bot import hindi as H

LANGS = ("hi", "en")

EDGE_WARNING_EN = ("Backtest warning: on 2024-2026 data this signal engine showed NO demonstrated edge "
                   "(net expectancy was negative). Treat as an experiment only.")
EDGE_WARNING_HI = ("Backtest chetavni: 2024-2026 ke data par is signal engine ka koi pakka edge nahi mila "
                   "(kharch ke baad average result negative tha). Ise sirf experiment maniye.")
NOT_ADVICE_EN = "Paper/test information only, not advice. No target is guaranteed. No orders are placed."
NOT_ADVICE_HI = "Yeh sirf paper/test jaankari hai, salaah nahi. Koi target guaranteed nahi. Koi order nahi lagta."
NEWS_CAVEAT_EN = ("Headlines are read by simple keywords and can be wrong or late. This is NOT validated by any backtest. "
                  "The system does not trade on headlines; read the story yourself.")
NEWS_CAVEAT_HI = ("Headlines sirf keywords se padhi jaati hain, to galat ya der se ho sakti hain. Iska koi backtest nahi hua. "
                  "System headline par trade nahi karta; khabar khud padhein.")

# kept for older imports / tests that use the English constants
EDGE_WARNING, NOT_ADVICE, NEWS_CAVEAT = EDGE_WARNING_EN, NOT_ADVICE_EN, NEWS_CAVEAT_EN


def not_advice(lang="en"):
    return NOT_ADVICE_HI if lang == "hi" else NOT_ADVICE_EN


def news_caveat(lang="en"):
    return NEWS_CAVEAT_HI if lang == "hi" else NEWS_CAVEAT_EN


def _x(lang, text):
    """Engine-generated sentence in the chosen language."""
    return H.tr(text) if lang == "hi" else text


def label(source: str) -> str:
    return "LIVE DATA - PAPER TRADING ONLY" if source == "LIVE" else f"PAPER MODE - {source} DATA"


def _stamp(now: datetime) -> str:
    return now.strftime("%d %b %Y %H:%M IST")


def _zones(ts, lang="en", n=3):
    z = "zone" if lang == "en" else "zone"
    return "\n".join(f"  {t['label']}: {t['level']:.0f} ({z} {t['zone_low']:.0f}-{t['zone_high']:.0f})" for t in ts[:n]) or ("  none" if lang == "en" else "  koi nahi")


def _price_line(f, ds, lang="en"):
    when = ds["last_candle_end"].strftime("%d %b %H:%M")
    ref = _x(lang, f["reference_note"])
    if lang == "hi":
        note = "" if ds["status"] == "FRESH" else f" [{H.DATA_STATE.get(ds['status'], ds['status'])}: data purana ho sakta hai, market {H.MARKET_STATE.get(ds['market'], ds['market'])}]"
        return f"Price: {f['reference_price']:.2f} ({ref}; candle {when} par band hui, delayed data){note}"
    note = "" if ds["status"] == "FRESH" else f" [{ds['status']}: data may be old, market {ds['market']}]"
    return f"Price: {f['reference_price']:.2f} ({ref.lower()}; candle ended {when}, delayed data){note}"


def mood_line(f, lang="en") -> str:
    """One clear headline: is the day looking positive, negative or sideways. A tilt, not a promise."""
    b = f["bias"]
    icon, word, why = H.MOOD[b["bias"]]
    if lang == "hi":
        return (f"{icon} AAJ KA MAHAUL: {word} ({why}) | bharosa: {H.CONVICTION.get(b['conviction'].lower(), b['conviction'])}\n"
                "   (yeh andaza hai, guarantee nahi; backtest mein bias ka pakka edge nahi mila)")
    en = {"BULLISH": "leaning up", "BEARISH": "leaning down", "NEUTRAL": "no clear direction, likely range-bound"}[b["bias"]]
    return (f"{icon} TODAY'S MOOD: {word} ({en}) | conviction {b['conviction'].lower()}\n"
            "   (a tilt, not a promise; backtests found no proven edge in the bias)")


def _plan_side(f, side, price, lang):
    """One side of the plan: when to enter, where the idea is wrong, and every target with how often price got there before."""
    sc = f["scenarios"][side]
    up = side == "bullish"
    icon, word = ("🟢", "CALL (CE)") if up else ("🔴", "PUT (PE)")
    lvl = sc["trigger_level"]
    atr = f["volatility"]["intraday_atr"]
    inv = lvl - 0.5 * atr if up else lvl + 0.5 * atr          # stop just beyond the trigger level, same rule as the signal engine
    role = H.ROLE.get(sc["role"], sc["role"]) if lang == "hi" else sc["role"]
    past = (price - lvl) if up else (lvl - price)           # >0: price already beyond the trigger
    if lang == "hi":
        head = f"{icon} {word} PLAN [{role}] - index {'UPAR' if up else 'NEECHE'} jaane par"
        if past > 0.5 * atr:
            when = f"Trigger ({lvl:.0f}) paar ho chuka hai, price {past:.0f} points aage hai. Seedha peeche na bhaagein; {lvl:.0f} tak wapsi (retest) mein level tikne par entry sochein."
        elif past > 0:
            when = f"Trigger {lvl:.0f} abhi paar hua hai. Entry zone: {lvl:.0f} se {abs(past) + 0.3 * atr:.0f} points ke andar."
        else:
            when = f"Entry tab, jab {_x(lang, sc['trigger'])}"
        stop = f"🛑 Stop loss: {inv:.0f} ({f['timeframe']} candle iske {'neeche' if up else 'upar'} band ho to idea galat)"
        reach = "pichle dino mein itne din price yahan tak pahunchi (ginti, guarantee nahi)"
    else:
        head = f"{icon} {word} PLAN [{role}] - if the index goes {'UP' if up else 'DOWN'}"
        if past > 0.5 * atr:
            when = f"Trigger ({lvl:.0f}) already crossed; price is {past:.0f} points beyond it. Do not chase; consider an entry only on a pullback to {lvl:.0f} that holds."
        elif past > 0:
            when = f"Trigger {lvl:.0f} just crossed. Entry zone: within {abs(past) + 0.3 * atr:.0f} points of {lvl:.0f}."
        else:
            when = f"Enter when: {sc['trigger']}"
        stop = f"🛑 Stop loss: {inv:.0f} (a {f['timeframe']} close {'below' if up else 'above'} it means the idea is wrong)"
        reach = "share of past days price got this far (a count, not a promise)"
    rows = []
    beyond = [t for t in sc["targets"] if (t["level"] > lvl if up else t["level"] < lvl)]   # only levels past the entry
    for k, t in enumerate(beyond[:3], 1):
        done = (price >= t["level"]) if up else (price <= t["level"])
        mark = ("✅ paar ho chuka" if lang == "hi" else "✅ already passed") if done else ""
        rate = t.get("historical_reach_rate")
        rows.append(f"🎯 T{k}: {t['level']:.0f}  ({abs(t['level'] - lvl):.0f} pts) " + (f"[{rate:.0%}] " if rate is not None else "") + mark)
    if len(beyond) < 2:      # no measured level beyond the entry: a plain ATR projection, labelled as such
        for m in (0.75, 1.5)[len(beyond):]:
            lv = lvl + m * atr if up else lvl - m * atr
            rows.append(f"🎯 {lv:.0f}  ({m * atr:.0f} pts) " + ("[ATR se andaza, naapa hua level nahi]" if lang == "hi" else "[ATR estimate, not a measured level]"))
    return [head, when, f"📍 Entry level: {lvl:.0f}", stop] + rows + [f"   [..%] = {reach}"]


def flip_block(f, stopped_up: bool, stop_level: float, price: float, lang="en") -> list:
    """The OPPOSITE trade that opens if a CALL (or PUT) idea fails: its stop level becomes the new entry."""
    up = not stopped_up                                    # stopped CALL -> look for a PUT, and the other way round
    sc = f["scenarios"]["bullish" if up else "bearish"]
    atr = f["volatility"]["intraday_atr"]
    lvl = float(stop_level)
    inv = lvl - 0.5 * atr if up else lvl + 0.5 * atr
    icon, word = ("🟢", "CALL (CE)") if up else ("🔴", "PUT (PE)")
    old = "PUT (PE)" if up else "CALL (CE)"
    tf = f["timeframe"]
    rows = []
    beyond = [t for t in sc["targets"] if (t["level"] > lvl if up else t["level"] < lvl)]
    for k, t in enumerate(beyond[:3], 1):
        rate = t.get("historical_reach_rate")
        rows.append(f"🎯 T{k}: {t['level']:.0f}  ({abs(t['level'] - lvl):.0f} pts)" + (f" [{rate:.0%}]" if rate is not None else ""))
    if len(beyond) < 2:
        for m in (0.75, 1.5)[len(beyond):]:
            lv = lvl + m * atr if up else lvl - m * atr
            rows.append(f"🎯 {lv:.0f}  ({m * atr:.0f} pts) " + ("[ATR se andaza]" if lang == "hi" else "[ATR estimate]"))
    if lang == "hi":
        return [f"🔁 PALTA TRADE: agar {old} ka stop ({lvl:.0f}) lag jaye to {icon} {word} idea",
                f"Entry tab, jab {tf} candle {lvl:.0f} ke {'upar' if up else 'neeche'} band ho (pehla idea galat sabit hua, ab disha ulti).",
                f"📍 Entry level: {lvl:.0f}", f"🛑 Stop loss: {inv:.0f} ({tf} candle iske {'neeche' if up else 'upar'} band ho to yeh bhi galat)"] + rows + [
                "Dhyan: ulte trade mein bhi stop loss zaroor lagayein; dono taraf phansne ka risk hota hai (whipsaw)."]
    return [f"🔁 REVERSE TRADE: if the {old} stop ({lvl:.0f}) is hit, the {icon} {word} idea opens",
            f"Enter when a {tf} candle closes {'above' if up else 'below'} {lvl:.0f} (the first idea failed, direction flips).",
            f"📍 Entry level: {lvl:.0f}", f"🛑 Stop loss: {inv:.0f} (a {tf} close {'below' if up else 'above'} it means this one is wrong too)"] + rows + [
            "Note: use a stop on the reverse trade as well; getting whipsawed on both sides is a real risk."]


def plan_message(f, ds, now, lang="en") -> str:
    """Two-sided trade PLAN built from the day's levels. It is conditional ('if X happens'), not a signal."""
    price = f["reference_price"]
    hi = lang == "hi"
    icon, word, why = H.MOOD[f["bias"]["bias"]]
    fav = {"BULLISH": "bullish", "BEARISH": "bearish"}.get(f["bias"]["bias"])
    head = [f"[{label(f['source'])}]", f"📊 {f['instrument']} | {'TRADE PLAN (dono taraf)' if hi else 'TRADE PLAN (both sides)'} | {_stamp(now)}",
            _price_line(f, ds, lang), mood_line(f, lang), RULE]
    order = ["bullish", "bearish"] if fav != "bearish" else ["bearish", "bullish"]
    body = []
    for side in order:
        up = side == "bullish"
        lvl = f["scenarios"][side]["trigger_level"]
        atr = f["volatility"]["intraday_atr"]
        body += _plan_side(f, side, price, lang) + [""] + flip_block(f, up, lvl - 0.5 * atr if up else lvl + 0.5 * atr, price, lang) + [RULE]
    if hi:
        sw = f["scenarios"]["sideways"]
        tail = [f"🟡 SIDEWAYS: {sw['range'][0]:.0f} - {sw['range'][1]:.0f} ke beech ghoome to dono taraf entry na lein.",
                f"Support {f['support']:.0f} | Resistance {f['resistance']:.0f}", "",
                "Yeh signal nahi, SHART WALA PLAN hai ('agar yeh hua to'). Pehle trigger poora hone dein, phir hi entry sochein.",
                "Strike, expiry aur premium aap khud chunein.", "", EDGE_WARNING_HI, NOT_ADVICE_HI]
    else:
        sw = f["scenarios"]["sideways"]
        tail = [f"🟡 SIDEWAYS: if price stays between {sw['range'][0]:.0f} and {sw['range'][1]:.0f}, skip both sides.",
                f"Support {f['support']:.0f} | Resistance {f['resistance']:.0f}", "",
                "This is a CONDITIONAL plan ('if this happens'), not a signal. Wait for the trigger first.",
                "Strike, expiry and premium are not chosen by this system.", "", EDGE_WARNING_EN, NOT_ADVICE_EN]
    return "\n".join(head + body + tail)


def forecast_message(f, ds, now, signal=None, title="FORECAST", lang="en") -> str:
    sc, b = f["scenarios"], f["bias"]
    hi = lang == "hi"
    sig = (signal["action"] if signal else _x(lang, "No signal before the 09:30 entry window"))
    reason = ("; ".join(_x(lang, r) for r in signal.get("reasons", [])[:2]) if signal else _x(lang, "Opening range not formed yet"))
    role = (lambda r: H.ROLE.get(r, r)) if hi else (lambda r: r)
    if hi:
        lines = [f"[{label(f['source'])}]", f"{f['instrument']} | {H.TITLE.get(title, title)} | {_stamp(now)}", _price_line(f, ds, lang), mood_line(f, lang),
                 f"Bias: {H.BIAS[b['bias']]} ({b['score']:+d}/{b['of']}, bharosa: {H.CONVICTION.get(b['conviction'].lower(), b['conviction'])})",
                 f"Support {f['support']:.0f} | Resistance {f['resistance']:.0f}", "",
                 f"Upar ka scenario (Bullish) [{role(sc['bullish']['role'])}] - kab: {_x(lang, sc['bullish']['trigger'])}",
                 _zones(sc["bullish"]["targets"], lang), f"  Cancel kab: {_x(lang, sc['bullish']['invalidation'])}", "",
                 f"Neeche ka scenario (Bearish) [{role(sc['bearish']['role'])}] - kab: {_x(lang, sc['bearish']['trigger'])}",
                 _zones(sc["bearish"]["targets"], lang), f"  Cancel kab: {_x(lang, sc['bearish']['invalidation'])}", "",
                 f"Sideways [{role(sc['sideways']['role'])}]: {sc['sideways']['range'][0]:.0f}-{sc['sideways']['range'][1]:.0f}",
                 f"Signal sthiti: {sig}", f"Wajah: {reason}"]
        o = f["opening_range_analysis"]
        if o["status"] == "COMPLETE":
            lines.append(f"Opening range: {o['or_low']:.0f}-{o['or_high']:.0f}, price is range {H.VS_RANGE.get(o['price_vs_range'].lower(), o['price_vs_range'].lower())} hai")
    else:
        lines = [f"[{label(f['source'])}]", f"{f['instrument']} | {title} | {_stamp(now)}", _price_line(f, ds), mood_line(f, lang),
                 f"Bias: {b['bias']} ({b['score']:+d}/{b['of']}, conviction {b['conviction'].lower()})",
                 f"Support {f['support']:.0f} | Resistance {f['resistance']:.0f}", "",
                 f"Bullish [{sc['bullish']['role']}] - needs: {sc['bullish']['trigger']}",
                 _zones(sc["bullish"]["targets"]), f"  Cancelled if: {sc['bullish']['invalidation']}", "",
                 f"Bearish [{sc['bearish']['role']}] - needs: {sc['bearish']['trigger']}",
                 _zones(sc["bearish"]["targets"]), f"  Cancelled if: {sc['bearish']['invalidation']}", "",
                 f"Sideways [{sc['sideways']['role']}]: {sc['sideways']['range'][0]:.0f}-{sc['sideways']['range'][1]:.0f}",
                 f"Signal status: {sig}", f"Reason: {reason}"]
        o = f["opening_range_analysis"]
        if o["status"] == "COMPLETE":
            lines.append(f"Opening range: {o['or_low']:.0f}-{o['or_high']:.0f}, price {o['price_vs_range'].lower()} it")
    lines += ["", not_advice(lang)]
    return "\n".join(lines)


EXIT_EN = {"TARGET1 HIT": "Book half and move the remaining stop to entry (no loss on the rest).",
           "TARGET2 HIT": "Book the rest. Trade complete.", "STOPPED": "Stop hit: exit and do not re-enter.",
           "EXPIRED": "Signal window over: do not open a fresh entry on this signal."}
RULE = "━━━━━━━━━━━━━━━━━━━━"


def compact_signal_message(sig, now, lang="en") -> str:
    """Trades-only card: side, entry, stop loss, targets, exit. Nothing else."""
    buy = sig["action"] == "BUY"
    icon, word = ("🟢", "CALL (CE)") if buy else ("🔴", "PUT (PE)")
    tg = " | ".join(f"T{i} {t['level']:.0f}" for i, t in enumerate(sig["targets"], 1))
    exit_ = ("T1 par aadha book, stop entry par; baaki T2 par. Stop ke paar 15m candle band ho to nikal jao." if lang == "hi"
             else "Book half at T1, move stop to entry; rest at T2. Exit if a 15m candle closes beyond the stop.")
    return "\n".join([f"{icon} {sig['instrument']} {word} | {_stamp(now)}",
                      f"📍 Entry {sig['entry_zone'][0]:.0f} - {sig['entry_zone'][1]:.0f}",
                      f"🛑 Stop loss {sig['stop']:.0f}", f"🎯 {tg}", f"🚪 {exit_}",
                      f"[{sig['mode_label']}] paper test, salaah nahi" if lang == "hi" else f"[{sig['mode_label']}] paper test, not advice"])


def signal_message(sig, f, ds, now, lang="en") -> str:
    """The trade card: one fixed layout for every signal."""
    t = sig["targets"]
    buy = sig["action"] == "BUY"
    icon = "🟢" if buy else "🔴"
    mood = H.MOOD[f["bias"]["bias"]][1]
    entry = f"{sig['entry_zone'][0]:.0f} - {sig['entry_zone'][1]:.0f}"
    if lang == "hi":
        word = "CALL (CE)" if buy else "PUT (PE)"
        return "\n".join([
            f"[{sig['mode_label']}]", f"{icon} {sig['instrument']} | {word} SIGNAL ({sig['setup']}) | {_stamp(now)}", RULE,
            f"{icon} {'CALL (CE) LO - index UPAR jaayega' if buy else 'PUT (PE) LO - index NEECHE jaayega'}", RULE,
            f"📍 Entry zone (index level): {entry}", f"🛑 Stop loss (index level): {sig['stop']:.0f}",
            f"🎯 Target (index level) 1: {t[0]['level']:.0f}  (R:R {t[0]['rr']})", f"🎯 Target 2: {t[1]['level']:.0f}  (R:R {t[1]['rr']})",
            f"🚪 Exit: T1 par aadha book karo, stop ko entry par le aao; baaki T2 par ya 15:15 par band. "
            f"Stop {sig['stop']:.0f} ke paar 15m candle band ho to turant nikal jao.", f"⏱ Signal lagbhag {sig['valid_for_bars']} candles tak valid hai.", RULE,
            f"Aaj ka mahaul: {mood} | Quality {sig['quality_score']:.0f}/100 (checklist score, jeetne ki sambhavna nahi)",
            _price_line(f, ds, lang), f"Support {f['support']:.0f} | Resistance {f['resistance']:.0f}", "",
            "Kyun: " + "; ".join(_x(lang, r) for r in sig["reasons"][:3]), f"Kab galat: {_x(lang, sig['invalidation'])}", "",
            f"Kya karein: {word} side ka idea hai. Strike, expiry aur premium yeh system nahi chunta, woh aap khud dekhein.",
            "", EDGE_WARNING_HI, NOT_ADVICE_HI])
    return "\n".join([
        f"[{sig['mode_label']}]", f"{icon} {sig['instrument']} | {'CALL (CE)' if buy else 'PUT (PE)'} ALERT ({sig['setup']}) | {_stamp(now)}", RULE,
        f"{icon} {'TAKE CALL (CE) - index likely UP' if buy else 'TAKE PUT (PE) - index likely DOWN'}", RULE,
        f"📍 Entry zone (index level): {entry}", f"🛑 Stop loss (index level): {sig['stop']:.0f}",
        f"🎯 Target (index level) 1: {t[0]['level']:.0f}  (R:R {t[0]['rr']})", f"🎯 Target 2: {t[1]['level']:.0f}  (R:R {t[1]['rr']})",
        f"🚪 Exit: book half at T1 and move the stop to entry; rest at T2 or by 15:15. "
        f"If a {f['timeframe']} candle closes beyond {sig['stop']:.0f}, get out at once.", f"⏱ Valid for about {sig['valid_for_bars']} candles.", RULE,
        f"Today's mood: {mood} | Quality {sig['quality_score']:.0f}/100 (checklist, not a win chance)",
        _price_line(f, ds), f"Support {f['support']:.0f} | Resistance {f['resistance']:.0f}", "",
        "Why: " + "; ".join(sig["reasons"][:3]), f"Invalid if: {sig['invalidation']}", "",
        "What to do: Call-side idea." if buy else "What to do: Put-side idea.",
        "Strike, expiry and premium are NOT chosen by this system.", "", EDGE_WARNING_EN, NOT_ADVICE_EN])


def status_update_message(sig, status, now, lang="en") -> str:
    if lang == "hi":
        word = "CALL (CE)" if sig["action"] == "BUY" else "PUT (PE)"
        return (f"[{sig['mode_label']}]\n{sig['instrument']} | SIGNAL UPDATE | {_stamp(now)}\n"
                f"{word} {sig['setup']} (entry ~{sig['entry_ref']:.0f}, stop {sig['stop']:.0f}): "
                f"{H.STATUS.get(status.replace('_', ' '), status.replace('_', ' '))}\n"
                f"👉 {H.EXIT_HINT.get(status.replace('_', ' '), '')}\n\n{NOT_ADVICE_HI}")
    return (f"[{sig['mode_label']}]\n{sig['instrument']} | SIGNAL UPDATE | {_stamp(now)}\n"
            f"{sig['action']} {sig['setup']} (entry ~{sig['entry_ref']:.0f}, stop {sig['stop']:.0f}): {status.replace('_', ' ')}\n"
            f"-> {EXIT_EN.get(status.replace('_', ' '), '')}\n\n{NOT_ADVICE_EN}")


def reversal_message(inst, r, f, ds, now, lang="en") -> str:
    p = r["primary"]
    met = [_x(lang, e["detail"]) for e in p["evidence"] + p["structural"] if e["met"]]
    if lang == "hi":
        d = "bearish (neeche ki taraf)" if p["direction"] == "BEARISH" else "bullish (upar ki taraf)"
        return "\n".join([f"[{label(f['source'])}]", f"{inst} | CONFIRMED {p['direction']} REVERSAL | {_stamp(now)}", _price_line(f, ds, lang),
                          f"Level: {p['level']['label']} {p['level']['price']:.0f}" if p["level"] else "Level: n/a",
                          f"Reversal ki disha: {d}", "Confirmations: " + "; ".join(met), f"Kab galat: {_x(lang, p['invalidation'])}",
                          "Yeh sirf context hai, akele trade signal nahi.", "", NOT_ADVICE_HI])
    return "\n".join([f"[{label(f['source'])}]", f"{inst} | CONFIRMED {p['direction']} REVERSAL | {_stamp(now)}", _price_line(f, ds),
                      f"Level: {p['level']['label']} {p['level']['price']:.0f}" if p["level"] else "Level: n/a",
                      "Confirmations: " + "; ".join(met), f"Invalid if: {p['invalidation']}",
                      "This is context, not a trade signal by itself.", "", NOT_ADVICE_EN])


def invalidation_message(inst, side, level, f, ds, now, lang="en") -> str:
    if lang == "hi":
        s = "Bullish (upar wala)" if side == "BULLISH" else "Bearish (neeche wala)"
        return "\n".join([f"[{label(f['source'])}]", f"{inst} | FORECAST GALAT HUA ({side}) | {_stamp(now)}", _price_line(f, ds, lang),
                          f"Close {level:.0f} ke paar gaya, isliye {s} scenario cancel ho gaya. Naya forecast dekhein.", "", NOT_ADVICE_HI])
    return "\n".join([f"[{label(f['source'])}]", f"{inst} | FORECAST INVALIDATED ({side}) | {_stamp(now)}", _price_line(f, ds),
                      f"A close beyond {level:.0f} cancelled the {side.lower()} scenario. Re-read the updated forecast.", "", NOT_ADVICE_EN])


def status_message(settings, rows, now, lang="en") -> str:
    if lang == "hi":
        lines = [f"[{settings.mode_label}]", f"STATUS | {_stamp(now)}", "Orders: BAND (yeh system kabhi trade nahi karta)",
                 "Telegram: jud gaya | Groww: abhi nahi juda (planned)"]
        for inst, ds, price in rows:
            lines.append(f"{inst}: aakhri close {price:.2f}, candle {ds['last_candle_end']:%d %b %H:%M} par band hui, "
                         f"data {H.DATA_STATE.get(ds['status'], ds['status'])}, market {H.MARKET_STATE.get(ds['market'], ds['market'])}")
        return "\n".join(lines)
    lines = [f"[{settings.mode_label}]", f"STATUS | {_stamp(now)}", "Orders: DISABLED (this system never trades)",
             "Telegram: connected | Groww: not connected (planned)"]
    for inst, ds, price in rows:
        lines.append(f"{inst}: last close {price:.2f}, candle ended {ds['last_candle_end']:%d %b %H:%M}, "
                     f"data {ds['status']}, market {ds['market']}")
    return "\n".join(lines)


def _ago(h, now_utc, lang="en"):
    m = max(0, int((now_utc - h.published).total_seconds() // 60))
    if lang == "hi":
        return f"{m} minute pehle" if m < 120 else f"{m // 60} ghante pehle"
    return f"{m} min ago" if m < 120 else f"{m // 60} h ago"


def news_alert_message(items, reactions, forecasts, now, now_utc, mode_label, lang="en") -> str:
    """items: [(Headline, classification)], reactions/forecasts: {instrument: ...}."""
    dirs = [c["direction"] for _, c in items]
    d = "NEGATIVE" if dirs.count("NEGATIVE") > dirs.count("POSITIVE") else "POSITIVE" if dirs.count("POSITIVE") > dirs.count("NEGATIVE") else "UNCLEAR"
    hi = lang == "hi"
    dl = (lambda x: H.DIRECTION.get(x, x)) if hi else (lambda x: x)
    lines = [f"[{mode_label}]", f"NEWS ALERT | {_stamp(now)}",
             (f"Nayi khabron ka overall tone: {dl(d)}" if hi else f"Overall tone of new headlines: {d}"), ""]
    for n, (h, c) in enumerate(items, 1):
        lines += [f"{n}. [{dl(c['direction'])}] {h.title}", f"   {h.source}, {_ago(h, now_utc, lang)}", f"   {h.link}"]
    lines += ["", "Price ne ab tak kaise react kiya:" if hi else "Price reaction so far:"]
    for inst, r in reactions.items():
        if not r.get("known"):
            lines.append(f"  {inst}: abhi tulna karne layak live session data nahi" if hi else f"  {inst}: no live session data to compare yet")
            continue
        if hi:
            verdict = ("price khabar ki hi disha mein chal raha hai" if r["confirming"] else
                       "price abhi khabar ko confirm NAHI kar raha" if d != "UNCLEAR" else "disha saaf nahi")
            lines.append(f"  {inst} {r['price']:.0f}: open se {r['from_open_pct']:+.2f}%, pichli 3 candles {r['last3_atr']:+.1f} ATR - {verdict}")
        else:
            verdict = ("price IS moving the same way as the news" if r["confirming"] else
                       "price is NOT confirming yet" if d != "UNCLEAR" else "direction unclear")
            lines.append(f"  {inst} {r['price']:.0f}: {r['from_open_pct']:+.2f}% from open, last 3 candles {r['last3_atr']:+.1f} ATR - {verdict}")
    for inst, f in forecasts.items():
        if d == "NEGATIVE":
            sc, side = f["scenarios"]["bearish"], ("neeche" if hi else "downside")
        elif d == "POSITIVE":
            sc, side = f["scenarios"]["bullish"], ("upar" if hi else "upside")
        else:
            lines.append(f"  {inst}: support {f['support']:.0f} | resistance {f['resistance']:.0f}")
            continue
        lv = ", ".join(f"{t['label']} {t['level']:.0f}" for t in sc["targets"][:2])
        if hi:
            lines.append(f"  {inst} forecast ke {side} wale zones agar move jaari rahe: {lv}. {_x(lang, sc['invalidation'])}")
        else:
            lines.append(f"  {inst} forecast {side} zones if the move continues: {lv}. {sc['invalidation']}")
    if hi:
        lines += ["", "Kya karein: system aapke liye kuch kharidta ya bechta nahi. Price confirmation ka intezaar karein (upar ke levels ke paar 15m close) "
                  "aur normal signal alerts dekhein; khabar se market jaldi palat sakta hai.", NEWS_CAVEAT_HI, NOT_ADVICE_HI]
    else:
        lines += ["", "What to do: nothing is bought or sold for you. Wait for price confirmation (a 15m close beyond the levels above) "
                  "and use the normal signal alerts; news can reverse quickly.", NEWS_CAVEAT_EN, NOT_ADVICE_EN]
    return "\n".join(lines)


def context_message(rows, notes, heads, now, now_utc, mode_label, errors=(), lang="en") -> str:
    hi = lang == "hi"
    lines = [f"[{mode_label}]", f"MARKET CONTEXT | {_stamp(now)}", ""]
    for r in rows:
        lines.append(f"  {r['name']:<12} {r['last']:>10.2f}  ({r['pct']:+.2f}%)")
    if notes:
        lines += ["", "Matlab:" if hi else "Reading:"] + [f"  - {_x(lang, n)}" for n in notes]
    if heads:
        lines += ["", "Taaza market headlines:" if hi else "Latest market headlines:"]
        for h, c in heads:
            lines.append(f"  - [{c['direction'][:3]}] {h.title} ({h.source}, {_ago(h, now_utc, lang)})")
    if errors:
        lines += ["", ("Abhi uplabdh nahi: " if hi else "Unavailable right now: ") + ", ".join(errors[:4])]
    lines += ["", "Yeh sirf jaankari hai; forecast ke numbers nahi badalti." if hi else
              "Context is information only; it does not change the forecast numbers.", news_caveat(lang)]
    return "\n".join(lines)


HELP_EN = """Commands:
/status - connection, data freshness, mode
/forecast - NIFTY and BANKNIFTY forecast
/plan - two-sided trade plan with entry, stop and all targets
/check - check YOUR idea, e.g. /check nifty call 23300 sl 23270 target 23360
/nifty - full NIFTY forecast
/banknifty - full BANKNIFTY forecast
/signals - today's signals and current check
/report - end-of-day report (or so far)
/news - latest market headlines
/context - global markets, VIX, crude, rupee + headlines
/explain - why the forecast says what it says
/lang hi | /lang en - switch the language of all messages
/help - this list

Automatic: 09:00 pre-market forecast, 09:05 market context, ~09:35 opening range update, confirmed setups / reversals /
news alerts only during the day, 15:20 quick summary, ~15:50 end-of-day report. All messages are PAPER/TEST MODE."""

HELP_HI = """Commands:
/status - connection, data kitna taaza hai, mode
/forecast - NIFTY aur BANKNIFTY ka forecast
/plan - dono taraf ka trade plan: entry, stop loss aur saare targets
/check - APNA idea check karein, jaise: /check nifty call 23300 sl 23270 target 23360
/nifty - NIFTY ka poora forecast
/banknifty - BANKNIFTY ka poora forecast
/signals - aaj ke signals aur abhi ki jaanch
/report - din ki report (ya ab tak ki)
/news - taaza market headlines
/context - duniya ke market, VIX, crude, rupaya + headlines
/explain - forecast aisa kyun keh raha hai
/lang hi | /lang en - saare messages ki bhasha badlein
/help - yeh list

Apne aap: 9:00 market khulne se pehle ka forecast, 9:05 market context, ~9:35 opening range update, din mein sirf confirmed
setup / reversal / news alert, 3:20 chhota summary, ~3:50 din ki poori report. Saare messages PAPER/TEST MODE mein hain.
BUY signal = CALL (CE) side ka idea, SELL signal = PUT (PE) side ka idea. Strike aur premium aap khud chunein."""

HELP = HELP_EN


def help_text(lang="en"):
    return HELP_HI if lang == "hi" else HELP_EN

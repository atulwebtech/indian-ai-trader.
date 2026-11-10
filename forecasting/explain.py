"""Plain-language explanations built from the engine's own numbers (no AI service, no API cost).

Every sentence is assembled from fields the forecast / reversal / signal engines already computed, so
the text can never say something the numbers do not support. Languages: "en" and "hi" (Hinglish).

AI_PROVIDER at the bottom is the optional hook for an external language model. It is OFF and has
no implementation: adding one needs a paid API key and per-call cost, which must be discussed first.
"""

T = {
    "en": {
        "bias": "Why {b}: {n} of {of} checks point that way (score {s:+d}). ",
        "bias_neutral": "Why NEUTRAL: the checks disagree (score {s:+d} of {of}), so no side has a clear edge. ",
        "reason": "{sign} {text}.",
        "target": "{label} {level:.0f}: important because {src}. ",
        "target_rate": "In the past, price moved this far from the open on about {r:.0%} of days. ",
        "target_cond": "To get there: {needs}. ",
        "rev_none": "No reversal is confirmed. A touch of a level is not enough on its own. ",
        "rev_state": "Reversal state: {state}. Confirmed so far: {met}. Still missing: {missing}. ",
        "invalid": "What cancels this view: {inv}. ",
        "avoid": "Why no trade now: {why}. ",
        "signal": "Signal: {a} (checklist score {q:.0f}/100, which is not a chance of winning). ",
        "caveat": "These are estimates from past data; no target is guaranteed. Backtests found no proven trading edge.",
        "stale": "Note: data is {st}; treat the numbers with care. ",
    },
    "hi": {
        "bias": "{b} kyun: {of} mein se {n} checks isi taraf ishara karte hain (score {s:+d}). ",
        "bias_neutral": "NEUTRAL kyun: checks aapas mein sahmat nahi hain (score {s:+d}/{of}), isliye kisi taraf saaf badhat nahi. ",
        "reason": "{sign} {text}.",
        "target": "{label} {level:.0f}: yeh isliye khaas hai kyunki {src}. ",
        "target_rate": "Pehle ke dinon mein price open se itna door lagbhag {r:.0%} dinon par gaya. ",
        "target_cond": "Wahan tak pahunchne ke liye: {needs}. ",
        "rev_none": "Abhi koi reversal confirm nahi hai. Sirf level chhoo lena kaafi nahi hota. ",
        "rev_state": "Reversal ki sthiti: {state}. Ab tak mile: {met}. Abhi baaki: {missing}. ",
        "invalid": "Yeh soch kab galat maani jayegi: {inv}. ",
        "avoid": "Abhi trade kyun nahi: {why}. ",
        "signal": "Signal: {a} (checklist score {q:.0f}/100, yeh jeetne ki sambhavna nahi hai). ",
        "caveat": "Yeh purane data par based andaaze hain; koi target guaranteed nahi hai. Backtest mein koi pakka trading edge nahi mila.",
        "stale": "Dhyan: data {st} hai; numbers ko savdhani se dekhiye. ",
    },
}

# translations of the engine's fixed vocabulary for Hinglish (English text passes through)
HI_WORDS = {"BULLISH": "BULLISH (tezi)", "BEARISH": "BEARISH (mandi)", "NEUTRAL": "NEUTRAL",
            "POTENTIAL_REVERSAL": "sambhavit reversal", "CONFIRMED_REVERSAL": "confirmed reversal",
            "NO_CONFIRMATION": "koi confirmation nahi", "FALSE_BREAKOUT": "false breakout",
            "TREND_CONTINUATION": "trend jaari"}


def _w(word, lang):
    if lang == "hi" and word in HI_WORDS:
        return HI_WORDS[word]
    return word.replace("_", " ").lower()


def explain_bias(f, lang="en"):
    t, b = T[lang], f["bias"]
    agree = sum(1 for sign, _ in b["reasons"] if (sign == "+") == (b["bias"] == "BULLISH") and sign != "0") if b["bias"] != "NEUTRAL" else 0
    head = (t["bias"].format(b=HI_WORDS[b["bias"]] if lang == "hi" else b["bias"], n=agree, of=b["of"], s=b["score"])
            if b["bias"] != "NEUTRAL" else t["bias_neutral"].format(s=b["score"], of=b["of"]))
    return head + " ".join(t["reason"].format(sign=s, text=txt) for s, txt in b["reasons"])


def explain_targets(f, lang="en", n=2):
    t, out = T[lang], []
    for key in ("bullish", "bearish"):
        sc = f["scenarios"][key]
        for tg in sc["targets"][:n]:
            src = "; ".join(tg["sources"][:3])
            s = t["target"].format(label=f"{key.capitalize()} {tg['label']}", level=tg["level"], src=src)
            if tg.get("historical_reach_rate") is not None:
                s += t["target_rate"].format(r=tg["historical_reach_rate"])
            out.append(s.strip())
        needs = next((c["needs"] for c in sc["conditions"][:1]), [])
        out.append(t["target_cond"].format(needs="; ".join(needs)).strip())
    return "\n".join(out)


def explain_reversal(r, lang="en"):
    t = T[lang]
    p = r["primary"]
    if p["state"] in ("NO_CONFIRMATION", "TREND_CONTINUATION") and not p["still_needed"]:
        return t["rev_none"]
    met = [e["detail"] for e in p["evidence"] + p["structural"] if e["met"]] or ["none"]
    missing = p["still_needed"] or ["nothing"]
    return t["rev_state"].format(state=_w(p["state"], lang), met="; ".join(met), missing=", ".join(missing).replace("_", " "))


def explain_invalidation(f, lang="en"):
    parts = f["global_invalidation"][:1] + [f["scenarios"]["bullish"]["invalidation"], f["scenarios"]["bearish"]["invalidation"]]
    return T[lang]["invalid"].format(inv=" ".join(x.rstrip(".") + "." for x in parts)).replace("..", ".").rstrip(". ") + "."


def explain_signal(sig, lang="en"):
    t = T[lang]
    if sig["action"] in ("BUY", "SELL"):
        return t["signal"].format(a=sig["action"], q=sig["quality_score"]) + "; ".join(sig["reasons"][:3])
    why = "; ".join(sig.get("reasons", [])[:2]) or "no confirmed setup"
    return f"Signal: {sig['action']}. " + t["avoid"].format(why=why)


def explain(f, reversal=None, signal=None, lang="en", data_status=None) -> str:
    parts = [f"{f['instrument']} ({f['reference_price']:.0f})", explain_bias(f, lang), explain_targets(f, lang)]
    if reversal:
        parts.append(explain_reversal(reversal, lang))
    if signal:
        parts.append(explain_signal(signal, lang))
    parts.append(explain_invalidation(f, lang))
    if data_status and data_status.get("status") in ("STALE",):
        parts.insert(1, T[lang]["stale"].format(st=data_status["status"]).strip())
    parts.append(T[lang]["caveat"])
    return "\n\n".join(parts)


class AIProvider:
    """Optional external language-model hook. Not implemented on purpose (needs a paid API key)."""
    enabled = False

    def polish(self, text: str) -> str:
        raise NotImplementedError("External AI explanations are not enabled. See README, 'Optional AI'.")


AI_PROVIDER = AIProvider()

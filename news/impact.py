"""Crude, transparent headline classifier (keywords only).

It answers two questions: does this headline concern the MARKET AS A WHOLE (not one company), and does
it read negative or positive? It cannot understand context, sarcasm or numbers and it is NOT validated
by any backtest (historical headlines are not available for free). Treat its output as a prompt for a
human to read the headline, never as a trading signal.
"""
import re

MARKET = re.compile(
    r"\b(nifty|sensex|bank ?nifty|dalal street|indian (?:stock |equity )?markets?|stock markets?|markets?|equities|rbi|"
    r"reserve bank|sebi|fiis?|fpis?|rupee|wall street|fed|federal reserve|fomc|s&p ?500|nasdaq|dow|crude|oil|brent|opec|"
    r"tariffs?|treasury yields?|bond yields?|inflation|gdp|budget|recession|ecb|boj|geopolitic\w*|war|sanctions?)\b", re.I)

# EVENTS are causes (a war, a rate decision, sanctions). They can move the market whatever price did so far.
EVENT_NEG = re.compile(
    r"\b(war|attacks?|attacked|missiles?|invasion|terror\w*|(?:debt|loan|bond|sovereign|payment|credit) default|defaults? on (?:its |their |the )?(?:debt|loans?|bonds?|payments?|obligations)|defaulted on|bank run|downgrade[sd]?|sanctions?|tariffs?|"
    r"emergency|lockdown|rate hikes?|hawkish|recession|crisis|shutdown|circuit breaker|trading halt|black (?:monday|swan)|"
    r"collapse[sd]?)\b", re.I)
EVENT_POS = re.compile(r"\b(ceasefire|peace deal|rate cuts?|stimulus|trade deal|dovish)\b", re.I)
# MOVES are consequences (price already moved). Only an extreme FALL in an Indian index headline counts as HIGH.
MOVE_NEG_EXTREME = re.compile(r"\b(crash(?:es|ed)?|plunge[sd]?|tumble[sd]?|rout|meltdown|bloodbath|freefall|nosedive[sd]?|panic)\b", re.I)
MOVE_NEG_SOFT = re.compile(r"\b(sell-?off|slump(?:s|ed)?|tank(?:s|ed)?)\b", re.I)
MOVE_POS = re.compile(r"\b(surge[sd]?|soar(?:s|ed)?|rall(?:y|ies|ied)|record high|rebound(?:s|ed)?|jumps?|jumped|upgrade[sd]?)\b", re.I)
MILD_NEG = re.compile(r"\b(fall(?:s|en)?|drops?|dropped|slips?|slipped|declines?|declined|weak|lower|loses|lost|dips?)\b", re.I)
MILD_POS = re.compile(r"\b(rises?|rose|gains?|gained|higher|advances?|climbs?|firm|strong)\b", re.I)
INDIA = re.compile(r"\b(nifty|sensex|bank ?nifty|dalal street|indian (?:stock |equity )?markets?|rbi|reserve bank|sebi|rupee|fiis?|fpis?|india)\b", re.I)
SCHEDULED = re.compile(r"\b(rbi policy|repo rate|monetary policy|fomc|fed decision|interest rate decision|union budget|cpi data|gdp data)\b", re.I)


NOT_MARKET_SENSE = re.compile(r"\b(cyber\w*|hack\w*|ransomware|heart attack|panic attack|data breach)\b", re.I)


def event_topics(title: str) -> frozenset:
    """Normalised cause words found in the title, used to avoid re-alerting the same story all day."""
    t = EVENT_NEG.findall(title) + EVENT_POS.findall(title)
    norm = {"attacks": "attack", "attacked": "attack", "missiles": "missile", "tariffs": "tariff", "sanctions": "sanction",
            "rate hikes": "rate hike", "rate cuts": "rate cut", "collapsed": "collapse", "collapses": "collapse"}
    return frozenset(norm.get(x.lower(), x.lower()) for x in t if isinstance(x, str))


def classify(title: str) -> dict:
    """{'severity': HIGH|MEDIUM|LOW, 'direction': NEGATIVE|POSITIVE|UNCLEAR, 'kind': EVENT|MOVE|SCHEDULED|NONE, ...}

    HIGH = a market-wide EVENT (war, rate decision, sanctions...), a scheduled macro event, or an extreme
    FALL reported for an Indian index. Routine rally/surge reports are at most MEDIUM and never alert."""
    market = bool(MARKET.search(title))
    india = bool(INDIA.search(title))
    cyber = bool(NOT_MARKET_SENSE.search(title))
    ev_n, ev_p = (0 if cyber else len(EVENT_NEG.findall(title))), len(EVENT_POS.findall(title))
    mv_x, mv_s, mv_p = len(MOVE_NEG_EXTREME.findall(title)), len(MOVE_NEG_SOFT.findall(title)), len(MOVE_POS.findall(title))
    neg_m, pos_m = len(MILD_NEG.findall(title)), len(MILD_POS.findall(title))
    sched = bool(SCHEDULED.search(title))
    neg = (ev_n + mv_x) * 2 + mv_s * 2 + neg_m
    pos = (ev_p + mv_p) * 2 + pos_m
    direction = "NEGATIVE" if neg > pos else "POSITIVE" if pos > neg else "UNCLEAR"
    if not market:
        return {"severity": "LOW", "direction": direction, "kind": "NONE", "why": ["not about the market as a whole"], "india": False}
    if ev_n or ev_p:
        return {"severity": "HIGH", "direction": direction, "kind": "EVENT", "why": ["market-moving event"], "india": india,
                "topics": event_topics(title)}
    if sched:
        return {"severity": "HIGH", "direction": "UNCLEAR", "kind": "SCHEDULED", "why": ["scheduled macro event"], "india": india}
    if mv_x and india and direction == "NEGATIVE":
        return {"severity": "HIGH", "direction": "NEGATIVE", "kind": "MOVE", "why": ["extreme fall in an Indian index"], "india": True}
    if mv_x or mv_s or mv_p or neg_m or pos_m:
        return {"severity": "MEDIUM", "direction": direction, "kind": "MOVE", "why": ["market move reported"], "india": india}
    return {"severity": "LOW", "direction": direction, "kind": "NONE", "why": [], "india": india}


def qualifies(fresh: list) -> list:
    """Which fresh HIGH headlines deserve an alert: India-specific ones, or any that are backed by at least
    one other HIGH headline pointing the same way (so a single mis-read headline cannot fire an alert)."""
    high = [(h, c) for h, c in fresh if c["severity"] == "HIGH"]
    out = []
    for h, c in high:
        same = [x for x in high if x[0].id != h.id and c["direction"] != "UNCLEAR" and x[1]["direction"] == c["direction"]]
        if c["india"] or same:
            out.append((h, c))
    return out


def price_reaction(intraday, direction: str) -> dict:
    """How price has behaved today. 'confirming' = price already moves the way the headline reads."""
    day = intraday[intraday.index.date == intraday.index[-1].date()]
    if day.empty or len(day) < 4:
        return {"known": False}
    from indicators.volatility import atr
    a = float(atr(intraday).iloc[-1])
    last = float(day["close"].iloc[-1])
    from_open = 100 * (last - float(day["open"].iloc[0])) / float(day["open"].iloc[0])
    move_atr = (last - float(day["close"].iloc[-4])) / a if a else 0.0
    confirm = ((direction == "NEGATIVE" and (from_open <= -0.3 or move_atr <= -1.0)) or
               (direction == "POSITIVE" and (from_open >= 0.3 or move_atr >= 1.0)))
    return {"known": True, "price": last, "from_open_pct": from_open, "last3_atr": move_atr,
            "confirming": bool(confirm and direction != "UNCLEAR")}

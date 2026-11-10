"""Hinglish (Hindi in Roman letters) for Telegram.

Trading words (entry, stop loss, target, support, resistance, call, put, candle, breakout) stay in English, as Indian
traders say them. `tr()` turns the engine's fixed English sentences into Hinglish; anything it does not recognise is
left as it is, so no information is ever lost or invented.
"""
import re

ROLE = {"PRIMARY": "mukhya", "ALTERNATE": "vikalp"}
BIAS = {"BULLISH": "BULLISH (tezi)", "BEARISH": "BEARISH (mandi)", "NEUTRAL": "NEUTRAL (koi saaf disha nahi)"}
CONVICTION = {"high": "zyada", "moderate": "madhyam", "low": "kam"}
TITLE = {"FORECAST": "FORECAST", "PRE-MARKET FORECAST": "MARKET KHULNE SE PEHLE KA FORECAST",
         "OPENING RANGE UPDATE": "OPENING RANGE UPDATE"}
DIRECTION = {"NEGATIVE": "NEGATIVE (buri)", "POSITIVE": "POSITIVE (achhi)", "UNCLEAR": "UNCLEAR (saaf nahi)"}
VS_RANGE = {"above": "ke upar", "below": "ke neeche", "inside": "ke andar"}
MARKET_STATE = {"OPEN": "khula", "CLOSED": "band", "PRE_MARKET": "khulne se pehle", "HOLIDAY": "chhutti", "SPECIAL_SESSION": "special session"}
DATA_STATE = {"FRESH": "taaza", "STALE": "purana", "CLOSED": "market band"}
MOOD = {"BULLISH": ("🟢", "POSITIVE", "upar ki taraf jhukaav"), "BEARISH": ("🔴", "NEGATIVE", "neeche ki taraf jhukaav"),
        "NEUTRAL": ("🟡", "SIDEWAYS", "koi saaf disha nahi, range mein rehne ke chance")}
EXIT_HINT = {"TARGET1 HIT": "Aadha hissa book karein aur bacha hua stop entry par le aayein (nuksan nahi hoga).",
             "TARGET2 HIT": "Baaki hissa bhi book karein. Trade poora.",
             "STOPPED": "Stop loss laga: trade se nikal jayein. Wapas entry na lein.",
             "EXPIRED": "Signal ki samay seema khatam: ab is signal par naya entry na lein."}
STATUS = {"ACTIVE": "chalu", "TARGET1 HIT": "pehla target (T1) mila", "TARGET2 HIT": "dusra target (T2) mila",
          "STOPPED": "stop loss laga", "EXPIRED": "samay khatam"}

_P = [  # (regex, replacement) - first match wins, anchored on the whole sentence
    (r"^(\d+m) candle CLOSES above (\d+) \(opening-range high\)$", r"\1 candle ka close \2 (opening-range high) ke upar aaye"),
    (r"^(\d+m) candle CLOSES below (\d+) \(opening-range low\)$", r"\1 candle ka close \2 (opening-range low) ke neeche aaye"),
    (r"^After 09:30, a (\d+m) candle CLOSES above the opening-range high \(not known yet\)$",
     r"9:30 ke baad \1 candle ka close opening-range high ke upar aaye (range abhi bani nahi)"),
    (r"^After 09:30, a (\d+m) candle CLOSES below the opening-range low \(not known yet\)$",
     r"9:30 ke baad \1 candle ka close opening-range low ke neeche aaye (range abhi bani nahi)"),
    (r"^A (\d+m) close below (\d+) cancels the bullish scenario$", r"\1 candle \2 ke neeche band hui to bullish scenario cancel"),
    (r"^A (\d+m) close above (\d+) cancels the bearish scenario$", r"\1 candle \2 ke upar band hui to bearish scenario cancel"),
    (r"^Price keeps rotating between (\d+) and (\d+) without a (\d+m) close beyond either$",
     r"Price \1 aur \2 ke beech ghoomta rahe, \3 close kisi bhi taraf bahar na ho"),
    (r"^A (\d+m) close outside (\d+)-(\d+) with follow-through ends the range view$",
     r"\1 close \2-\3 ke bahar follow-through ke saath aaye to range wali soch khatam"),
    (r"^Last traded bar close$", "aakhri band candle ka close"),
    (r"^Previous day close \(pre-market reference\)$", "pichle din ka close (pre-market reference)"),
    # signal reasons / invalidation
    (r"^(Up|Down)side break of opening range at (\d+) confirmed by a (\d+m) close$",
     lambda m: f"Opening range ({m[2]}) ka {'upar' if m[1] == 'Up' else 'neeche'} ki taraf breakout, {m[3]} close se confirm"),
    (r"^Break is (\d+) bar\(s\) old$", r"Breakout \1 candle purana hai"),
    (r"^Forecast bias (\w+); MACD and EMA agree$", r"Forecast bias \1; MACD aur EMA saath hain"),
    (r"^Confirmed (bearish|bullish) reversal at (.+)$", r"Confirmed \1 reversal, level: \2"),
    (r"^A (\d+m) close back (below|above) (\d+) \(stop\) cancels the idea$",
     lambda m: f"{m[1]} candle wapas {m[3]} (stop) ke {'neeche' if m[2] == 'below' else 'upar'} band hui to idea cancel"),
    (r"^A close (above|below) ([\d.]+) cancels this (bearish|bullish) reversal idea$",
     lambda m: f"Close {m[2]} ke {'upar' if m[1] == 'above' else 'neeche'} aaya to yeh {m[3]} reversal idea cancel"),
    (r"^(bullish|bearish) rejection/engulfing candle in the last (\d+) bars$", r"pichli \2 candles mein \1 rejection/engulfing candle"),
    (r"^Close crossed (above|below) the session average price$",
     lambda m: f"Close din ke average price ke {'upar' if m[1] == 'above' else 'neeche'} nikla"),
    (r"^RSI divergence \((\d+)\)$", r"RSI divergence (\1)"),
    (r"^RSI rolling over from extreme \((\d+)\)$", r"RSI extreme se palat raha hai (\1)"),
    (r"^Close (below|above) a turning EMA 9$", lambda m: f"Close palatte hue EMA 9 ke {'neeche' if m[1] == 'below' else 'upar'}"),
    (r"^Moved ([\d.]+) ATR away from the extreme \(needs ([\d.]+)\)$", r"Extreme se \1 ATR door gaya (kam se kam \2 chahiye)"),
    (r"^Relative volume ([\d.]+)$", r"Relative volume \1"),
    (r"^Close beyond the last confirmed swing$", "Close pichle confirmed swing ke paar gaya"),
    (r"^Broke the recent range and closed back inside$", "Recent range todi aur wapas andar band hui"),
    # why no signal
    (r"^Outside entry window ([\d:]+)-([\d:]+) IST$", r"Entry window (\1-\2 IST) ke bahar"),
    (r"^Not a regular trading day \((\w+)\)$", r"Aaj regular trading din nahi hai (\1)"),
    (r"^Daily limit of (\d+) signals across both indices reached$", r"Dono index milake din ki \1 signals ki limit poori"),
    (r"^Daily signal cap reached \((\d+)\)$", r"Is index ke liye din ki signal limit (\1) poori"),
    (r"^Cooldown: (\d+) min left after last signal$", r"Cooldown: pichle signal ke baad \1 minute baaki"),
    (r"^Daily loss limit hit \((.+)\)$", r"Din ki loss limit lag gayi (\1)"),
    (r"^(\d+) consecutive losses: stopped for the day$", r"Lagatar \1 loss: aaj ke liye band"),
    (r"^Choppy market: ADX < 15 and range contracting$", "Market choppy hai: ADX 15 se kam aur range sikud rahi hai"),
    (r"^Duplicate of an active signal$", "Pehle se chalu signal jaisa hi hai"),
    (r"^No confirmed setup yet$", "Abhi koi confirmed setup nahi hai"),
    (r"^Stop too wide \((.+)\)$", r"Stop bahut door hai (\1)"),
    (r"^Fewer than two target zones ahead of price$", "Price ke aage do target zones nahi mile"),
    (r"^Reward:risk too low \((.+)\)$", r"Reward:risk kam hai (\1)"),
    (r"^Opening-range break is stale \(older than 3 bars\)$", "Opening-range breakout purana ho chuka hai (3 candle se zyada)"),
    (r"^Price already ([\d.]+) ATR beyond the break: do not chase$", r"Price breakout se pehle hi \1 ATR aage nikal gaya: peeche na bhaagein"),
    (r"^Opening range not complete$", "Opening range abhi poori nahi hui"),
    (r"^Opening range not formed yet$", "Opening range abhi bani nahi"),
    (r"^No signal before the 09:30 entry window$", "9:30 ke entry window se pehle koi signal nahi"),
    (r"^Opening range (high|low) ([\d.]+) broke (\d+) bar\(s\) ago, was retested and held$",
     lambda m: f"Opening range {'high' if m[1] == 'high' else 'low'} {m[2]} {m[3]} candle pehle toota, retest hua aur level tika raha"),
    (r"^Bounce bar closed (above|below) the level on the (\d+m) chart$",
     lambda m: f"Bounce wali candle {m[2]} chart par level ke {'upar' if m[1] == 'above' else 'neeche'} band hui"),
    (r"^(\w+) setup present but quality (\d+) < (\d+) or only (\d+) strong components \(< (\d+)\)$",
     r"\1 setup hai, par quality \2 (chahiye \3) ya sirf \4 mazboot hisse (chahiye \5)"),
    (r"^Scanning every 15 minutes; alerts only for confirmed setups$", "Har 15 minute scan hota hai; alert sirf confirmed setup par"),
    # market context readings
    (r"^US markets closed (clearly lower|lower|higher|little changed) \(avg ([+-][\d.]+)%\)$",
     lambda m: f"US market {({'clearly lower': 'kaafi neeche', 'lower': 'neeche', 'higher': 'upar', 'little changed': 'lagbhag sapaat'})[m[1]]} band hue (average {m[2]}%)"),
    (r"^Asia (weak|strong|mixed) \(avg ([+-][\d.]+)%\)$",
     lambda m: f"Asia {({'weak': 'kamzor', 'strong': 'mazboot', 'mixed': 'mila-jula'})[m[1]]} (average {m[2]}%)"),
    (r"^India VIX ([\d.]+) \(([+-][\d.]+)%\): (.+)$",
     lambda m: f"India VIX {m[1]} ({m[2]}%): " + {"fear rising, bigger swings likely": "darr badh raha hai, badi hilchal ho sakti hai",
                                                   "fear falling": "darr ghat raha hai", "no big change": "koi badi badlav nahi"}.get(m[3], m[3])),
    (r"^Brent crude moved ([+-][\d.]+)% \(large\)$", r"Brent crude \1% hila (bada move)"),
    (r"^Rupee moved sharply: USD/INR ([+-][\d.]+)%$", r"Rupaya tezi se hila: USD/INR \1%"),
]
_COMPILED = [(re.compile(p), r) for p, r in _P]


def tr(text) -> str:
    """Hinglish version of one engine sentence (unchanged if the sentence is not recognised)."""
    if not isinstance(text, str):
        return text
    for rx, rep in _COMPILED:
        m = rx.match(text)
        if m:
            return rep(m) if callable(rep) else rx.sub(rep, text)
    return text


def tr_many(items) -> list:
    return [tr(x) for x in items]

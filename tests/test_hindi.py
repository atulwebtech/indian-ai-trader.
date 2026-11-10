import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from forecasting.daily_forecast import generate_forecast
from market_data.loader import data_status
from news import feeds, impact
from paper_trading import scorecard as sc
from reversal_detection import detector as rev
from telegram_bot import hindi as H
from telegram_bot import messages as M
from tests.test_news import H as headline
from tests.test_scorecard import card
from tests.test_telegram import DAILY, INTRA, T, make_bot

# English engine sentences that must never reach a Hinglish message untranslated
ENGLISH_LEFTOVERS = ["CLOSES", "cancels", "Cancelled if", "Previous day close", "Last traded", "Outside entry window",
                     "Not a regular", "min left after last signal", "rotating between", "ends the range", "confirmed by a", "Signal status",
                     "Reason:", "Why:", "Invalid if", "Valid for about", "do not chase", "No signal before", "Daily loss limit hit",
                     "Opening range not", "setup present but", "What to do", "Price reaction so far", "Paper/test information only"]


def nums(text):
    return sorted(set(re.findall(r"\d{4,6}", text)))


def make_forecast():
    cut = INTRA["NIFTY"].loc[:"2026-01-29 11:00"]
    cut.attrs.update(INTRA["NIFTY"].attrs)
    f = generate_forecast("NIFTY", cut, DAILY["NIFTY"][DAILY["NIFTY"].index.date < pd.Timestamp("2026-01-29").date()])
    ds = data_status(cut, T("2026-01-29 11:02"))
    return f, ds, cut


def fake_signal(action):
    buy = action == "BUY"
    return {"mode_label": "PAPER MODE - MOCK DATA", "instrument": "NIFTY", "action": action, "setup": "ORB", "quality_score": 74,
            "entry_zone": [23060, 23075] if buy else [22985, 23000], "stop": 23025 if buy else 23030,
            "targets": [{"level": 23110 if buy else 22950, "rr": 1.0}, {"level": 23150 if buy else 22905, "rr": 1.94}],
            "reasons": ["Upside break of opening range at 23060 confirmed by a 15m close", "Break is 1 bar(s) old",
                        "Forecast bias BULLISH; MACD and EMA agree"],
            "invalidation": "A 15m close back below 23025 (stop) cancels the idea", "valid_for_bars": 8, "entry_ref": 23065.0,
            "id": "x"}


@pytest.mark.parametrize("en,hi", [
    ("15m candle CLOSES above 22980 (opening-range high)", "15m candle ka close 22980 (opening-range high) ke upar aaye"),
    ("A 15m close below 23020 cancels the bullish scenario", "15m candle 23020 ke neeche band hui to bullish scenario cancel"),
    ("Previous day close (pre-market reference)", "pichle din ka close (pre-market reference)"),
    ("Cooldown: 30 min left after last signal", "Cooldown: pichle signal ke baad 30 minute baaki"),
    ("Outside entry window 09:30-14:45 IST", "Entry window (09:30-14:45 IST) ke bahar"),
    ("A 15m close back below 23025 (stop) cancels the idea", "15m candle wapas 23025 (stop) ke neeche band hui to idea cancel"),
    ("Upside break of opening range at 23060 confirmed by a 15m close", "Opening range (23060) ka upar ki taraf breakout, 15m close se confirm"),
    ("US markets closed clearly lower (avg -2.73%)", "US market kaafi neeche band hue (average -2.73%)"),
])
def test_known_sentences_translate_exactly(en, hi):
    assert H.tr(en) == hi


def test_unknown_text_is_left_alone_not_invented():
    assert H.tr("Some brand new sentence 123") == "Some brand new sentence 123"
    assert H.tr(None) is None


def test_signal_message_says_call_or_put_and_keeps_every_number():
    f, ds, _ = make_forecast()
    now = T("2026-01-29 11:02")
    for action, word, other in (("BUY", "CALL (CE)", "PUT (PE)"), ("SELL", "PUT (PE)", "CALL (CE)")):
        s = fake_signal(action)
        hi, en = M.signal_message(s, f, ds, now, "hi"), M.signal_message(s, f, ds, now, "en")
        assert word in hi.splitlines()[1] and other not in hi.splitlines()[1]
        for needle in ("Kya karein", "Strike, expiry aur premium", "Entry zone (index level)", "Stop loss (index level)", "Target (index level)",
                       "Kyun:", "Kab galat:", "PAPER MODE", "salaah nahi", "Backtest chetavni"):
            assert needle in hi, needle
        assert nums(hi) == nums(en)                                  # same levels in both languages
        assert not [w for w in ENGLISH_LEFTOVERS if w in hi]


def test_forecast_message_hinglish_has_no_leftover_english_and_same_numbers():
    f, ds, _ = make_forecast()
    now = T("2026-01-29 11:02")
    for title in ("PRE-MARKET FORECAST", "OPENING RANGE UPDATE", "FORECAST"):
        hi = M.forecast_message(f, ds, now, signal={"action": "NO TRADE", "reasons": ["Cooldown: 30 min left after last signal"]},
                                title=title, lang="hi")
        en = M.forecast_message(f, ds, now, signal={"action": "NO TRADE", "reasons": ["Cooldown: 30 min left after last signal"]},
                                title=title, lang="en")
        assert not [w for w in ENGLISH_LEFTOVERS if w in hi], [w for w in ENGLISH_LEFTOVERS if w in hi]
        assert nums(hi) == nums(en)
        for needle in ("Bias:", "Support", "Resistance", "Cancel kab", "Signal sthiti", "Wajah"):
            assert needle in hi


def test_all_other_messages_have_hinglish_versions():
    f, ds, cut = make_forecast()
    now = T("2026-01-29 11:02")
    r = rev.detect_reversal(cut, rev.levels_from_forecast(f))
    msgs = [M.reversal_message("NIFTY", r, f, ds, now, "hi"), M.invalidation_message("NIFTY", "BULLISH", 23000.0, f, ds, now, "hi"),
            M.status_update_message(fake_signal("BUY"), "TARGET1_HIT", now, "hi"),
            M.status_message(type("S", (), {"mode_label": "PAPER MODE (MOCK DATA)"})(), [("NIFTY", ds, 23000.0)], now, "hi")]
    for m in msgs:
        assert "PAPER MODE" in m
        assert not [w for w in ENGLISH_LEFTOVERS if w in m], (m, [w for w in ENGLISH_LEFTOVERS if w in m])
    assert "CALL (CE)" in msgs[2] and "pehla target" in msgs[2]
    assert "BAND" in msgs[3]


def test_news_and_context_messages_hinglish():
    f, ds, cut = make_forecast()
    now = T("2026-01-29 11:02")
    now_utc = now.astimezone(timezone.utc)
    h = headline("Sensex crashes as war fears hit markets", 15, now_utc, 1)
    items = [(h, impact.classify(h.title))]
    react = {"NIFTY": impact.price_reaction(cut, "NEGATIVE"), "BANKNIFTY": {"known": False}}
    m = M.news_alert_message(items, react, {"NIFTY": f}, now, now_utc, "PAPER MODE (MOCK DATA)", "hi")
    for needle in ("NEWS ALERT", "Nayi khabron ka overall tone", "NEGATIVE (buri)", "Price ne ab tak kaise react kiya", "Kya karein", "minute pehle", "Sensex crashes"):
        assert needle in m, needle
    assert "What to do" not in m and "Price reaction so far" not in m
    rows = [{"name": "S&P 500", "last": 1.0, "prev": 1.0, "pct": -2.1}, {"name": "India VIX", "last": 16.0, "prev": 15.0, "pct": 7.0}]
    from news.context import read_context
    c = M.context_message(rows, read_context(rows), items, now, now_utc, "PAPER MODE (MOCK DATA)", (), "hi")
    assert "MARKET CONTEXT" in c and "darr badh raha hai" in c and "Matlab:" in c and "Reading:" not in c


def test_eod_report_hinglish():
    from paper_trading.scorecard import ScoreStore, cumulative, render_eod
    c = card()
    cum = cumulative([c])
    t = render_eod([c], cum, {}, "PAPER MODE (MOCK DATA)", "29 Jan 2026 15:55 IST", "", "hi")
    for needle in ("DIN KI REPORT", "Subah ka view", "Upar ke zones", "Neeche ke zones", "Din ke sabse bade moves", "TOOL KITNA ACCURATE RAHA",
                   "Sirf 1 din ka record hai", "salaah nahi"):
        assert needle in t, needle
    for bad in ("END-OF-DAY", "Pre-market view", "HOW ACCURATE", "Only 1 day"):
        assert bad not in t


def test_bot_defaults_to_hinglish_and_lang_command_switches_and_persists(tmp_path):
    from dataclasses import replace
    from config.settings import load_settings
    from telegram_bot.bot import Bot
    bot, _ = make_bot(tmp_path, now="2026-01-29 11:00")
    bot.set_lang("hi")
    start = bot.handle_command("/start")
    assert "Main kabhi order nahi lagata" in start and "BUY signal = CALL (CE)" in start
    assert "Maaf" not in bot.handle_command("/forecast")
    assert "English" in bot.handle_command("/lang en") and bot.lang == "en"
    assert "I never place orders" in bot.handle_command("/start")
    assert "Aise likhein" in bot.handle_command("/lang") or "Use /lang" in bot.handle_command("/lang")
    bot.handle_command("/lang hi")
    again = Bot(bot.client, replace(load_settings(), data_mode="MOCK", telegram_lang="en"), mode="MOCK", loader=bot.loader,
                state_dir=tmp_path, clock=bot.clock)
    assert again.lang == "hi"                                         # the saved choice beats the .env default
    fresh_default = Bot(bot.client, replace(load_settings(), data_mode="MOCK"), mode="MOCK", loader=bot.loader,
                        state_dir=tmp_path / "new", clock=bot.clock)
    assert fresh_default.lang == "hi"                                 # default is Hinglish


def test_every_command_works_in_hinglish(tmp_path):
    bot, _ = make_bot(tmp_path, now="2026-01-29 11:00")
    bot.set_lang("hi")
    bot.news_fetcher = lambda now=None, max_age_hours=12: ([], [])
    bot.context_fetcher = lambda: ([], [])
    for cmd in ("/start", "/help", "/status", "/forecast", "/nifty", "/banknifty", "/signals", "/report", "/news", "/context", "/explain"):
        out = bot.handle_command(cmd)
        assert out and "Maaf kijiye" not in out and "Sorry" not in out, (cmd, out[:150])
    assert "Yeh command samajh nahi aaya" in bot.handle_command("/nope")

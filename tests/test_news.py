from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from config.settings import IST
from news import context, feeds, impact
from tests.test_telegram import INTRA, T, make_bot

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Sensex crashes 1,200 points as war fears hit markets - Reuters</title><link>http://x/1</link>
<pubDate>Fri, 30 Oct 2026 05:30:00 GMT</pubDate></item>
<item><title>Banks &amp; metals lift Nifty - Mint</title><link>http://x/2</link><pubDate>Fri, 30 Oct 2026 05:00:00 GMT</pubDate></item>
<item><title>No date here</title><link>http://x/3</link></item>
<item><title></title><link>http://x/4</link><pubDate>Fri, 30 Oct 2026 05:00:00 GMT</pubDate></item>
</channel></rss>"""

NOW_UTC = datetime(2026, 10, 30, 6, 0, tzinfo=timezone.utc)


# ---------- feeds ----------
def test_parse_rss_handles_google_source_entities_and_bad_items():
    items = feeds.parse_rss(RSS, "Google News")
    assert [h.title for h in items] == ["Sensex crashes 1,200 points as war fears hit markets", "Banks & metals lift Nifty"]
    assert items[0].source == "Reuters" and items[0].published.tzinfo is not None


def test_fetch_all_merges_duplicates_drops_old_and_survives_a_dead_feed():
    class R:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass

    class S:
        n = 0
        def get(self, url, **k):
            S.n += 1
            if S.n == 1:
                raise requests.ConnectionError("down")
            return R(RSS.replace(" - Reuters", "").replace(" - Mint", ""))   # same stories from every feed -> duplicates
    out, errs = feeds.fetch_all(S(), now=NOW_UTC, max_age_hours=3, extra=[])
    assert len(errs) == 1 and len(out) == 2                      # deduped across the other feeds
    assert out[0].published >= out[1].published
    old, _ = feeds.fetch_all(S(), now=NOW_UTC + timedelta(hours=10), max_age_hours=3, extra=[])
    assert old == []


# ---------- impact ----------
@pytest.mark.parametrize("title,sev,direction", [
    ("Sensex crashes 1,200 points as war fears hit markets", "HIGH", "NEGATIVE"),
    ("Nifty rallies to record high on rate cut hopes", "HIGH", "POSITIVE"),
    ("RBI policy today: repo rate decision awaited", "HIGH", "UNCLEAR"),
    ("Sensex, Nifty rise as IT and pharma lift markets", "MEDIUM", "POSITIVE"),
    ("Intel stock plunges 30% as weak PC demand bites", "LOW", "NEGATIVE"),
    ("RIL profit soars 41% to Rs 25,548 cr", "LOW", "POSITIVE"),
    ("Wall Street tumbles as Fed signals more rate hikes", "HIGH", "NEGATIVE"),
    ("Sensex surges 905 points, Nifty tops 22,900 on banking, FMCG", "MEDIUM", "POSITIVE"),   # routine rally report: never an alert
    ("Can Sensex, Nifty extend rally on Friday?", "MEDIUM", "POSITIVE"),
    ("SEBI to make direct plans the default on AMC platforms", "LOW", "UNCLEAR"),             # 'default' in a different sense
    ("RBI steps up scrutiny of banks' cyber defences amid rising AI-led attacks", "LOW", "UNCLEAR"),
    ("Sovereign default fears rattle global markets", "HIGH", "NEGATIVE"),
])
def test_headline_classification(title, sev, direction):
    c = impact.classify(title)
    assert (c["severity"], c["direction"]) == (sev, direction)


def test_price_reaction_confirms_only_when_price_agrees():
    intra = INTRA["NIFTY"].loc[:"2026-01-29 11:00"].copy()
    down = intra.copy()
    last_day = down.index.date == down.index[-1].date()
    n = int(last_day.sum())
    down.loc[last_day, ["open", "high", "low", "close"]] = [[22000 - 20 * k] * 4 for k in range(n)]
    down.loc[down.index[-n], "open"] = 22000
    r = impact.price_reaction(down, "NEGATIVE")
    assert r["known"] and r["confirming"] and r["from_open_pct"] < 0
    assert not impact.price_reaction(down, "POSITIVE")["confirming"]
    assert not impact.price_reaction(down, "UNCLEAR")["confirming"]
    assert impact.price_reaction(intra.iloc[:3], "NEGATIVE")["known"] is False


def test_context_reading_is_factual():
    rows = [{"name": "S&P 500", "last": 1, "prev": 1, "pct": -1.6}, {"name": "Nasdaq", "last": 1, "prev": 1, "pct": -2.0},
            {"name": "India VIX", "last": 18.2, "prev": 16, "pct": 8.0}, {"name": "Brent crude", "last": 1, "prev": 1, "pct": 3.1}]
    notes = " | ".join(context.read_context(rows))
    assert "clearly lower" in notes and "fear rising" in notes and "Brent" in notes
    assert context.read_context([]) == []


def test_context_fetch_tolerates_partial_failure():
    class R:
        def __init__(self, ok): self.ok = ok
        def raise_for_status(self): pass
        def json(self): return {"chart": {"result": [{"indicators": {"quote": [{"close": [10.0, 11.0]}]}}]}}

    class S:
        n = 0
        def get(self, url, **k):
            S.n += 1
            if S.n == 2:
                raise requests.ConnectionError("x")
            return R(True)
    rows, errs = context.fetch_context(S())
    assert len(rows) == len(context.SYMBOLS) - 1 and len(errs) == 1 and rows[0]["pct"] == pytest.approx(10.0)


# ---------- bot integration ----------
def H(title, mins_ago, now_utc, i=0):
    return feeds.Headline(feeds._norm(title)[:12] + str(i), title, "Reuters", f"http://x/{i}", now_utc - timedelta(minutes=mins_ago))


def bot_with_news(tmp_path, headlines):
    bot, clock = make_bot(tmp_path, now="2026-01-29 11:02")
    bot.news_fetcher = lambda now=None, max_age_hours=12: (list(headlines), [])
    return bot, clock


def test_high_severity_news_sends_alert_with_price_context_and_no_trade(tmp_path):
    now_utc = T("2026-01-29 11:02").astimezone(timezone.utc)
    hl = [H("Sensex crashes 1,200 points as war fears hit markets", 15, now_utc, 1),
          H("Sensex, Nifty rise as IT lifts markets", 20, now_utc, 2)]        # MEDIUM: must not alert
    bot, clock = bot_with_news(tmp_path, hl)
    bot.news_scan(clock["now"])
    assert len(bot.client.sent) == 1
    m = bot.client.sent[0]
    for needle in ("NEWS ALERT", "crashes", "Reuters", "http://x/1", "Price reaction so far", "NIFTY", "BANKNIFTY",
                   "nothing is bought or sold", "NOT validated", "PAPER MODE"):
        assert needle in m, needle
    assert "BUY ALERT" not in m and "SELL ALERT" not in m and "rise as IT" not in m
    bot.last_news_alert = None
    bot.news_scan(clock["now"])
    assert len(bot.client.sent) == 1                                             # same headline never repeats


def test_stale_headlines_are_ignored_and_gap_rule_defers_not_drops(tmp_path):
    now_utc = T("2026-01-29 11:02").astimezone(timezone.utc)
    hl = [H("Sensex crashes as war fears hit markets", 15, now_utc, 1)]
    bot, clock = bot_with_news(tmp_path, hl)
    bot.news_scan(clock["now"])
    hl.append(H("Sensex, Nifty plunge as oil spike fuels recession fears", 3, now_utc, 2))
    bot.news_scan(clock["now"] + timedelta(minutes=5))
    assert len(bot.client.sent) == 1                                             # 5 min < 20 min gap: deferred
    bot.news_scan(clock["now"] + timedelta(minutes=21))
    assert len(bot.client.sent) == 2 and "Sensex, Nifty plunge" in bot.client.sent[1]   # sent later, not lost


def test_news_jobs_scheduled_only_on_trading_days_and_commands_work(tmp_path):
    bot, _ = make_bot(tmp_path)
    assert any(j.startswith("news:") for j in bot.due_jobs(T("2026-01-29 10:03")))
    assert not any(j.startswith("news:") for j in bot.due_jobs(T("2026-01-29 15:50")))
    assert bot.due_jobs(T("2026-01-31 10:03")) == []                              # Saturday
    assert "context" in bot.due_jobs(T("2026-01-29 09:06"))
    now_utc = T("2026-01-29 11:02").astimezone(timezone.utc)
    bot.news_fetcher = lambda now=None, max_age_hours=12: ([H("Sensex falls as crude surges, markets slip", 30, now_utc)], [])
    bot.context_fetcher = lambda: ([{"name": "India VIX", "last": 16.0, "prev": 15.0, "pct": 6.7}], [])
    assert "MARKET HEADLINES" in bot.handle_command("/news") and "Sensex falls" in bot.handle_command("/news")
    ctx = bot.handle_command("/context")
    assert "India VIX" in ctx and "does not change the forecast" in ctx and "fear rising" in ctx


def test_news_failure_does_not_crash_the_bot(tmp_path):
    bot, clock = make_bot(tmp_path)
    bot.news_fetcher = lambda **k: (_ for _ in ()).throw(RuntimeError("feeds down"))
    bot.run_job("news:1102")                  # job wrapper swallows and logs the error
    assert bot.client.sent == []


def test_same_story_is_not_alerted_again_for_three_hours(tmp_path):
    now_utc = T("2026-01-29 11:02").astimezone(timezone.utc)
    hl = [H("Fed's Musalem signals more rate hikes, Sensex slips", 10, now_utc, 1)]
    bot, clock = bot_with_news(tmp_path, hl)
    bot.news_scan(clock["now"])
    hl.append(H("Fed's Logan calls for two more rate hikes, Nifty weak", 5, now_utc, 2))     # same cause words
    bot.news_scan(clock["now"] + timedelta(minutes=25))
    assert len(bot.client.sent) == 1
    hl.append(H("US-Iran war escalates, Sensex tumbles", 2, now_utc, 3))                      # a different cause
    bot.news_scan(clock["now"] + timedelta(minutes=50))
    assert len(bot.client.sent) == 2
    bot.news_scan(clock["now"] + timedelta(hours=4))                                           # cooldown over
    hl.append(H("Fed hawks again flag rate hikes, Sensex dips", 1, now_utc + timedelta(hours=4), 4))
    bot.news_scan(clock["now"] + timedelta(hours=4, minutes=1))
    assert len(bot.client.sent) == 3


def test_daily_cap_on_news_alerts(tmp_path):
    from telegram_bot import bot as botmod
    now_utc = T("2026-01-29 11:02").astimezone(timezone.utc)
    hl = []
    bot, clock = bot_with_news(tmp_path, hl)
    causes = ["war", "tariffs", "sanctions", "recession", "lockdown", "missile", "emergency", "invasion"]
    for k, c in enumerate(causes):
        hl.append(H(f"Sensex falls as {c} fears grow", 1, now_utc + timedelta(minutes=30 * k), 10 + k))
        bot.news_scan(clock["now"] + timedelta(minutes=30 * k))
    assert len(bot.client.sent) == botmod.NEWS_MAX_PER_DAY

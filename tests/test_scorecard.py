from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from config.settings import IST
from paper_trading import scorecard as sc
from risk_management.gate import SignalGate
from tests.test_telegram import DAILY, INTRA, T, make_bot


def test_zigzag_finds_turning_points():
    closes = [100, 110, 120, 115, 105, 112, 125]
    assert sc.zigzag(closes, 8) == [0, 2, 4, 6]
    assert sc.zigzag(closes, 30) == []          # no move that large
    assert sc.zigzag([100], 5) == []


def day_df():
    idx = pd.date_range("2026-01-12 09:15", periods=8, freq="15min", tz=IST)
    closes = [100, 108, 120, 112, 100, 104, 118, 130]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 0}, index=idx)


def test_best_swings_rank_by_size_and_credit_only_same_direction_signals():
    d = day_df()
    buy_in_first_leg = {"time": "2026-01-12 09:45:00+05:30", "action": "BUY"}
    sell_in_first_leg = {"time": "2026-01-12 09:45:00+05:30", "action": "SELL"}
    out = sc.best_swings(d, 8, [buy_in_first_leg], top=3)
    assert [g["points"] for g in out] == sorted([g["points"] for g in out], reverse=True)
    assert out[0]["points"] == 30 and out[0]["direction"] == "UP"      # 100 -> 130 is not one leg; biggest leg is 100->130
    first = next(g for g in out if g["start_time"] == "09:15")
    assert first["caught"] is True
    wrong_way = sc.best_swings(d, 8, [sell_in_first_leg])
    assert all(not g["caught"] for g in wrong_way if g["direction"] == "UP")      # a SELL does not "catch" an up-leg


def card(inst="NIFTY", hhmm="2026-01-29 15:55"):
    day = pd.Timestamp(hhmm).date()
    return sc.day_scorecard(inst, INTRA[inst], DAILY[inst], day, [])


def test_scorecard_fields_and_forecast_is_pre_market_only():
    c = card()
    assert c["complete"] and c["bias"] in ("BULLISH", "BEARISH", "NEUTRAL")
    assert len(c["targets"]) == 6 and all("reached" in t for t in c["targets"])
    assert (c["bias_right"] is None) == (c["bias"] == "NEUTRAL")
    # changing today's candles must not change the forecast that is being graded (no look-ahead)
    day = pd.Timestamp("2026-01-29").date()
    tampered = INTRA["NIFTY"].copy()
    m = tampered.index.date == day
    tampered.loc[m, ["open", "high", "low", "close"]] *= 1.5
    tampered.attrs.update(INTRA["NIFTY"].attrs)
    c2 = sc.day_scorecard("NIFTY", tampered, DAILY["NIFTY"], day, [])
    assert c2["bias"] == c["bias"] and [t["level"] for t in c2["targets"]] == [t["level"] for t in c["targets"]]


def test_partial_day_is_flagged_and_never_stored(tmp_path):
    day = pd.Timestamp("2026-01-29").date()
    part = INTRA["NIFTY"].loc[:"2026-01-29 12:00"]
    part.attrs.update(INTRA["NIFTY"].attrs)
    c = sc.day_scorecard("NIFTY", part, DAILY["NIFTY"], day, [])
    assert not c["complete"]
    store = sc.ScoreStore(tmp_path / "s.json")
    assert store.add(c) is False and store.records() == []
    assert store.add(card()) is True
    assert len(sc.ScoreStore(tmp_path / "s.json").records()) == 1        # persisted


def rec(date, right, change, r=None, stated=0.5, reached=True, caught=True):
    return {"date": date, "instrument": "NIFTY", "complete": True, "bias_right": right, "change_pct": change,
            "targets": [{"side": "up", "label": "T1", "stated": stated, "reached": reached}],
            "signals": [{"r": r}] if r is not None else [], "swings": [{"caught": caught}]}


def test_cumulative_math_and_verdict_honesty():
    recs = [rec("d1", True, 0.3, 1.5), rec("d2", False, 0.2, -1.0), rec("d3", None, -0.1), rec("d4", True, -0.4, -1.0, reached=False, caught=False)]
    c = sc.cumulative(recs)
    assert c["days"] == 4 and c["bias"]["scored"] == 3 and c["bias"]["right"] == 2
    assert c["bias"]["rate"] == pytest.approx(2 / 3) and c["bias"]["up_close_rate"] == pytest.approx(0.5)
    assert c["targets"]["up T1"] == {"n": 4, "stated": 0.5, "actual": 0.75}
    assert c["signals"]["closed"] == 3 and c["signals"]["total_r"] == pytest.approx(-0.5) and c["signals"]["wins"] == 1
    assert c["swings"] == {"n": 4, "caught": 3}
    v = " ".join(sc.verdict(c))
    assert "Only 4 day(s)" in v and "far too few" in v
    many = sc.cumulative([rec(f"d{i}", i % 2 == 0, 0.1) for i in range(60)])
    assert "coin flip" in " ".join(sc.verdict(many))
    good = sc.cumulative([rec(f"d{i}", i % 10 != 0, 0.1) for i in range(100)])
    assert "above a coin flip" in " ".join(sc.verdict(good))
    bad = sc.cumulative([rec(f"d{i}", i % 10 == 0, 0.1) for i in range(100)])
    assert "BELOW a coin flip" in " ".join(sc.verdict(bad))


def test_report_text_has_every_requested_section(tmp_path):
    bot, _ = make_bot(tmp_path, now="2026-01-29 15:55")
    text, complete = bot._eod_report(bot.clock())
    assert complete
    for needle in ("END-OF-DAY REPORT", "=== NIFTY ===", "=== BANKNIFTY ===", "Pre-market view", "Upside zones", "Downside zones",
                   "Paper signals", "Biggest moves of the day", "NOT knowable in advance", "HOW ACCURATE HAS THE TOOL BEEN",
                   "Only 1 day(s) recorded", "not advice", "PAPER MODE"):
        assert needle in text, needle
    bot._eod_report(bot.clock())                        # same day again: still one day in the totals
    assert len(sc.ScoreStore(tmp_path / "scorecard.json").records()) == 2          # NIFTY + BANKNIFTY, no duplicates
    assert "END-OF-DAY REPORT" in bot.handle_command("/report")


def test_eod_job_waits_for_last_candle_then_sends_once(tmp_path):
    bot, clock = make_bot(tmp_path, now="2026-01-29 15:52", stale=True)          # data feed ~3h behind
    bot.run_job("eod")
    assert bot.client.sent == [] and "eod" not in bot.due_jobs(clock["now"])      # retry scheduled, not hammering
    clock["now"] = T("2026-01-29 16:45")                                          # window nearly over: send what exists
    bot.run_job("eod")
    assert len(bot.client.sent) == 1 and "Day not finished yet" in bot.client.sent[0]
    assert sc.ScoreStore(tmp_path / "scorecard.json").records() == []             # partial day not recorded
    good, _ = make_bot(tmp_path / "ok", now="2026-01-29 15:55")
    assert "eod" in good.due_jobs(T("2026-01-29 15:55"))
    good.run_job("eod")
    good.run_job("eod")
    assert len(good.client.sent) == 1 or "eod" not in good.due_jobs(T("2026-01-29 15:56"))


def test_open_ranges_jobs_do_not_hammer_when_data_is_missing(tmp_path):
    bot, clock = make_bot(tmp_path, now="2026-01-29 09:36", stale=True)
    bot.run_job("opening")
    assert "opening" not in bot.due_jobs(clock["now"])
    assert "opening" in bot.due_jobs(clock["now"] + timedelta(minutes=3))


def test_total_daily_signal_cap_across_both_indices():
    g = SignalGate()
    for k, inst in enumerate(["NIFTY", "BANKNIFTY", "NIFTY"]):
        g.register({"id": f"s{k}", "instrument": inst, "direction": "BUY", "setup": "ORB", "entry_ref": 100.0 + 1000 * k},
                   T(f"2026-01-12 {10 + 2 * k}:00"))
    why = g.blocks("BANKNIFTY", T("2026-01-12 14:00"))
    assert any("across both indices" in w for w in why)
    assert not SignalGate().blocks("BANKNIFTY", T("2026-01-12 14:00"))

import copy
import json
from datetime import timedelta

import pytest

from option_live import monitor as M
from option_live.monitor import Monitor
from paper_trading.costs import CostModel
from paper_trading.option_ledger import OptionLedger
from risk_management.option_risk import OptionParams, OptionRiskGate
from telegram_bot.option_alerts import OptionNotifier
from tests.engine_harness import at, gate_for, run
from tests.synth import breakout_scenario

EPOCH = 1_800_000_000.0


class FakeFeed:
    def __init__(self):
        self.ticks, self.connected = {}, True

    def set(self, inst, price, age=0.2):
        self.ticks[inst] = {"price": price, "exchange_ts_ms": (EPOCH - age) * 1000, "received_at": EPOCH - age}

    def status(self):
        return {"connected": self.connected, "stale": [], "tick_age_seconds": {}, "errors": 0, "reconnects": 0, "last_error": None}


class FakeRunner:
    def __init__(self, tmp_path, signal):
        self.instruments = ("NIFTY",)
        self.feed = FakeFeed()
        self.params = OptionParams(capital=200000, min_score=60.0, wait_score=50.0)
        self.gate = OptionRiskGate(self.params, tmp_path / "gate.json")
        self.signal, self.calls, self.q = signal, 0, {"ltp": None, "bid": None, "ask": None}
        self.raise_on_eval = False

    def evaluate(self, inst, now):
        self.calls += 1
        if self.raise_on_eval:
            raise RuntimeError("boom")
        return copy.deepcopy(self.signal)

    def quote(self, symbol):
        return dict(self.q)


class Sink:
    def __init__(self, fail=False):
        self.sent, self.fail, self.chat_id = [], fail, "1"

    def send(self, text, chat_id=None, parse_mode=None):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text))


@pytest.fixture(scope="module")
def buy_signal():
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    s = run(breakout_scenario(1), at(10, 41), gate_for(d, min_score=60.0, wait_score=50.0))
    assert s["action"] == "BUY CALL"
    return s


@pytest.fixture
def env(tmp_path, buy_signal, monkeypatch):
    monkeypatch.setattr(M, "market_state", lambda now: "OPEN")
    r = FakeRunner(tmp_path, buy_signal)
    r.feed.set("NIFTY", buy_signal["underlying_price"])
    ledger = OptionLedger(tmp_path / "ledger.json", CostModel())
    sink = Sink()
    mon = Monitor(r, ledger, OptionNotifier(sink, ledger, printer=lambda *_: None), tmp_path / "state.json", now_epoch=lambda: EPOCH)
    return r, ledger, sink, mon


def texts(sink):
    return [t for _, t in sink.sent]


def test_one_evaluation_per_new_minute_and_one_alert(env):
    r, ledger, sink, mon = env
    mon.step(at(10, 41, 5))
    mon.step(at(10, 41, 20))                              # same minute: must not evaluate again
    assert r.calls == 1 and len(texts(sink)) == 1 and "BUY CALL" in texts(sink)[0] and "PAPER SIGNAL" in texts(sink)[0]
    mon.step(at(10, 42, 5))                               # new minute but a position is open: manage it, do not open another
    assert r.calls == 1 and len(ledger.data["signals"]) == 1
    mon.step(at(10, 42, 1))                               # first 2 seconds of a minute are skipped (candle still settling)
    assert r.calls == 1


def test_signal_not_in_first_seconds_of_minute(env):
    r, _, _, mon = env
    mon.step(at(10, 41, 1))
    assert r.calls == 0


def test_full_winning_lifecycle_alerts_ledger_costs_and_gate(env):
    r, ledger, sink, mon = env
    sig = r.signal
    p = sig["premium"]
    mon.step(at(10, 41, 5))
    r.q = {"ltp": p["entry"], "bid": p["entry"] - 0.2, "ask": p["entry"]}
    mon.step(at(10, 41, 30))
    assert any("ENTRY TRIGGERED" in t for t in texts(sink))
    r.q = {"ltp": p["targets"][0]["premium"] + 0.5, "bid": p["targets"][0]["premium"] + 0.3, "ask": p["targets"][0]["premium"] + 0.7}
    mon.step(at(10, 50, 30))
    mon.step(at(10, 50, 40))                              # repeated step: T1 must not be alerted twice
    assert sum("TARGET 1 HIT" in t for t in texts(sink)) == 1
    top = p["targets"][-1]["premium"]
    r.q = {"ltp": top + 0.5, "bid": top + 0.3, "ask": top + 0.7}
    mon.step(at(11, 5, 30))
    rec = ledger.get(sig["id"])
    assert rec["status"] in ("TARGET2_HIT", "TARGET3_HIT") and rec["entered"]
    assert rec["gross"] > 0 and rec["costs"]["total"] > 0 and rec["net"] == pytest.approx(rec["gross"] - rec["costs"]["total"], abs=0.02)
    assert r.gate.realised_pnl() == pytest.approx(rec["net"])
    m = ledger.metrics()
    assert m["trades"] == 1 and m["wins"] == 1 and m["win_rate_pct"] == 100.0 and m["net_pnl"] == rec["net"]
    assert len(texts(sink)) == len(set(texts(sink)))      # no duplicate message text at all


def test_stop_loss_then_no_automatic_reentry(env):
    r, ledger, sink, mon = env
    p = r.signal["premium"]
    mon.step(at(10, 41, 5))
    r.q = {"ltp": p["entry"], "bid": p["entry"] - 0.2, "ask": p["entry"]}
    mon.step(at(10, 41, 30))
    r.q = {"ltp": p["stop"] - 1, "bid": p["stop"] - 1.2, "ask": p["stop"] - 0.8}
    mon.step(at(10, 47, 30))
    rec = ledger.get(r.signal["id"])
    assert rec["status"] == "STOP_LOSS" and rec["net"] < 0 and any("STOP LOSS HIT" in t for t in texts(sink))
    r.signal = {**r.signal, "id": "NIFTY-NEW", "action": "BUY CALL"}
    mon.step(at(10, 55, 5))
    assert mon.latest["NIFTY"]["status"] == "COOLDOWN_AFTER_LOSS" and ledger.get("NIFTY-NEW") is None
    assert ledger.metrics()["losses"] == 1 and ledger.metrics()["profit_factor"] in (0, 0.0, None)


def test_unentered_signal_is_not_counted_as_a_trade(env):
    r, ledger, sink, mon = env
    p = r.signal["premium"]
    mon.step(at(10, 41, 5))
    r.q = {"ltp": p["entry"] * 1.25, "bid": p["entry"] * 1.24, "ask": p["entry"] * 1.26}     # ran away without filling
    mon.step(at(10, 50, 30))
    rec = ledger.get(r.signal["id"])
    assert rec["status"] == "EXPIRED" and not rec["entered"] and rec["net"] is None
    m = ledger.metrics()
    assert m["trades"] == 0 and m["wins"] == 0 and m["not_entered"] == 1 and m["net_pnl"] == 0
    assert r.gate.realised_pnl() == 0


def test_data_lost_and_restored_alert_once(env):
    r, ledger, sink, mon = env
    mon.step(at(10, 41, 5))
    r.feed.set("NIFTY", 24900, age=120)                                     # stale
    mon.step(at(10, 42, 5))
    mon.step(at(10, 42, 10))
    assert sum("MARKET DATA LOST" in t for t in texts(sink)) == 1
    assert mon.latest["NIFTY"] is not None
    r.feed.set("NIFTY", 24900, age=0.1)
    mon.step(at(10, 43, 5))
    mon.step(at(10, 43, 10))
    assert sum("MARKET DATA RESTORED" in t for t in texts(sink)) == 1


def test_disconnect_detected(env):
    r, ledger, sink, mon = env
    mon.step(at(10, 41, 5))
    r.feed.connected = False
    mon.step(at(10, 41, 30))
    assert any("MARKET DATA LOST" in t and "WebSocket is not connected" in t for t in texts(sink))


def test_restart_does_not_resend_alerts_and_keeps_open_signal(env, tmp_path):
    r, ledger, sink, mon = env
    mon.step(at(10, 41, 5))
    n = len(texts(sink))
    ledger2 = OptionLedger(tmp_path / "ledger.json", CostModel())          # simulated restart
    mon2 = Monitor(r, ledger2, OptionNotifier(sink, ledger2, printer=lambda *_: None), tmp_path / "state.json", now_epoch=lambda: EPOCH)
    assert len(ledger2.open_signals()) == 1
    ledger2.add(r.signal)                                                     # same id again: refused
    mon2.notifier.send(f"{r.signal['id']}:SIGNAL", "again", at(10, 42))
    assert len(texts(sink)) == n and len(ledger2.data["signals"]) == 1


def test_telegram_failure_is_retried_and_does_not_stop_loop(env, tmp_path):
    r, ledger, sink, mon = env
    bad = Sink(fail=True)
    mon.notifier.client = bad
    mon.step(at(10, 41, 5))
    assert ledger.get(r.signal["id"]) is not None and not ledger.alert_sent(f"{r.signal['id']}:SIGNAL")
    mon.notifier.client = sink
    ok = mon.notifier.send(f"{r.signal['id']}:SIGNAL", "retry", at(10, 42))
    assert ok and ledger.alert_sent(f"{r.signal['id']}:SIGNAL")


def test_extra_chat_failure_does_not_cause_resend_to_owner(tmp_path):
    ledger = OptionLedger(tmp_path / "l.json")

    class Half(Sink):
        def send(self, text, chat_id=None, parse_mode=None):
            if chat_id == "2":
                raise RuntimeError("blocked")
            super().send(text, chat_id)
    c = Half()
    n = OptionNotifier(c, ledger, extra_chat_ids=("2",), printer=lambda *_: None)
    assert n.send("k", "hello", at(10, 0)) and not n.send("k", "hello", at(10, 1))
    assert len(c.sent) == 1


def test_engine_error_is_reported_not_swallowed(env):
    r, ledger, sink, mon = env
    r.raise_on_eval = True
    mon.step(at(10, 41, 5))
    assert mon.latest["NIFTY"]["status"] == "ERROR" and "boom" in mon.latest["NIFTY"]["reasons"][0] and mon.errors == 1
    r.raise_on_eval = False
    mon.step(at(10, 42, 5))
    assert mon.latest["NIFTY"]["action"] == "BUY CALL"                        # loop recovered


def test_state_file_has_dashboard_data(env, tmp_path):
    r, ledger, sink, mon = env
    mon.step(at(10, 41, 5))
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["mode"].startswith("PAPER") and st["instruments"]["NIFTY"]["action"] == "BUY CALL"
    assert st["open_signals"][0]["contract"] and st["metrics_today"]["signals_total"] == 1 and st["limits"]["max_trades"] == 3
    assert st["instruments"]["NIFTY"]["premium"]["stop"] < st["instruments"]["NIFTY"]["premium"]["entry"]


def test_ledger_metrics_profit_factor_and_drawdown(tmp_path):
    L = OptionLedger(tmp_path / "m.json", CostModel(brokerage_per_order=0, slippage_pct=0, stt_sell_pct=0, exchange_txn_pct=0, sebi_pct=0, stamp_buy_pct=0))
    base = {"instrument": "NIFTY", "action": "BUY CALL", "created_at": "2026-11-06T10:00:00", "option": {"symbol": "X"},
            "premium": {"entry": 100.0, "stop": 90.0, "targets": [{"premium": 115.0}, {"premium": 130.0}], "quantity": 10, "risk_per_unit": 10.0},
            "underlying": {}, "score": 80, "pattern": "p", "regime_name": "r", "why_trade": [], "invalidation": "i"}
    for i, (ex, ) in enumerate([(120.0,), (90.0,), (85.0,), (110.0,)]):
        sid = f"s{i}"
        L.add({**base, "id": sid, "created_at": f"2026-11-06T10:0{i}:00", "sig": {}})
        L.apply(sid, {"status": "ACTIVE", "event": "e", "final": False, "pnl_per_unit": None}, at(10, i))
        L.apply(sid, {"status": "STOP_LOSS" if ex < 100 else "TARGET2_HIT", "event": "x", "final": True, "pnl_per_unit": ex - 100.0}, at(11, i), exit_premium=ex)
    m = L.metrics()
    assert (m["trades"], m["wins"], m["losses"]) == (4, 2, 2)
    assert m["net_pnl"] == pytest.approx(200 - 100 - 150 + 100) and m["profit_factor"] == pytest.approx(300 / 250)
    assert m["max_drawdown"] == pytest.approx(250.0)          # cumulative 200, 100, -50, 50: peak 200 -> trough -50
    assert m["win_rate_pct"] == 50.0


def test_data_loss_keeps_last_analysis_but_marks_it_old(env, tmp_path):
    r, ledger, sink, mon = env
    mon.step(at(10, 41, 5))
    ledger.data["signals"].clear()
    r.feed.set("NIFTY", 24900, age=120)
    r.signal = {"instrument": "NIFTY", "action": "NO TRADE", "status": "DATA_UNAVAILABLE", "reasons": ["DATA UNAVAILABLE: stale"],
                "time": "2026-11-06T10:42:05+05:30", "data": {"state": "STALE", "label": "DATA STALE"}}
    mon.step(at(10, 42, 5))
    d = mon.latest["NIFTY"]
    assert d["action"] == "NO TRADE" and d["stale_analysis"] is True and d["sides"] and "premium" not in d and d["analysis_time"].startswith("2026-11-06T10:41")


# ---------------- Telegram HTML format ----------------
def test_signal_message_is_html_table_balanced_escaped_and_short(buy_signal):
    import re
    from telegram_bot.option_alerts import fmt_signal, table
    msg = fmt_signal(buy_signal)
    assert len(msg) < 3500 and "BUY CALL" in msg and "PAPER SIGNAL" in msg and "WHY THIS TRADE?" in msg and "🟢" in msg
    for tag in ("b", "pre", "code", "i"):
        assert len(re.findall(f"<{tag}>", msg)) == len(re.findall(f"</{tag}>", msg))
    pre = re.search(r"<pre>(.*?)</pre>", msg, re.S).group(1)
    widths = {len(line) for line in pre.split("\n")}
    assert len(widths) == 1                                                       # every table line has the same width (aligned)
    assert "Entry" in pre and "Stop Loss" in pre and "Target 1" in pre and "Target 2" in pre and "OPTION" in pre and "INDEX" in pre
    evil = {**buy_signal, "pattern": "<script>x</script> & co", "why_trade": ["a < b & c > d"], "invalidation": "x<y"}
    out = fmt_signal(evil)
    assert "<script>" not in out and "&lt;script&gt;" in out and "a &lt; b &amp; c &gt; d" in out
    assert table([["a", "1"]], ["h", "v"]).count("\n") == 4


def test_put_signal_is_red_and_event_messages_are_coloured(buy_signal):
    from telegram_bot.option_alerts import fmt_data, fmt_event, fmt_setup
    put = {**buy_signal, "action": "BUY PUT"}
    from telegram_bot.option_alerts import fmt_signal
    assert fmt_signal(put).startswith("🔴")
    rec = {"instrument": "NIFTY", "action": "BUY CALL", "option": buy_signal["option"], "premium": buy_signal["premium"], "id": "X", "entered": True}
    win = fmt_event(rec, {"status": "TARGET1_HIT", "event": "Target 1 reached", "final": False}, 110.0, 24950.0)
    assert "🎯" in win and "TARGET 1 HIT" in win
    loss = fmt_event(rec, {"status": "STOP_LOSS", "event": "Stop loss hit", "final": True}, 80.0, 24880.0, net=-1200.0)
    assert "🛑" in loss and "🟥" in loss and "-1,200" in loss
    assert "🟡" in fmt_setup("NIFTY", "BUY CALL", ["a & b"], 70) and "🔴" in fmt_data("LOST", "NIFTY", "x") and "🟢" in fmt_data("RESTORED", "NIFTY", "x")


def test_client_sends_html_unsplit_and_strips_tags_when_too_long():
    from telegram_bot.client import TelegramClient
    calls = []

    class S:
        def post(self, url, json=None, timeout=None):
            calls.append(json)

            class R:
                status_code = 200
                def json(self_inner):
                    return {"ok": True, "result": {}}
            return R()
    c = TelegramClient("tok", "1", session=S())
    c.send("<b>hi</b>", parse_mode="HTML")
    assert calls[-1]["parse_mode"] == "HTML" and calls[-1]["text"] == "<b>hi</b>"
    c.send("plain")
    assert "parse_mode" not in calls[-1]
    n = c.send("<b>" + "x" * 5000 + "</b>", parse_mode="HTML")
    assert n >= 2 and all("parse_mode" not in x and "<" not in x["text"] for x in calls[-n:])


def test_market_closed_refreshes_analysis_only_every_two_minutes(env, monkeypatch):
    r, ledger, sink, mon = env
    monkeypatch.setattr(M, "market_state", lambda now: "PRE_MARKET")
    clock = {"t": EPOCH}
    mon.now_epoch = lambda: clock["t"]
    r.feed.ticks.clear()
    for i in range(10):                                   # 10 polls over 20 seconds
        clock["t"] = EPOCH + 2 * i
        mon.step(at(9, 10, 2 * i))
    assert r.calls == 1                                   # one evaluation, not ten
    clock["t"] = EPOCH + 130
    mon.step(at(9, 12, 20))
    assert r.calls == 2
    assert mon.latest["NIFTY"] is not None and sink.sent == []                        # nothing is alerted while the market is closed

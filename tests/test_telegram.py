from dataclasses import replace
from datetime import datetime

import pandas as pd
import pytest
import requests

from config.settings import IST, load_settings
from market_data.loader import data_status
from market_data.mock_adapter import MockAdapter
from paper_trading.ledger import Ledger, result_r
from telegram_bot import messages as M
from telegram_bot.bot import AlertLog, Bot
from telegram_bot.client import TelegramClient, TelegramError, split_message

TOKEN = "123456:SECRET_TOKEN_VALUE_abcdefghijklmnopqrstuvwxyz"
M_ = MockAdapter()
INTRA = {i: M_.get_candles(i, "15m", 40) for i in ("NIFTY", "BANKNIFTY")}
DAILY = {i: M_.get_candles(i, "1d", 200) for i in ("NIFTY", "BANKNIFTY")}


class FakeClient:
    chat_id = "42"

    def __init__(self):
        self.sent, self.updates = [], []

    def send(self, text, chat_id=None):
        self.sent.append(text)
        return 1

    def get_updates(self, offset=None, timeout=5):
        u, self.updates = self.updates, []
        return u


def T(s):
    return pd.Timestamp(s, tz=IST).to_pydatetime()


def make_bot(tmp_path, now="2026-01-29 10:02", stale=False):
    clock = {"now": T(now)}

    def loader(inst, mode):
        df = INTRA[inst]
        cut = df[df.index + pd.Timedelta(minutes=15) <= clock["now"] - (pd.Timedelta(hours=3) if stale else pd.Timedelta(0))]
        cut.attrs.update(df.attrs)
        return cut, DAILY[inst][DAILY[inst].index.date < clock["now"].date()]

    s = replace(load_settings(), data_mode="MOCK", telegram_lang="en")
    bot = Bot(FakeClient(), s, mode="MOCK", loader=loader, state_dir=tmp_path, clock=lambda: clock["now"])
    return bot, clock


# ---------------- client ----------------
def test_split_message_respects_limit():
    parts = split_message("\n".join(["x" * 90] * 200), limit=1000)
    assert len(parts) > 1 and all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts).count("x" * 90) == 200


def test_token_never_appears_in_errors():
    class Boom:
        def post(self, url, **kw):
            raise requests.ConnectionError(f"failed for {url}")
    c = TelegramClient(TOKEN, "42", session=Boom(), retries=1)
    with pytest.raises(TelegramError) as e:
        c.send("hi")
    assert TOKEN not in str(e.value) and "SECRET_TOKEN" not in str(e.value)


def test_missing_credentials_rejected():
    with pytest.raises(TelegramError):
        TelegramClient("", "")


def test_api_error_is_reported():
    class R:
        status_code = 401
        def json(self): return {"ok": False, "description": "Unauthorized"}
    class S:
        def post(self, *a, **k): return R()
    with pytest.raises(TelegramError, match="Unauthorized"):
        TelegramClient(TOKEN, "42", session=S()).send("hi")


# ---------------- commands & security ----------------
def test_unauthorised_chat_is_ignored_and_authorised_gets_help(tmp_path):
    bot, _ = make_bot(tmp_path)
    bot.client.updates = [{"update_id": 1, "message": {"chat": {"id": 999}, "text": "/forecast"}},
                          {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/help"}}]
    bot.process_updates()
    assert len(bot.client.sent) == 1 and "/forecast" in bot.client.sent[0] and bot.offset == 3


@pytest.mark.parametrize("cmd", ["/start", "/help", "/status", "/forecast", "/nifty", "/banknifty", "/signals", "/report"])
def test_every_command_answers_with_mode_label(tmp_path, cmd):
    bot, _ = make_bot(tmp_path, now="2026-01-29 11:00")
    out = bot.handle_command(cmd)
    assert out and "Sorry" not in out
    if cmd not in ("/start", "/help"):
        assert "PAPER MODE" in out or "PAPER" in out


def test_forecast_message_has_required_fields(tmp_path):
    bot, clock = make_bot(tmp_path, now="2026-01-29 11:00")
    out = bot.handle_command("/nifty")
    for needle in ("NIFTY", "29 Jan 2026 11:00 IST", "Price:", "Bias:", "Support", "Resistance", "T1", "Cancelled if",
                   "Signal status", "Reason", "not advice", "PAPER MODE - MOCK DATA"):
        assert needle in out, needle


def test_unknown_command_and_failure_do_not_crash(tmp_path):
    bot, _ = make_bot(tmp_path)
    assert "Unknown command" in bot.handle_command("/nope")
    bot.loader = lambda *a: (_ for _ in ()).throw(RuntimeError("feed down"))
    assert "Sorry" in bot.handle_command("/forecast")


# ---------------- schedule & duplicates ----------------
def test_due_jobs_times_and_weekend(tmp_path):
    bot, _ = make_bot(tmp_path)
    assert all(j.startswith("news:") for j in bot.due_jobs(T("2026-01-29 08:59")))   # only the news scan runs before 09:00
    assert bot.due_jobs(T("2026-01-29 08:40")) == []
    assert bot.due_jobs(T("2026-01-29 09:01")) == ["premarket"]
    assert "opening" in bot.due_jobs(T("2026-01-29 09:36"))
    assert "summary" in bot.due_jobs(T("2026-01-29 15:21"))
    assert "scan:1002" in bot.due_jobs(T("2026-01-29 10:03"))
    assert bot.due_jobs(T("2026-01-29 10:30")) == ["scan:1017"] or "scan:1032" in bot.due_jobs(T("2026-01-29 10:33"))
    assert bot.due_jobs(T("2026-01-31 09:01")) == []        # Saturday
    assert bot.due_jobs(T("2026-01-29 16:30")) == ["eod"]          # end-of-day report may still be waiting for the last candle
    assert bot.due_jobs(T("2026-01-29 17:10")) == []


def test_job_runs_once_and_survives_restart(tmp_path):
    bot, clock = make_bot(tmp_path, now="2026-01-29 09:01")
    bot.run_job("premarket")
    assert len(bot.client.sent) == 2 and all("PRE-MARKET FORECAST" in m for m in bot.client.sent)
    assert "premarket" not in bot.due_jobs(clock["now"])
    bot2, _ = make_bot(tmp_path, now="2026-01-29 09:05")     # new process, same state folder
    assert "premarket" not in bot2.due_jobs(T("2026-01-29 09:05"))


def test_alert_dedupe_by_key_and_new_day(tmp_path):
    bot, clock = make_bot(tmp_path)
    assert bot._send("x", key="k") is True and bot._send("x", key="k") is False
    assert len(bot.client.sent) == 1
    clock["now"] = T("2026-01-30 10:00")
    assert bot._send("x", key="k") is True


def test_alertlog_persists(tmp_path):
    a = AlertLog(tmp_path / "a.json")
    a.mark("2026-01-29", "z")
    assert AlertLog(tmp_path / "a.json").seen("2026-01-29", "z")
    assert not AlertLog(tmp_path / "a.json").seen("2026-01-30", "z")


def test_stale_data_pauses_alerts_and_says_so(tmp_path):
    bot, _ = make_bot(tmp_path, now="2026-01-29 11:02", stale=True)
    bot.scan(bot.clock())
    assert bot.client.sent and all("STALE" in m or "unavailable" in m for m in bot.client.sent)
    assert not any("ALERT" in m for m in bot.client.sent)
    n = len(bot.client.sent)
    bot.scan(bot.clock())
    assert len(bot.client.sent) == n   # same hour: no repeat


def test_data_status_states():
    intra = INTRA["NIFTY"]
    last_end = intra.index[-1] + pd.Timedelta(minutes=15)
    assert data_status(intra, last_end + pd.Timedelta(minutes=5))["market"] in ("OPEN", "CLOSED", "PRE_MARKET")
    ds = data_status(intra.loc[:"2026-01-29 11:00"], T("2026-01-29 12:30"))
    assert ds["status"] == "STALE"
    assert data_status(intra, T("2026-01-31 12:00"))["status"] == "CLOSED"


# ---------------- ledger ----------------
def test_ledger_flow(tmp_path):
    sig = {"id": "s", "instrument": "NIFTY", "time": "2026-01-29 10:00:00+05:30",
           "targets": [{"rr": 1.0}, {"rr": 2.0}]}
    L = Ledger(tmp_path / "l.json")
    L.add(sig); L.add(sig)
    assert len(L.items) == 1
    assert L.update("s", "TARGET1_HIT")["status"] == "TARGET1_HIT" and L.update("s", "TARGET1_HIT") is None
    done = L.update("s", "STOPPED")
    assert done["r"] == pytest.approx(0.5)            # half banked at T1, rest at breakeven
    assert Ledger(tmp_path / "l.json").items[0]["status"] == "STOPPED"
    assert result_r(sig, "STOPPED", False) == -1 and result_r(sig, "TARGET2_HIT", True) == pytest.approx(1.5)
    assert not L.active()


def test_explain_command_both_languages(tmp_path):
    bot, _ = make_bot(tmp_path, now="2026-01-29 11:00")
    en, hi = bot.handle_command("/explain"), bot.handle_command("/explain hi")
    assert "Why" in en and "PAPER MODE" in en and "kyun" in hi and "Sorry" not in en + hi


def test_real_client_get_updates_and_get_me_use_correct_request_arguments():
    """Regression: get_updates once crashed with 'multiple values for keyword argument timeout'."""
    calls = []

    class R:
        status_code = 200
        def __init__(self, result): self.result = result
        def json(self): return {"ok": True, "result": self.result}

    class S:
        def get(self, url, params=None, timeout=None):
            calls.append((url.rsplit("/", 1)[-1], params, timeout))
            return R([{"update_id": 7}] if url.endswith("getUpdates") else {"username": "b", "first_name": "B"})
        def post(self, url, json=None, timeout=None):
            calls.append((url.rsplit("/", 1)[-1], json, timeout))
            return R({})
    c = TelegramClient(TOKEN, "42", session=S())
    assert c.get_updates(offset=5, timeout=20) == [{"update_id": 7}]
    assert calls[0][0] == "getUpdates" and calls[0][1]["timeout"] == 20 and calls[0][1]["offset"] == 5 and calls[0][2] == 30
    assert c.get_me() == {"username": "b", "name": "B"}
    c.send("hello")
    assert calls[-1][0] == "sendMessage" and calls[-1][1]["chat_id"] == "42" and calls[-1][2] == 15

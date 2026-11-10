import json
from dataclasses import replace
from datetime import datetime

import pandas as pd
import pytest

from config.settings import IST, load_settings
from dashboard.app import create_app
from dashboard.state import DashboardState
from market_data.mock_adapter import MockAdapter
from paper_trading.ledger import Ledger
from paper_trading.report import report_text

M = MockAdapter()
INTRA = {i: M.get_candles(i, "15m", 40) for i in ("NIFTY", "BANKNIFTY")}
DAILY = {i: M.get_candles(i, "1d", 200) for i in ("NIFTY", "BANKNIFTY")}
NOW = pd.Timestamp("2026-01-29 11:02", tz=IST).to_pydatetime()


def loader(inst, mode):
    df = INTRA[inst]
    cut = df[df.index + pd.Timedelta(minutes=15) <= NOW]
    cut.attrs.update(df.attrs)
    return cut, DAILY[inst][DAILY[inst].index.date < NOW.date()]


class FakeTG:
    def get_me(self):
        return {"username": "my_bot", "name": "x"}


def make(tmp_path, **kw):
    s = replace(load_settings(), data_mode="MOCK", telegram_bot_token="123456:ABCDEF_secret_value_zzzzzzzzzzzzzzzz",
                telegram_chat_id="42", groww_api_key="", groww_api_secret="")
    return DashboardState(s, "MOCK", loader=loader, clock=lambda: NOW, state_dir=tmp_path, ttl=0,
                          telegram_client=FakeTG(), **kw), s


def test_state_has_everything_requested(tmp_path):
    st, _ = make(tmp_path)
    d = st.build()
    assert [i["name"] for i in d["instruments"]] == ["NIFTY", "BANKNIFTY"] and not d["errors"]
    i = d["instruments"][0]
    for k in ("price", "bias", "scenarios", "reversal_zones", "signal_now", "freshness", "data_source", "chart",
              "support", "resistance", "opening_range", "volatility"):
        assert k in i, k
    assert len(i["scenarios"]["bullish"]["targets"]) == 3 and len(i["scenarios"]["bearish"]["targets"]) == 3
    assert d["mode_label"] == "PAPER MODE (MOCK DATA)"
    assert d["telegram"]["state"] == "CONNECTED" and d["groww"]["state"] == "NOT CONNECTED"
    assert "DISABLED" in d["orders"] and "NO trading edge" in d["backtest_warning"]
    assert i["data_source_key"] == "MOCK" and "MOCK" in i["data_source"]


def test_no_secrets_in_state(tmp_path):
    st, s = make(tmp_path)
    blob = json.dumps(st.build())
    assert "ABCDEF_secret" not in blob and s.telegram_bot_token not in blob


def test_unconfigured_telegram_and_data_error_are_reported(tmp_path):
    st, s = make(tmp_path)
    st.settings = replace(s, telegram_bot_token="", telegram_chat_id="")
    st.loader = lambda *a: (_ for _ in ()).throw(RuntimeError("feed down"))
    d = st.build()
    assert d["telegram"]["state"] == "NOT CONFIGURED" and d["errors"] and d["instruments"] == []


def test_flask_routes_and_localhost_only(tmp_path):
    st, s = make(tmp_path)
    app = create_app(s, "MOCK", loader=loader, clock=lambda: NOW, state_dir=tmp_path, ttl=0, telegram_client=FakeTG())
    c = app.test_client()
    page = c.get("/")
    assert page.status_code == 200 and b"ORDERS DISABLED" in page.data and b"FORECAST" in page.data
    low = page.data.lower()
    assert b"<button" not in low and b"<form" not in low and b"<input" not in low and b"place_order" not in low   # nothing to click
    api = c.get("/api/state")
    assert api.status_code == 200 and api.headers["Cache-Control"] == "no-store"
    assert c.get("/healthz").json == {"ok": True}
    import inspect, dashboard.app as mod
    assert 'host="127.0.0.1"' in inspect.getsource(mod.run_dashboard)


def test_paper_history_and_report(tmp_path):
    sig = {"id": "a", "instrument": "NIFTY", "action": "BUY", "setup": "ORB", "entry_ref": 100.0, "stop": 95.0,
           "time": "2026-01-29 10:00:00+05:30", "targets": [{"rr": 1.0}, {"rr": 2.0}]}
    L = Ledger(tmp_path / "paper_ledger.json")
    L.add(sig)
    L.update("a", "STOPPED")
    st, _ = make(tmp_path)
    p = st.build()["paper"]
    assert p["summary"]["trades"] == 1 and p["history"][0]["status"] == "STOPPED" and len(p["today"]) == 1
    txt = report_text(tmp_path / "paper_ledger.json")
    assert "1 closed" in txt and "far too few" in txt
    assert "no signals recorded" in report_text(tmp_path / "none.json")


def test_header_label_follows_actual_data_mode_not_env(tmp_path):
    st, s = make(tmp_path)
    st.mode = "HISTORICAL"
    assert st.build()["mode_label"] == "PAPER MODE (HISTORICAL DATA)"
    st.mode = "LIVE"
    assert "LIVE" in st.build()["mode_label"] and "PAPER TRADING ONLY" in st.build()["mode_label"]


# ---------------- option terminal (/options, /api/options) ----------------
def _opt_app(tmp_path, state=None):
    p = tmp_path / "option_state.json"
    if state is not None:
        p.write_text(json.dumps(state))
    return create_app(replace(load_settings(), data_mode="MOCK"), "MOCK", option_state_path=p).test_client()


def test_options_api_reports_monitor_not_running(tmp_path):
    r = _opt_app(tmp_path).get("/api/options").get_json()
    assert r["available"] is False and "option-monitor" in r["message"]


def test_options_api_marks_old_state_as_stale_and_fresh_as_ok(tmp_path):
    import time
    old = _opt_app(tmp_path, {"updated_epoch": time.time() - 300, "instruments": {}}).get("/api/options").get_json()
    assert old["available"] and old["monitor_stale"] is True and old["age_seconds"] > 200
    fresh = _opt_app(tmp_path, {"updated_epoch": time.time(), "instruments": {}}).get("/api/options").get_json()
    assert fresh["monitor_stale"] is False


def test_options_page_is_read_only_and_self_contained(tmp_path):
    html = _opt_app(tmp_path).get("/options").get_data(as_text=True)
    assert "PAPER SIGNALS" in html and "WHY THIS TRADE" in html and "WHY NO TRADE" in html
    assert "<form" not in html.lower() and "<button" not in html.lower()          # no order controls
    assert "http://" not in html and "https://" not in html                        # no external resources

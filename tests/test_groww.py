import re
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from config.settings import IST, ROOT
from groww_integration import instruments as ins
from groww_integration.auth import GrowwAuthError, GrowwSession
from groww_integration.historical import GrowwHistoricalAdapter, parse_candles
from groww_integration.live_feed import LiveIndexFeed, parse_index_payload
from groww_integration.safe_client import SafeGroww
from market_data.base import MarketDataError
from market_data.groww_adapter import GrowwAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "groww_instruments_sample.csv"


class AuthExc(Exception):
    code = "401"


EXC = SimpleNamespace(GrowwAPIAuthenticationException=AuthExc)


class FakeAPI:
    EXCHANGE_NSE, SEGMENT_CASH, SEGMENT_FNO = "NSE", "CASH", "FNO"
    CANDLE_INTERVAL_MIN_5, CANDLE_INTERVAL_MIN_15 = "5minute", "15minute"
    CANDLE_INTERVAL_HOUR_1, CANDLE_INTERVAL_DAY = "1hour", "1day"
    INSTRUMENT_CSV_URL = "https://example.invalid/instruments.csv"
    logins, reject_next, hist_calls = [], 0, []

    def __init__(self, token):
        self.token = token

    @classmethod
    def get_access_token(cls, api_key, totp=None, secret=None):
        cls.logins.append((api_key, bool(totp), bool(secret)))
        return {"token": f"tok{len(cls.logins)}"}

    def get_all_instruments(self):
        return pd.read_csv(FIXTURE, dtype=str)

    def get_historical_candles(self, exchange, segment, groww_symbol, start_time, end_time, candle_interval, timeout=None):
        cls = type(self)
        if cls.reject_next > 0:
            cls.reject_next -= 1
            raise AuthExc("expired")
        cls.hist_calls.append((segment, groww_symbol, start_time, end_time, candle_interval))
        t0 = int(pd.Timestamp('2026-01-05 09:15:00', tz=IST).timestamp())   # fixed Monday, independent of today's date
        vol = 500 if segment == "FNO" else None
        return {"candles": [[t0, 100, 101, 99, 100.5, vol, None], [t0 + 300, 100.5, 102, 100, 101, vol, None]]}

    def place_order(self, **kw):  # must never be reachable
        raise AssertionError("order placed!")


@pytest.fixture(autouse=True)
def reset():
    FakeAPI.logins, FakeAPI.reject_next, FakeAPI.hist_calls = [], 0, []


def session(**kw):
    return GrowwSession("KEY", secret="SEC", growwapi=FakeAPI, exceptions=EXC, **kw)


# ---------- orders are impossible ----------
def test_safe_wrapper_blocks_every_order_method():
    g = SafeGroww(FakeAPI("t"))
    for name in ("place_order", "modify_order", "cancel_order", "create_smart_order", "anything_else"):
        with pytest.raises(PermissionError):
            getattr(g, name)
    assert g.EXCHANGE_NSE == "NSE" and g.CANDLE_INTERVAL_MIN_5 == "5minute"
    with pytest.raises(PermissionError):
        g.foo = 1


def test_session_client_is_always_wrapped():
    s = session()
    s.connect()
    with pytest.raises(PermissionError):
        s.client.place_order(trading_symbol="X")


def test_no_order_calls_exist_in_project_source():
    bad = re.compile(r"\.(place_order|modify_order|cancel_order|create_smart_order|place_smart_order)\s*\(")
    offenders = []
    for p in ROOT.rglob("*.py"):
        if ".venv" in p.parts or "tests" in p.parts or "reference" in p.parts:
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if bad.search(line) and not line.strip().startswith("#"):
                offenders.append(f"{p.relative_to(ROOT)}:{i}")
    assert offenders == []


# ---------- auth ----------
def test_missing_credentials_explained():
    with pytest.raises(GrowwAuthError, match="GROWW_API_KEY"):
        GrowwSession("", "x")
    with pytest.raises(GrowwAuthError, match="SECRET"):
        GrowwSession("key", "", "")


def test_login_uses_documented_arguments():
    session().connect()
    assert FakeAPI.logins == [("KEY", False, True)]
    GrowwSession("KEY", totp_secret="T", growwapi=FakeAPI, exceptions=EXC, totp_factory=lambda s: "123456").connect()
    assert FakeAPI.logins[-1] == ("KEY", True, False)


def test_expired_token_triggers_one_relogin_then_works():
    s = session()
    s.connect()
    FakeAPI.reject_next = 1
    out = s.call("get_historical_candles", exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY",
                 start_time="2026-01-05 09:15:00", end_time="2026-01-05 15:30:00", candle_interval="5minute")
    assert out["candles"] and len(FakeAPI.logins) == 2


def test_persistent_rejection_gives_clear_instruction():
    s = session()
    FakeAPI.reject_next = 5
    with pytest.raises(GrowwAuthError, match="API Keys page"):
        s.call("get_historical_candles", exchange="NSE", segment="CASH", groww_symbol="x", start_time="a",
               end_time="b", candle_interval="5minute")


def test_login_attempts_are_capped():
    s = session()
    for _ in range(5):
        s.connect()
    with pytest.raises(GrowwAuthError, match="login attempts"):
        s.connect()


# ---------- instruments ----------
def test_index_validation_on_real_rows():
    df = pd.read_csv(FIXTURE, dtype=str)
    v = ins.validate_indices(df)
    assert v["NIFTY"]["groww_symbol"] == "NSE-NIFTY" and v["NIFTY"]["exchange_token"] == "NIFTY"
    assert v["BANKNIFTY"]["instrument_type"] == "IDX" and v["BANKNIFTY"]["segment"] == "CASH"


def test_missing_or_ambiguous_index_is_refused_not_guessed():
    df = pd.read_csv(FIXTURE, dtype=str)
    with pytest.raises(ins.InstrumentError, match="NSE-BANKNIFTY"):
        ins.validate_indices(df[df["groww_symbol"] != "NSE-BANKNIFTY"])
    with pytest.raises(ins.InstrumentError, match="found 2"):
        ins.validate_indices(pd.concat([df, df[df["groww_symbol"] == "NSE-NIFTY"]]))
    with pytest.raises(ins.InstrumentError, match="columns"):
        ins.validate_indices(df.drop(columns=["segment"]))


def test_nearest_future_skips_expired():
    df = pd.read_csv(FIXTURE, dtype=str)
    assert ins.nearest_future(df, "NIFTY", date(2026, 10, 30))["trading_symbol"] == "NIFTY26NOVFUT"
    assert ins.nearest_future(df, "NIFTY", date(2026, 11, 24))["trading_symbol"] == "NIFTY26DECFUT"
    assert ins.nearest_future(df, "NIFTY", date(2027, 6, 1)) is None
    assert ins.nearest_future(df, "NOPE", date(2026, 10, 30)) is None


# ---------- historical ----------
def test_parse_candles_handles_all_timestamp_forms():
    p = {"candles": [[1785901500, 1, 2, 0.5, 1.5, 10, None], [1785901800000, 1, 2, 0.5, 1.5, None, None],
                     ["2026-08-05T09:15:00", 1, 2, 0.5, 1.5, 5, 7], ["2026-08-05 09:20:00", 1, 2, 0.5, 1.5, 5, 7]]}
    df = parse_candles(p)
    assert str(df.index.tz) == "Asia/Kolkata" and (df["volume"] >= 0).all() and not df.isna().any().any()
    assert df.index[2].hour == 9 and df.index[2].minute == 15
    with pytest.raises(MarketDataError):
        parse_candles({"oops": 1})


def test_requests_are_chunked_to_documented_limits():
    a = GrowwHistoricalAdapter(session(), sleep=lambda s: None, today=date(2026, 10, 30))
    a.get_candles("NIFTY", "5m", 70, with_futures_volume=False)
    cash = [c for c in FakeAPI.hist_calls if c[0] == "CASH"]
    assert len(cash) == 3 and all(c[1] == "NSE-NIFTY" and c[4] == "5minute" for c in cash)
    for _, _, s, e, _ in cash:
        assert (pd.Timestamp(e) - pd.Timestamp(s)).days <= 30


def test_adapter_output_is_labelled_and_volume_comes_from_future():
    a = GrowwHistoricalAdapter(session(), sleep=lambda s: None, today=date(2026, 10, 30))
    df = a.get_candles("BANKNIFTY", "15m", 5)
    assert df.attrs["source"] == "LIVE" and "BANKNIFTY26NOVFUT" in df.attrs["volume_source"]
    fut_calls = [c for c in FakeAPI.hist_calls if c[0] == "FNO"]
    assert fut_calls and fut_calls[0][1] == "NSE-BANKNIFTY-23Nov26-FUT"
    assert str(df.index.tz) == "Asia/Kolkata"


def test_volume_failure_does_not_break_candles():
    a = GrowwHistoricalAdapter(session(), sleep=lambda s: None, today=date(2027, 6, 1))   # no unexpired future
    df = a.get_candles("NIFTY", "15m", 5)
    assert len(df) > 0 and (df["volume"] == 0).all() and "none" in df.attrs["volume_source"]


def test_groww_adapter_without_keys_gives_market_data_error(monkeypatch):
    for k in ("GROWW_API_KEY", "GROWW_API_SECRET", "GROWW_TOTP_SECRET"):
        monkeypatch.setenv(k, "")
    with pytest.raises(MarketDataError, match="GROWW_API_KEY"):
        GrowwAdapter().get_candles("NIFTY", "5m", 3)


# ---------- live feed ----------
def test_payload_parser_variants_and_unknown_shape():
    p1 = {"index": {"NSE": {"CASH": {"NIFTY": {"tsInMillis": 1, "value": 23000.5}, "BANKNIFTY": {"value": 54000}}}}}
    assert parse_index_payload(p1) == {"NIFTY": (23000.5, 1), "BANKNIFTY": (54000.0, None)}
    assert parse_index_payload({"x": {"NIFTY": {"foo": "bar"}}}) == {}
    assert parse_index_payload("garbage") == {}


class FakeFeed:
    instances, fail_first, payload = [], True, {"NSE": {"CASH": {"NIFTY": {"tsInMillis": 5, "ltp": 23100.0}}}}

    def __init__(self, groww_api):
        self.n = len(FakeFeed.instances)
        FakeFeed.instances.append(self)
        self.cb = None

    def subscribe_index_value(self, instrument_list, on_data_received=None):
        assert instrument_list[0] == {"exchange": "NSE", "segment": "CASH", "exchange_token": "NIFTY"}
        self.cb = on_data_received

    def get_index_value(self):
        return FakeFeed.payload

    def consume(self):
        if FakeFeed.fail_first and self.n == 0:
            raise ConnectionError("socket dropped")
        self.cb({"feed_type": "index"})
        self.stop_flag = True
        raise ConnectionError("done")   # leave the loop quickly after one tick

    def unsubscribe_index_value(self, instrument_list):
        pass


def test_feed_reconnects_after_drop_and_serves_ticks():
    FakeFeed.instances = []
    sleeps = []
    f = LiveIndexFeed(object(), feed_cls=FakeFeed, sleep=lambda s: (sleeps.append(s), f._stop.set() if len(sleeps) >= 2 else None))
    f._run()
    assert len(FakeFeed.instances) >= 2 and sleeps[0] == 1.0 and sleeps[1] == 2.0   # exponential backoff
    price, age = f.latest("NIFTY")
    assert price == 23100.0 and f.errors >= 1 and f.reconnects >= 1


def test_stale_ticks_are_not_served():
    now = {"t": 1000.0}
    f = LiveIndexFeed(object(), feed_cls=FakeFeed, clock=lambda: now["t"], stale_after=30)
    f.ticks["NIFTY"] = {"price": 23000.0, "exchange_ts_ms": 1, "received_at": 1000.0}
    assert f.latest("NIFTY") == (23000.0, 0.0)
    now["t"] = 1100.0
    assert f.latest("NIFTY") is None
    st = f.status()
    assert "NIFTY" in st["stale"] and "BANKNIFTY" in st["stale"]
    f.market_open = lambda: False
    assert f.latest("NIFTY") is not None and f.status()["stale"] == []   # outside market hours silence is normal

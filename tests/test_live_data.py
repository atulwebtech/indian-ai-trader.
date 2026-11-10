from datetime import datetime, timedelta

import pandas as pd
import pytest

from config.settings import IST
from market_data.candle_builder import CandleBuilder, bucket_start
from market_data.live_guard import live_state


def T(h, m, s=0):
    return datetime(2026, 11, 6, h, m, s, tzinfo=IST)


def test_bucket_alignment_to_0915():
    assert bucket_start(T(9, 16, 30), 1) == T(9, 16)
    assert bucket_start(T(9, 29, 59), 15) == T(9, 15)
    assert bucket_start(T(9, 30), 15) == T(9, 30)
    assert bucket_start(T(9, 44), 30) == T(9, 15)
    assert bucket_start(T(9, 46), 30) == T(9, 45)
    assert bucket_start(T(9, 17), 3) == T(9, 15) and bucket_start(T(9, 18), 3) == T(9, 18)


def test_builder_ohlc_close_and_all_timeframes():
    b = CandleBuilder()
    assert b.on_tick("NIFTY", 100, T(9, 15, 1)) == []
    b.on_tick("NIFTY", 105, T(9, 15, 20))
    b.on_tick("NIFTY", 98, T(9, 15, 40))
    ev = b.on_tick("NIFTY", 101, T(9, 16, 2))
    assert [tf for tf, _ in ev] == ["1m"]
    c = ev[0][1]
    assert (c["open"], c["high"], c["low"], c["close"]) == (100, 105, 98, 98)
    ev = b.on_tick("NIFTY", 102, T(9, 18, 1))           # closes 1m (09:16) and the first 3m bar
    tfs = sorted(tf for tf, _ in ev)
    assert tfs == ["1m", "3m"]


def test_no_invented_candles_and_gap_counted():
    b = CandleBuilder(timeframes=("1m",))
    b.on_tick("NIFTY", 100, T(9, 15, 5))
    b.on_tick("NIFTY", 101, T(9, 19, 5))               # 09:16-09:18 had no ticks
    assert len(b.closed_df("NIFTY", "1m")) == 1
    assert b.missing("NIFTY", "1m") == 3


def test_late_duplicate_and_out_of_session_ticks_ignored():
    b = CandleBuilder(timeframes=("1m",))
    b.on_tick("NIFTY", 100, T(9, 20, 10))
    b.on_tick("NIFTY", 99, T(9, 20, 5))                # older than the last tick
    b.on_tick("NIFTY", 100, T(9, 20, 10))              # exact duplicate
    b.on_tick("NIFTY", 100, T(8, 59))                  # before open
    b.on_tick("NIFTY", 100, T(15, 31))                 # after close
    assert (b.late_ticks, b.duplicates, b.out_of_session) == (1, 1, 2)
    assert b.forming[("NIFTY", "1m")]["ticks"] == 1


def test_naive_timestamp_rejected_and_flush_closes():
    b = CandleBuilder(timeframes=("1m",))
    with pytest.raises(ValueError):
        b.on_tick("NIFTY", 1, datetime(2026, 11, 6, 9, 20))
    b.on_tick("NIFTY", 100, T(9, 20, 5))
    assert b.flush(T(9, 20, 30)) == []
    assert len(b.flush(T(9, 21, 1))) == 1


def test_seed_then_live_no_duplicate_bar():
    b = CandleBuilder(timeframes=("5m",))
    idx = pd.DatetimeIndex([T(9, 15), T(9, 20)])
    df = pd.DataFrame({"open": [1, 2], "high": [2, 3], "low": [0, 1], "close": [1.5, 2.5], "volume": [0, 0]}, index=idx)
    b.seed("NIFTY", "5m", df)
    b.seed("NIFTY", "5m", df)                           # reconnect: re-seed must not duplicate
    assert len(b.closed_df("NIFTY", "5m")) == 2


def _tick(age, ex_lag=0.1, now=1_800_000_000.0):
    return {"price": 1, "exchange_ts_ms": (now - age - ex_lag) * 1000, "received_at": now - age}


def test_live_state_matrix():
    now = 1_800_000_000.0
    assert live_state("LIVE", _tick(1), True, "OPEN", now)["live"] is True
    assert live_state("MOCK", _tick(1), True, "OPEN", now)["state"] == "NOT_LIVE_SOURCE"
    assert live_state("HISTORICAL", _tick(1), True, "OPEN", now)["live"] is False
    assert live_state("LIVE", _tick(1), True, "CLOSED", now)["state"] == "MARKET_CLOSED"
    assert live_state("LIVE", _tick(1), False, "OPEN", now)["state"] == "DISCONNECTED"
    assert live_state("LIVE", None, True, "OPEN", now)["state"] == "UNAVAILABLE"
    assert live_state("LIVE", _tick(60), True, "OPEN", now)["state"] == "STALE"
    # fresh receive time but an old exchange timestamp must also be treated as stale
    assert live_state("LIVE", _tick(1, ex_lag=120), True, "OPEN", now)["state"] == "STALE"
    ok = live_state("LIVE", _tick(1), True, "OPEN", now)
    assert ok["latency_s"] == pytest.approx(0.1, abs=0.01) and "IST" not in ok["data_time_ist"]


def test_parse_candles_drops_zero_price_placeholder():
    from groww_integration.historical import parse_candles
    p = {"candles": [[1762400100, 100, 101, 99, 100.5, 10, 0], [1762400160, 0, 0, 0, 0, 0, 0]]}
    df = parse_candles(p)
    assert len(df) == 1 and df["close"].iloc[-1] == 100.5

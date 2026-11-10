import pandas as pd
import pytest

from config.settings import IST
from market_data.base import MarketDataError
from market_data.factory import get_adapter
from market_data.mock_adapter import MockAdapter
from market_data.validation import validate_candles
from market_data.yahoo_adapter import YahooHistoricalAdapter


def _frame(times, price=100.0):
    idx = pd.DatetimeIndex(times, tz=IST)
    return pd.DataFrame(
        {"open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 0}, index=idx
    )


def test_mock_is_labelled_and_complete():
    df = MockAdapter().get_candles("NIFTY", "5m", 3)
    assert df.attrs["source"] == "MOCK"
    assert len(df) == 3 * 75
    assert df.attrs["report"].ok


def test_mock_is_deterministic():
    a = MockAdapter().get_candles("BANKNIFTY", "15m", 2)
    b = MockAdapter().get_candles("BANKNIFTY", "15m", 2)
    assert a.equals(b)


def test_duplicates_and_order_fixed():
    df = _frame(["2026-01-05 09:20", "2026-01-05 09:15", "2026-01-05 09:15"])
    out, rep = validate_candles(df, "5m")
    assert rep.duplicates_removed == 1
    assert out.index.is_monotonic_increasing


def test_missing_candle_detected_not_filled():
    df = _frame(["2026-01-05 09:15", "2026-01-05 09:25"])
    out, rep = validate_candles(df, "5m")
    assert rep.missing_candles == 1
    assert len(out) == 2  # nothing invented
    assert not rep.ok


def test_out_of_session_and_weekend_removed():
    df = _frame(["2026-01-05 09:10", "2026-01-05 15:30", "2026-01-03 10:00", "2026-01-05 09:15"])
    out, rep = validate_candles(df, "5m")
    assert len(out) == 1 and rep.out_of_session_removed == 3


def test_bad_ohlc_removed():
    df = _frame(["2026-01-05 09:15", "2026-01-05 09:20"])
    df.iloc[1, df.columns.get_loc("high")] = 50  # high below close
    out, rep = validate_candles(df, "5m")
    assert rep.bad_ohlc_removed == 1 and len(out) == 1


def test_naive_index_rejected():
    df = pd.DataFrame({"open": [1], "high": [1], "low": [1], "close": [1], "volume": [0]},
                      index=pd.DatetimeIndex(["2026-01-05 09:15"]))
    with pytest.raises(MarketDataError):
        validate_candles(df, "5m")


def test_yahoo_parse_handles_nulls_and_shape():
    payload = {"chart": {"result": [{
        "timestamp": [1785901500, 1785901800],
        "indicators": {"quote": [{"open": [1.0, None], "high": [2.0, None], "low": [0.5, None],
                                  "close": [1.5, None], "volume": [0, 0]}]}}]}}
    df = YahooHistoricalAdapter.parse(payload)
    out, rep = validate_candles(df, "5m")
    assert rep.null_rows_removed == 1 and len(out) == 1
    with pytest.raises(MarketDataError):
        YahooHistoricalAdapter.parse({"chart": {"result": None, "error": "x"}})


def test_unknown_instrument_and_groww_not_connected():
    with pytest.raises(MarketDataError):
        MockAdapter().get_candles("SENSEX", "5m", 1)
    with pytest.raises(MarketDataError):
        get_adapter("LIVE").get_candles("NIFTY", "5m", 1)


def test_cache_merge_twice_keeps_tz_and_dedupes(tmp_path, monkeypatch):
    from market_data import cache
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    a = MockAdapter().get_candles("NIFTY", "15m", 3)
    first = cache.save_merged(a, "NIFTY", "15m")
    again = cache.save_merged(a, "NIFTY", "15m")      # second run reads the CSV back and merges
    assert len(again) == len(first) == len(a)
    assert str(again.index.tz) == "Asia/Kolkata"

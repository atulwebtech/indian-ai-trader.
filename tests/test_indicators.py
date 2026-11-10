import numpy as np
import pandas as pd
import pytest

from config.settings import IST
from indicators import levels, momentum, price_action, trend, volatility, volume
from indicators.analysis import analyze
from market_data.mock_adapter import MockAdapter


def bars(highs, lows, closes=None, opens=None, vol=0, start="2026-01-05 09:15", freq="5min"):
    idx = pd.date_range(start, periods=len(highs), freq=freq, tz=IST)
    closes = closes if closes is not None else [(h + l) / 2 for h, l in zip(highs, lows)]
    opens = opens if opens is not None else closes
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": vol}, index=idx)


def test_ema_matches_manual_recursion():
    s = pd.Series([10.0, 11, 12, 11, 13])
    a = 2 / (3 + 1)
    exp = [10.0]
    for x in s[1:]:
        exp.append(a * x + (1 - a) * exp[-1])
    assert np.allclose(trend.ema(s, 3), exp)


def test_rsi_extremes():
    up = pd.Series(np.arange(1.0, 40))
    assert momentum.rsi(up).iloc[-1] == 100
    assert momentum.rsi(up[::-1].reset_index(drop=True)).iloc[-1] == pytest.approx(0, abs=1e-9)
    assert momentum.rsi(pd.Series([5.0] * 30)).iloc[-1] == 50


def test_rsi_wilder_known_value():
    # closes 1,2,1: one +1 move and one -1 move -> equal avg gain/loss -> RSI 50
    assert momentum.rsi(pd.Series([1.0, 2.0, 1.0]), period=2).iloc[2] == pytest.approx(50)
    # closes 1,2,3,2: Wilder (alpha=.5): gain .5->(1+.. ) computed by hand below
    c = pd.Series([1.0, 2.0, 3.0, 2.0])
    g, l = [1.0, 1.0, 0.5], [0.0, 0.0, 0.5]  # avg gain/loss at bars 1,2,3
    assert momentum.rsi(c, period=2).iloc[3] == pytest.approx(100 - 100 / (1 + g[2] / l[2]))


def test_macd_hist_sign_in_uptrend():
    m = momentum.macd(pd.Series(np.linspace(100, 200, 80)))
    assert m["hist"].iloc[-1] >= 0 and m["macd"].iloc[-1] > 0


def test_atr_manual():
    df = bars([12, 13, 14], [10, 11, 12], closes=[11, 12, 13])
    tr = volatility.true_range(df)
    assert list(tr) == [2, 2, 2]  # TR1 = 2, then max(2, |13-11|, |11-11|)=2 ...
    assert volatility.atr(df, 2).iloc[-1] == pytest.approx(2.0)


def test_pivots_exact():
    p = levels.classic_pivots(110, 90, 100)
    assert p["P"] == 100 and p["R1"] == 110 and p["S1"] == 90
    assert p["R2"] == 120 and p["S2"] == 80 and p["R3"] == 130 and p["S3"] == 70


def test_swings_not_known_early():
    df = bars([1, 2, 5, 2, 1, 1.5, 1.2], [0, 1, 4, 1, 0, 0.5, 0.2])
    sw = trend.swing_points(df, n=2)
    top = sw[(sw.kind == "high")].iloc[0]
    assert top.pos == 2 and top.confirmed_at == 4  # known only two bars later


def test_structure_uptrend_downtrend():
    highs = [1, 2, 5, 2, 1, 3, 7, 3, 2, 4, 9, 4, 3]
    lows = [h - 1 for h in highs]
    assert trend.market_structure(trend.swing_points(bars(highs, lows)))["structure"] == "UPTREND"
    dh, dl = [20 - l for l in lows], [20 - h for h in highs]
    assert trend.market_structure(trend.swing_points(bars(dh, dl)))["structure"] == "DOWNTREND"


def test_opening_range_only_when_complete():
    df = bars([10, 12, 11, 13], [9, 10, 10, 11])
    assert levels.opening_range(df.iloc[:2], "5m") is None  # only 10 minutes elapsed
    o = levels.opening_range(df, "5m")
    assert o["or_high"] == 12 and o["or_low"] == 9  # 4th bar excluded
    assert levels.opening_range(df, "1h") is None


def test_vwap_manual_and_proxy_label():
    df = bars([11, 13], [9, 11], closes=[10, 12], vol=[100, 300])
    v = levels.session_vwap(df)
    assert v.iloc[1] == pytest.approx((10 * 100 + 12 * 300) / 400)
    assert levels.session_vwap(bars([11], [9])).isna().all()


def test_volume_unavailable_for_index():
    assert volume.volume_summary(bars([11] * 30, [9] * 30))["available"] is False
    df = bars([11] * 30, [9] * 30, vol=[100] * 29 + [400])
    s = volume.volume_summary(df)
    assert s["available"] and s["volume_breakout"] and s["relative_volume"] == pytest.approx(4)


def test_rejection_and_engulfing():
    pin = bars([20], [10], closes=[11], opens=[10.5])  # long upper wick
    assert price_action.rejection(pin.iloc[0]) == "BEARISH_REJECTION"
    two = bars([11, 13], [9, 8.5], closes=[9.5, 12.5], opens=[10.5, 9])
    assert price_action.engulfing(two.iloc[0], two.iloc[1]) == "BULLISH_ENGULFING"


def test_breakout_and_failed_breakout():
    n = 30
    base = bars([10] * n, [9] * n, closes=[9.5] * n)
    up = pd.concat([base, bars([12], [9.5], closes=[11.5], start="2026-01-05 11:45")])
    assert price_action.breakout_state(up)["breakout"] == "BREAKOUT_UP"
    closes = [9.5] * n + [11.5, 9.6, 9.6]
    highs = [10] * n + [12, 10, 10]
    lows = [9] * n + [9.5, 9, 9]
    failed = bars(highs, lows, closes=closes)
    assert price_action.breakout_state(failed)["breakout"] == "FAILED_BREAKOUT_UP"


def test_no_lookahead_in_analyze():
    """Snapshot at bar t must not change if future bars are appended."""
    m = MockAdapter()
    intra = m.get_candles("NIFTY", "5m", 12)
    daily = m.get_candles("NIFTY", "1d", 60)
    cut = intra.iloc[:-40]
    cut.attrs.update(intra.attrs)
    full_to_cut = intra.loc[:cut.index[-1]]
    full_to_cut.attrs.update(intra.attrs)
    a, b = analyze(cut, daily), analyze(full_to_cut, daily)
    assert a == b


def test_analyze_snapshot_complete_on_mock():
    m = MockAdapter()
    s = analyze(m.get_candles("BANKNIFTY", "15m", 30), m.get_candles("BANKNIFTY", "1d", 120))
    for key in ("trend", "momentum", "volume", "volatility", "price_action", "pivots", "opening_range",
                "support_resistance", "vwap"):
        assert key in s
    assert s["source"] == "MOCK" and s["volume"]["available"] is False
    assert s["vwap"]["kind"].startswith("PROXY")

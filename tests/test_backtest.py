import numpy as np
import pandas as pd
import pytest

from backtesting.forecast_backtest import bias_report, target_calibration
from backtesting.metrics import CostModel, summarize
from backtesting.signal_backtest import simulate_trade
from config.settings import IST

NOCOST = CostModel(0)


def bars(rows):
    idx = pd.date_range("2026-01-12 10:30", periods=len(rows), freq="15min", tz=IST)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


BUY = {"direction": "BUY", "stop": 95.0, "instrument": "NIFTY", "setup": "ORB", "time": "t", "quality_score": 70,
       "targets": [{"level": 105.0}, {"level": 110.0}]}


def test_summarize_known_values():
    m = summarize([2, -1, -1, 3, -1, -1, -1])
    assert m["trades"] == 7 and m["win_rate"] == pytest.approx(2 / 7)
    assert m["expectancy"] == pytest.approx(0.0)
    assert m["profit_factor"] == pytest.approx(5 / 5)
    assert m["max_consecutive_losses"] == 3
    assert m["avg_reward_to_risk"] == pytest.approx(2.5 / 1.0)
    assert m["max_drawdown"] == pytest.approx(3.0)  # peak 3 (after 2,-1,-1,3 -> 3) down to 0
    assert summarize([])["trades"] == 0


def test_fill_is_next_bar_open_and_stop_first():
    t = simulate_trade(BUY, bars([(100, 106, 94, 100)]), NOCOST)   # both stop and T1 in one bar
    assert t["entry"] == 100 and t["exit"] == "STOP" and t["gross_r"] == -1


def test_t1_then_t2_pays_half_each():
    t = simulate_trade(BUY, bars([(100, 106, 99, 105), (105, 111, 104, 110)]), NOCOST)
    # risk 5; rr1 1.0, rr2 2.0 -> 0.5*1 + 0.5*2
    assert t["exit"] == "TARGET2" and t["gross_r"] == pytest.approx(1.5)


def test_t2_not_taken_in_same_bar_as_t1():
    t = simulate_trade(BUY, bars([(100, 111, 99, 110)]), NOCOST)
    assert t["exit"] == "EOD"  # T1 reached, T2 only considered from next bar; EOD close 110
    assert t["gross_r"] == pytest.approx(0.5 * 1.0 + 0.5 * 2.0)


def test_breakeven_after_t1():
    t = simulate_trade(BUY, bars([(100, 106, 99, 104), (104, 105, 99.5, 100)]), NOCOST)
    assert t["exit"] == "BREAKEVEN" and t["gross_r"] == pytest.approx(0.5)  # half banked at T1, rest at entry


def test_short_side_and_eod_exit():
    sell = {**BUY, "direction": "SELL", "stop": 105.0, "targets": [{"level": 95.0}, {"level": 90.0}]}
    t = simulate_trade(sell, bars([(100, 101, 99, 99), (99, 100, 98, 98)]), NOCOST)
    assert t["exit"] == "EOD" and t["gross_r"] == pytest.approx(2 / 5)
    t = simulate_trade(sell, bars([(100, 106, 99, 104)]), NOCOST)
    assert t["gross_r"] == -1


def test_gap_through_stop_is_not_filled_and_costs_reduce_r():
    assert simulate_trade(BUY, bars([(94, 96, 93, 95)]), NOCOST) is None
    gross = simulate_trade(BUY, bars([(100, 101, 99, 100)]), NOCOST)["net_r"]
    net = simulate_trade(BUY, bars([(100, 101, 99, 100)]), CostModel(10))["net_r"]
    assert net == pytest.approx(gross - 100 * 10 / 10_000 / 5)


def test_calibration_and_bias_report_shapes():
    rng = np.random.default_rng(0)
    n = 200
    df = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=n).date, "open": 100.0,
                       "close": 100 + rng.normal(0, 1, n), "bias": rng.choice(["BULLISH", "BEARISH", "NEUTRAL"], n)})
    for side in ("up", "dn"):
        for i in (1, 2, 3):
            df[f"{side}{i}_rate"] = 0.5
            df[f"{side}{i}_hit"] = rng.random(n) < 0.5
    cal = target_calibration(df)
    assert len(cal) == 6 and cal["actual_hit"].between(0.35, 0.65).all()
    rep = bias_report(df)
    assert rep["all_days"]["n"] == n and set(rep) >= {"BULLISH", "BEARISH", "NEUTRAL", "trade_bias", "by_quarter"}
    # random labels carry no information: trading them cannot show a big edge
    assert abs(rep["trade_bias"]["expectancy"]) < 0.3


def test_regime_breakdown_covers_every_day_once_per_dimension():
    from backtesting.forecast_backtest import add_regimes, regime_table
    rng = np.random.default_rng(1)
    n = 300
    ref = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    df = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=n).date, "ref": ref, "open": ref,
                       "high": ref * 1.01, "low": ref * 0.99, "close": ref * (1 + rng.normal(0, 0.005, n)),
                       "daily_atr": ref * rng.uniform(0.005, 0.02, n), "bias": rng.choice(["BULLISH", "BEARISH", "NEUTRAL"], n)})
    for side in ("up", "dn"):
        df[f"{side}1_rate"], df[f"{side}1_hit"] = 0.5, rng.random(n) < 0.5
    d = add_regimes(df)
    assert d["vol_regime"].value_counts().min() >= 99          # terciles
    t = regime_table(df)
    assert t[t.regime.str.contains("volatility")]["days"].sum() == n
    assert t[~t.regime.str.contains("volatility")]["days"].sum() == n - 20   # first 20 days have no trend label

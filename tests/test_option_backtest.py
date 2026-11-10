import math

import pytest

from backtesting.option_backtest import _m, backtest_params, build_report, bs, evidence_verdict, modelled_chain, replay, years_to_expiry
from tests.engine_harness import at
from tests.synth import breakout_scenario


def test_black_scholes_put_call_parity_and_greek_signs():
    c, p = bs(24900, 24900, 3 / 365, 0.15, "C"), bs(24900, 24900, 3 / 365, 0.15, "P")
    assert c["price"] - p["price"] == pytest.approx(24900 - 24900 * math.exp(-0.065 * 3 / 365), abs=1e-6)
    assert 0 < c["delta"] < 1 and -1 < p["delta"] < 0 and c["theta"] < 0 and c["gamma"] > 0 and c["vega"] > 0


def test_flat_market_decays_only_by_theta_no_artificial_gap():
    t0, t1 = at(10, 40), at(11, 10)
    p0 = bs(24900, 24900, years_to_expiry(3.0, t0), 0.15, "C")
    p1 = bs(24900, 24900, years_to_expiry(3.0, t1), 0.15, "C")
    expected_decay = abs(p0["theta"]) * (30 / 375)                                # theta per day x fraction of the session
    assert p0["price"] - p1["price"] == pytest.approx(expected_decay, rel=0.15)
    chain = modelled_chain("NIFTY", 24900, 0.15, years_to_expiry(3.0, t0))
    row = chain[chain.strike == 24900].iloc[0]
    assert row.ce_ltp == pytest.approx(p0["price"], abs=0.01)                    # chain and simulation agree at the same instant


def test_replay_runs_has_costs_and_does_not_count_unentered_signals():
    res = replay({"NIFTY": breakout_scenario(1)}, backtest_params(min_score=60.0, wait_score=50.0), iv=15, step_minutes=5)
    L = res["ledger"]
    assert res["rows"] and len(L.data["signals"]) >= 1
    for t in L.trades():
        assert t["costs"]["total"] > 0 and t["net"] == pytest.approx(t["gross"] - t["costs"]["total"], abs=0.02) and t["entered"]
    m = L.metrics()
    assert m["trades"] == len(L.trades()) and m["signals_total"] >= m["trades"]


def test_replay_ignores_future_candles():
    df = breakout_scenario(1)
    changed = df.copy()
    changed.loc[changed.index > at(10, 45), ["open", "high", "low", "close"]] *= 0.5
    kw = dict(params=backtest_params(min_score=60.0, wait_score=50.0), iv=15, step_minutes=5)
    a = [r for r in replay({"NIFTY": df}, **kw)["rows"] if r["time"] <= "2026-11-06T10:45"]
    b = [r for r in replay({"NIFTY": changed}, **kw)["rows"] if r["time"] <= "2026-11-06T10:45"]
    assert a == b                                                                # decisions up to 10:45 cannot see later candles


def test_verdict_never_claims_profit_without_enough_trades():
    fake = lambda n, net: [{"net": net, "costs": {"total": 10.0}} for _ in range(n)]
    assert evidence_verdict(fake(5, 100.0), []).startswith("INSUFFICIENT")
    assert evidence_verdict(fake(60, 100.0), fake(20, 100.0)).startswith("WEAK")
    assert evidence_verdict(fake(150, -50.0), fake(50, -50.0)).startswith("NOT PROVEN")
    # good in-sample, losing out-of-sample: must NOT be called positive
    mixed = fake(100, 300.0) + fake(50, -100.0)
    assert evidence_verdict(mixed, mixed[-40:]).startswith("NOT PROVEN")
    # profit factor high both in-sample and out-of-sample with enough trades
    good = fake(120, 200.0) + fake(60, -100.0)
    assert evidence_verdict(good, fake(40, 200.0) + fake(10, -100.0)).startswith("POSITIVE")


def test_metrics_helper_values():
    trades = [{"net": n, "costs": {"total": 1.0}} for n in (100.0, -50.0, -50.0, 200.0)]
    m = _m(trades)
    assert m["trades"] == 4 and m["win_rate"] == 50.0 and m["net"] == 200.0 and m["profit_factor"] == 3.0
    assert m["max_dd"] == 100.0 and m["max_consec_losses"] == 2


def test_fast_precomputed_frames_equal_the_slow_truncated_resample():
    import pandas as pd
    from backtesting.option_backtest import MINUTES, precompute, frames_at
    df = breakout_scenario(1)
    full = precompute(df)
    for now in (at(10, 41), at(11, 3), at(13, 59)):
        got = frames_at(full, now)
        trunc = df[df.index + pd.Timedelta(minutes=1) <= now]
        for tf, m in MINUTES.items():
            slow = trunc if tf == "1m" else trunc.resample(f"{m}min", origin="start_day", offset="9h15min").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
            slow = slow[slow.index + pd.Timedelta(minutes=m) <= now]
            n = len(got[tf])
            pd.testing.assert_frame_equal(got[tf], slow.tail(n), check_freq=False)
            assert got[tf].index[-1] + pd.Timedelta(minutes=m) <= now                  # nothing that was still forming

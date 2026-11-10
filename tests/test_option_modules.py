from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from config.settings import IST
from indicators import patterns as P
from indicators.extra import bollinger, ema_set, supertrend, vwap
from indicators.market_levels import cpr_and_pivots, fibonacci, session_summaries
from option_chain.chain import ChainError, SnapshotStore, UNAVAILABLE, analyze_chain, contract_quote_info, parse_chain
from risk_management.option_risk import OptionParams, OptionRiskGate, option_params_from_env
from strategies.regime import classify_regime


def bars(rows, start="2026-11-06 09:15", freq="5min", vol=100):
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz=IST)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=vol)


# ---------- indicators ----------
def test_vwap_matches_hand_calc_and_resets_daily_and_nan_without_volume():
    df = bars([(10, 12, 8, 11), (11, 13, 10, 12)], vol=0).assign(volume=[100, 300])
    tp = [(12 + 8 + 11) / 3, (13 + 10 + 12) / 3]
    assert vwap(df).iloc[1] == pytest.approx((tp[0] * 100 + tp[1] * 300) / 400)
    d2 = pd.concat([df, bars([(50, 52, 48, 51)], start="2026-11-09 09:15", vol=10)])
    assert vwap(d2).iloc[2] == pytest.approx((52 + 48 + 51) / 3)          # new day starts fresh
    assert vwap(bars([(1, 2, 0, 1)] * 3, vol=0)).isna().all()              # index spot: no volume -> unavailable


def test_ema_set_marks_short_history_unavailable():
    df = bars([(i, i + 1, i - 1, i) for i in range(60)])
    e = ema_set(df)
    assert e["ema200"].isna().all() and e["ema50"].iloc[:49].isna().all() and not np.isnan(e["ema50"].iloc[-1])


def test_bollinger_and_supertrend_direction():
    up = bars([(i, i + 1, i - 0.5, i + 0.8) for i in range(60)])
    bb = bollinger(up["close"])
    assert (bb["upper"].dropna() >= bb["mid"].dropna()).all()
    assert supertrend(up)["direction"].iloc[-1] == 1
    dn = bars([(100 - i, 101 - i, 99.5 - i, 99.2 - i) for i in range(60)])
    assert supertrend(dn)["direction"].iloc[-1] == -1


def test_cpr_pivots_fibonacci():
    r = cpr_and_pivots(110, 90, 100)
    assert r["pivot"] == pytest.approx(100) and r["cpr_top"] >= r["cpr_bottom"]
    assert r["pivots"]["R1"] == pytest.approx(110) and r["pivots"]["S1"] == pytest.approx(90)
    assert fibonacci(100, 200, "UP")["fib_0.5"] == 150


# ---------- patterns ----------
def test_engulfing_hammer_star_inside():
    assert "BULLISH_ENGULFING" in P.candle_patterns(bars([(105, 106, 99, 100), (99, 108, 98, 107)]))
    assert "BEARISH_ENGULFING" in P.candle_patterns(bars([(100, 106, 99, 105), (106, 107, 97, 98)]))
    assert "HAMMER" in P.candle_patterns(bars([(100, 101, 99, 100), (100, 101.2, 94, 101)]))
    assert "SHOOTING_STAR" in P.candle_patterns(bars([(100, 101, 99, 100), (100, 106, 98.9, 99.2)]))
    assert "INSIDE_BAR" in P.candle_patterns(bars([(100, 110, 90, 105), (101, 105, 95, 102)]))
    assert "MORNING_STAR" in P.star_patterns(bars([(110, 111, 100, 101), (100, 101, 99, 100.2), (101, 110, 100.5, 109)]))
    assert "EVENING_STAR" in P.star_patterns(bars([(100, 111, 99, 110), (110.5, 111.5, 110, 110.8), (110, 110.5, 100, 101)]))


def test_breakout_needs_close_and_age_limit_and_false_breakout():
    df = bars([(100, 101, 99, 100)] * 5 + [(100, 106, 99, 100.5)])        # wick above 102, close back below
    assert P.breakout_close(df, 102, 1) is None                              # wick is not a breakout
    df2 = bars([(100, 101, 99, 100)] * 5 + [(100, 104, 100, 103)])
    b = P.breakout_close(df2, 102, 1)
    assert b and b["age"] == 0 and b["extension"] == pytest.approx(1)
    old = bars([(100, 101, 99, 100)] * 3 + [(100, 104, 100, 103)] * 5)
    assert P.breakout_close(old, 102, 1, max_age=2) is None                  # stale breakout
    trap = bars([(100, 101, 99, 100)] * 3 + [(100, 104, 100, 103), (103, 104, 99, 100)])
    assert P.false_breakout(trap, 102, 1)


def test_liquidity_sweep_and_structure_no_lookahead():
    rows = [(100 + (i % 3), 102 + (i % 3), 98 + (i % 3), 100 + (i % 3)) for i in range(25)] + [(100, 101, 90, 100.5)]
    assert P.liquidity_sweep(bars(rows)) == "BULLISH_SWEEP"
    # structure_events on a prefix must not change when later candles are appended (confirmed swings only)
    rng = np.random.default_rng(1)
    c = 100 + np.cumsum(rng.normal(0, 1, 120))
    full = bars([(x, x + 1, x - 1, x + 0.2) for x in c])
    assert P.structure_events(full.iloc[:80]) == P.structure_events(pd.concat([full.iloc[:80], full.iloc[80:]]).iloc[:80])


# ---------- previous session ----------
def test_session_summaries_skip_missing_days():
    a = bars([(100, 105, 95, 104)] * 4, start="2026-11-02 09:15")
    b = bars([(104, 110, 103, 109)] * 4, start="2026-11-04 09:15")           # 11-03 absent (holiday): not invented
    s = session_summaries(pd.concat([a, b]), 5)
    assert [x["date"] for x in s] == ["2026-11-02", "2026-11-04"] and s[1]["gap_pct"] == pytest.approx(0.0, abs=0.01)


# ---------- option chain ----------
PAYLOAD = {"underlying_ltp": 24860.0, "strikes": {
    "24800": {"CE": {"greeks": {"delta": .6, "gamma": .002, "theta": -9, "vega": 5, "iv": 15}, "trading_symbol": "N24800CE", "ltp": 120, "open_interest": 90000, "volume": 50000},
              "PE": {"greeks": {"delta": -.4, "gamma": .002, "theta": -8, "vega": 5, "iv": 15.5}, "trading_symbol": "N24800PE", "ltp": 60, "open_interest": 200000, "volume": 60000}},
    "24900": {"CE": {"greeks": {"delta": .45, "gamma": .003, "theta": -10, "vega": 5, "iv": 14.8}, "trading_symbol": "N24900CE", "ltp": 80, "open_interest": 150000, "volume": 70000},
              "PE": {"greeks": {"delta": -.55, "gamma": .003, "theta": -9, "vega": 5, "iv": 15.2}, "trading_symbol": "N24900PE", "ltp": 95, "open_interest": 100000, "volume": 40000}}}}


def test_parse_and_analyze_chain():
    df, und = parse_chain(PAYLOAD)
    an = analyze_chain(df, und)
    assert an["atm_strike"] == 24900.0 and und == 24860.0
    assert an["pcr_total"] == pytest.approx(300000 / 240000, rel=1e-3)
    assert an["resistance_oi"]["strike"] == 24900.0 and an["support_oi"]["strike"] == 24800.0
    with pytest.raises(ChainError):
        parse_chain({"nope": 1})


def test_quote_info_spread_and_oi_change_never_invented():
    q = contract_quote_info({"depth": {"buy": [{"price": 100.0, "quantity": 65}], "sell": [{"price": 101.0, "quantity": 65}]},
                             "open_interest": 1200, "previous_open_interest": 1000})
    assert q["spread"] == 1.0 and q["spread_pct"] == pytest.approx(0.995, abs=0.01) and q["oi_change"] == 200
    none = contract_quote_info({"depth": {"buy": [], "sell": []}, "open_interest": 5})
    assert none["bid"] is None and none["spread"] is None and none["oi_change"] is None


def test_oi_snapshot_change_is_unavailable_until_history_exists(tmp_path):
    df, _ = parse_chain(PAYLOAD)
    store = SnapshotStore(tmp_path)
    t0 = datetime(2026, 11, 6, 10, 0, tzinfo=IST)
    assert store.change("NIFTY", "x", t0, df) == UNAVAILABLE
    store.save("NIFTY", "x", t0, df)
    assert store.change("NIFTY", "x", t0 + timedelta(minutes=5), df) == UNAVAILABLE   # too recent
    df2 = df.copy()
    df2["pe_oi"] = df2["pe_oi"] + 1000
    ch = store.change("NIFTY", "x", t0 + timedelta(minutes=20), df2)
    assert ch["pe_oi_change"] == 2000 and ch["ce_oi_change"] == 0 and ch["lean"] == "BULLISH"


# ---------- regime ----------
def test_regime_selection():
    r = classify_regime(30, 30, 10, "BULLISH", "UPTREND", 1.0, 1, 1)
    assert r["regime"] == "TRENDING_BULLISH" and "BREAKOUT" in r["allowed_setups"]
    assert classify_regime(12, 15, 14, "MIXED", "MIXED", 1.0, 0.5, 1)["regime"] == "SIDEWAYS"
    chop = classify_regime(20, 15, 14, "MIXED", "MIXED", 1.0, 2, 1)
    assert chop["regime"] == "CHOPPY" and chop["allowed_setups"] == []


# ---------- risk ----------
def test_params_from_env_validation():
    assert option_params_from_env({"OPT_CAPITAL": "250000", "OPT_MAX_TRADES_PER_DAY": "2"}).max_trades_per_day == 2
    with pytest.raises(ValueError):
        option_params_from_env({"OPT_CAPITAL": "abc"})
    with pytest.raises(ValueError):
        option_params_from_env({"OPT_MAX_RISK_PCT": "20"})
    with pytest.raises(ValueError):
        option_params_from_env({"OPT_MIN_SCORE": "60", "OPT_WAIT_SCORE": "70"})


def test_gate_limits_cooldown_losses_and_restart(tmp_path):
    p = OptionParams(capital=100000, max_trades_per_day=2, max_consecutive_losses=2, cooldown_minutes=20)
    g = OptionRiskGate(p, tmp_path / "g.json")
    t = datetime(2026, 11, 6, 10, 0, tzinfo=IST)
    assert g.blocks("NIFTY", t) == []
    assert g.blocks("NIFTY", t.replace(hour=9, minute=20))                  # before entry window
    g.register({"id": "a", "instrument": "NIFTY", "setup_key": "k1"}, t)
    assert any("Cooldown" in b for b in g.blocks("NIFTY", t + timedelta(minutes=5)))
    assert g.blocks("BANKNIFTY", t + timedelta(minutes=5)) == []
    g2 = OptionRiskGate(p, tmp_path / "g.json")                             # restart keeps the day's state
    assert g2.is_duplicate("k1")
    g2.record_result("a", -500, t)
    g2.record_result("b", -500, t)
    assert any("consecutive" in b for b in g2.blocks("BANKNIFTY", t + timedelta(hours=1)))
    g2.register({"id": "b", "instrument": "NIFTY", "setup_key": "k2"}, t + timedelta(minutes=30))
    assert any("Daily trade limit" in b for b in g2.blocks("NIFTY", t + timedelta(hours=2)))


def test_daily_loss_limit_and_expiry_cutoff(tmp_path):
    g = OptionRiskGate(OptionParams(capital=100000, max_daily_loss_pct=3), tmp_path / "g.json")
    t = datetime(2026, 11, 6, 10, 0, tzinfo=IST)
    g.record_result("a", -3000, t)
    assert g.remaining_daily_loss() == 0 and any("Daily loss" in b for b in g.blocks("NIFTY", t))
    g3 = OptionRiskGate(OptionParams(), tmp_path / "h.json")
    assert any("Expiry day" in b for b in g3.blocks("NIFTY", t.replace(hour=14), expiry_today=True))

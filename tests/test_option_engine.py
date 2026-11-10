import copy

import pytest

from strategies import option_engine as E
from tests.engine_harness import LIVE, at, gate_for, run
from tests.synth import breakout_scenario

RELAXED = dict(min_score=60.0, wait_score=50.0)


@pytest.fixture(scope="module")
def up():
    return breakout_scenario(1)


@pytest.fixture(scope="module")
def down():
    return breakout_scenario(-1)


def test_default_thresholds_wait_instead_of_forcing_a_trade(up, tmp_path):
    g = gate_for(tmp_path)
    s = run(up, at(10, 41), g)
    assert s["action"] == "WAIT FOR CONFIRMATION" and s["status"] == "WAIT_FOR_ENTRY_CONFIRMATION"
    assert s["sides"]["CALL"]["score"] < g.p.min_score and any("below minimum" in m for m in s["wait_for"])
    assert g.state["signals"] == []                      # nothing was issued or counted


def test_buy_call_signal_is_complete_and_consistent(up, tmp_path):
    g = gate_for(tmp_path, **RELAXED)
    s = run(up, at(10, 41), g)
    assert s["action"] == "BUY CALL" and s["status"] == "CONFIRMED" and s["mode"] == "PAPER SIGNAL"
    o, u, p = s["option"], s["underlying"], s["premium"]
    assert o["type"] == "CE" and o["expiry"] == "2026-11-09" and o["symbol"].endswith("CE")
    # underlying levels and premium levels live on different scales and must not be mixed
    assert u["stop"] < u["entry"] < u["targets"][0]["level"] < u["targets"][1]["level"] and u["entry"] > 20000
    assert p["stop"] < p["entry"] < p["targets"][0]["premium"] < p["targets"][1]["premium"] and p["entry"] < 1000
    assert p["entry"] == pytest.approx(s["quote"]["ask"])                        # a buyer pays the ask
    assert p["targets"][0]["rr"] >= g.p.min_rr_t1 and p["targets"][1]["rr"] >= g.p.min_rr_t2
    assert p["lot_size"] == 65 and p["quantity"] == p["lots"] * 65 and p["lots"] >= 1
    assert p["max_loss_rupees"] <= g.max_risk_rupees + 1e-6
    assert s["id"] and s["setup_key"] and s["why_trade"] and s["invalidation"] and s["data"]["state"] == "LIVE"
    assert s["premium"]["estimate_note"]


def test_second_evaluation_is_blocked_no_duplicate(up, tmp_path):
    g = gate_for(tmp_path, **RELAXED)
    assert run(up, at(10, 41), g)["action"] == "BUY CALL"
    again = run(up, at(10, 42), g)
    assert again["action"] == "NO TRADE" and any("Cooldown" in r for r in again["reasons"])
    assert len(g.state["signals"]) == 1


def test_buy_put_mirror(down, tmp_path):
    g = gate_for(tmp_path, **RELAXED)
    for h, m in ((10, 41), (10, 36), (10, 46)):
        s = run(down, at(h, m), g)
        if s["action"].startswith("BUY"):
            break
    assert s["action"] == "BUY PUT", s.get("reasons")
    assert s["option"]["type"] == "PE" and s["premium"]["stop"] < s["premium"]["entry"] < s["premium"]["targets"][0]["premium"]
    assert s["underlying"]["stop"] > s["underlying"]["entry"] > s["underlying"]["targets"][0]["level"]


@pytest.mark.parametrize("state", ["STALE", "DISCONNECTED", "NOT_LIVE_SOURCE", "UNAVAILABLE", "MARKET_CLOSED"])
def test_no_signal_unless_data_is_live(up, tmp_path, state):
    g = gate_for(tmp_path, **RELAXED)
    s = run(up, at(10, 41), g, live={**LIVE, "state": state, "live": False, "reason": state})
    assert s["action"] == "NO TRADE" and s["status"] == "DATA_UNAVAILABLE" and "DATA UNAVAILABLE" in s["reasons"][0]
    assert g.state["signals"] == []


def test_missing_option_chain_and_short_history_block(up, tmp_path):
    g = gate_for(tmp_path, **RELAXED)
    assert run(up, at(10, 41), g, chain_ok=False)["status"] == "NO_OPTION_DATA"
    only_today = up[up.index.date == at(10, 41).date()]
    from tests.engine_harness import MINS
    from tests.synth import closed_until, resample
    fr = {tf: closed_until(resample(only_today, tf), at(10, 41), m) for tf, m in MINS.items()}
    from tests.engine_harness import make_chain
    from option_chain.chain import analyze_chain
    cdf = make_chain(24900.0)
    r = E.evaluate_option_signal("NIFTY", at(10, 41), LIVE, fr, {"available": False}, {"available": False},
                                 (cdf, 24900.0, analyze_chain(cdf, 24900.0)), "2026-11-09", 65, g)
    assert r["status"] == "INSUFFICIENT_HISTORY" and "Not enough closed" in r["reasons"][0]


def test_wide_spread_and_illiquid_contracts_rejected(up, tmp_path):
    s = run(up, at(10, 41), gate_for(tmp_path, **RELAXED), spread=0.10)
    assert s["action"] == "NO TRADE" and s["status"] == "NO_CONTRACT" and "spread" in s["reasons"][0]
    s = run(up, at(10, 41), gate_for(tmp_path, **RELAXED), oi=500, vol=10)
    assert s["status"] == "NO_CONTRACT" and "illiquid" in s["reasons"][0]


def test_stop_that_cannot_fit_risk_budget_is_rejected(up, tmp_path):
    s = run(up, at(10, 41), gate_for(tmp_path, capital=20000, **RELAXED))      # 1% = Rs 200 < one lot's risk
    assert s["action"] == "NO TRADE" and s["status"] == "RISK_REJECTED" and "no sensible position size" in s["reasons"][0]


def test_result_does_not_depend_on_future_candles(up, tmp_path):
    changed = up.copy()
    future = changed.index > at(10, 41)
    changed.loc[future, ["open", "high", "low", "close"]] *= 0.5               # wildly different future
    a = run(up, at(10, 41), gate_for(tmp_path / "a" if False else tmp_path, **RELAXED))
    (tmp_path / "x").mkdir()
    b = run(changed, at(10, 41), gate_for(tmp_path / "x", **RELAXED))
    assert (a["action"], a["sides"]["CALL"]["score"], a["underlying_price"]) == (b["action"], b["sides"]["CALL"]["score"], b["underlying_price"])


def test_why_no_trade_lists_missing_conditions(up, tmp_path):
    s = run(up, at(10, 21), gate_for(tmp_path, **RELAXED))
    assert s["action"] == "NO TRADE" and s["why_no_trade"] and all(isinstance(x, str) for x in s["why_no_trade"])


def test_entry_outside_window_blocked(up, tmp_path):
    s = run(up, at(9, 20), gate_for(tmp_path, **RELAXED))
    assert s["status"] in ("RISK_BLOCKED", "INSUFFICIENT_HISTORY")


# ---------------- lifecycle ----------------
@pytest.fixture
def sig(up, tmp_path):
    return copy.deepcopy(run(up, at(10, 41), gate_for(tmp_path, **RELAXED)))


def test_lifecycle_full_win_path_without_repeated_events(sig):
    p = sig["premium"]
    t0 = at(10, 42)
    r = E.advance_signal(sig, p["entry"], sig["underlying"]["entry"], t0)
    assert (r["status"], r["event"]) == ("ACTIVE", "ENTRY TRIGGERED")
    assert E.advance_signal(sig, p["entry"] + 1, sig["underlying"]["entry"] + 3, t0)["event"] is None   # no repeat
    r = E.advance_signal(sig, p["targets"][0]["premium"] + 0.1, sig["underlying"]["entry"] + 30, t0)
    assert r["event"].startswith("Target 1") and not r["final"] and sig["sl_current"] == p["entry"]
    assert E.advance_signal(sig, p["targets"][0]["premium"] + 0.2, sig["underlying"]["entry"] + 31, t0)["event"] is None
    r = E.advance_signal(sig, p["targets"][1]["premium"] + 0.1, sig["underlying"]["entry"] + 60, t0)
    assert r["status"] in ("TARGET2_HIT", "TARGET3_HIT") and r["final"] and r["pnl_per_unit"] > 0
    assert E.advance_signal(sig, 1, 1, t0)["final"]                                                       # closed signals stay closed


def test_lifecycle_stop_loss_and_breakeven_after_t1(sig):
    p, t0 = sig["premium"], at(10, 42)
    E.advance_signal(sig, p["entry"], sig["underlying"]["entry"], t0)
    r = E.advance_signal(sig, p["stop"] - 0.5, sig["underlying"]["entry"] - 5, t0)
    assert r["status"] == "STOP_LOSS" and r["pnl_per_unit"] < 0 and r["final"]


def test_lifecycle_stopped_at_entry_after_target1(sig):
    p, t0 = sig["premium"], at(10, 42)
    E.advance_signal(sig, p["entry"], sig["underlying"]["entry"], t0)
    E.advance_signal(sig, p["targets"][0]["premium"] + 0.1, sig["underlying"]["entry"] + 30, t0)
    r = E.advance_signal(sig, p["entry"] - 0.1, sig["underlying"]["entry"] + 10, t0)
    assert r["status"] == "STOP_LOSS" and r["pnl_per_unit"] == pytest.approx(0.0, abs=1e-6) and "entry" in r["event"].lower()


def test_lifecycle_invalidated_and_unentered_signals_are_not_trades(sig):
    p, t0 = sig["premium"], at(10, 42)
    E.advance_signal(sig, p["entry"], sig["underlying"]["entry"], t0)
    r = E.advance_signal(sig, p["entry"] - 1, sig["underlying"]["stop"] - 1, t0)
    assert r["status"] == "INVALIDATED" and r["final"]
    s2 = copy.deepcopy(sig)
    s2["lifecycle"] = "CONFIRMED"
    r = E.advance_signal(s2, p["entry_zone"][1] * 1.2, s2["underlying"]["entry"] + 20, at(10, 55))     # never filled, window passed
    assert r["status"] == "EXPIRED" and r["pnl_per_unit"] is None
    s3 = copy.deepcopy(sig)
    s3["lifecycle"] = "CONFIRMED"
    r = E.advance_signal(s3, p["entry"] + 0.5, s3["underlying"]["stop"] - 2, at(10, 43))
    assert r["status"] == "INVALIDATED" and r["pnl_per_unit"] is None


def test_lifecycle_time_exit(sig):
    p = sig["premium"]
    E.advance_signal(sig, p["entry"], sig["underlying"]["entry"], at(10, 42))
    r = E.advance_signal(sig, p["entry"] + 1, sig["underlying"]["entry"] + 2, at(15, 16))
    assert r["status"] == "EXPIRED" and r["final"]


def test_premium_estimate_matches_hand_calc():
    o = {"ltp": 100.0, "delta": 0.5, "gamma": 0.01}
    assert E.premium_at(o, 10) == pytest.approx(100 + 5 + 0.5 * 0.01 * 100)
    assert E.premium_at({"ltp": 100.0, "delta": -0.5, "gamma": 0.01}, -10) == pytest.approx(100 + 5 + 0.5)


def test_score_classification_thresholds_are_configurable():
    from risk_management.option_risk import OptionParams
    p = OptionParams()
    assert [E.classify_score(x, p) for x in (90, 80, 65, 40)] == ["HIGH_QUALITY", "GOOD", "WEAK_WAIT", "NO_TRADE"]
    assert E.classify_score(70, OptionParams(min_score=70, wait_score=50)) == "GOOD"


def test_blocked_decision_still_carries_the_analysis_for_the_dashboard(up, tmp_path):
    g = gate_for(tmp_path, **RELAXED)
    assert run(up, at(10, 41), g)["action"] == "BUY CALL"
    blocked = run(up, at(10, 42), g)                               # cooldown
    assert blocked["status"] == "RISK_BLOCKED" and blocked["action"] == "NO TRADE"
    assert blocked["sides"]["CALL"]["score"] > 0 and blocked["levels"] and isinstance(blocked["regime"], str) and blocked["indicators"]["5m"]
    assert len(g.state["signals"]) == 1                            # a blocked evaluation never registers a signal


def test_side_without_a_trigger_gets_no_price_action_points(up, tmp_path):
    s = run(up, at(10, 41), gate_for(tmp_path, **RELAXED))
    put = {c["name"]: c for c in s["sides"]["PUT"]["components"]}
    assert s["sides"]["PUT"]["trigger"] is None and put["price_action"]["earned"] == 0


# ---- regression: defaults must accept the contracts Groww actually returned on 2026-11-06 ----
def _real_like_chain(price, strike, ltp, delta, theta, oi, vol, side="ce"):
    from option_chain.chain import parse_chain
    leg = {"greeks": {"delta": delta, "gamma": 0.001, "theta": theta, "vega": 8, "iv": 17}, "trading_symbol": f"X{int(strike)}{side.upper()}", "ltp": ltp,
           "open_interest": oi, "volume": vol}
    other = {"greeks": {"delta": -0.4, "gamma": 0.001, "theta": theta, "vega": 8, "iv": 17}, "trading_symbol": "Y", "ltp": 100, "open_interest": oi, "volume": vol}
    return parse_chain({"underlying_ltp": price, "strikes": {str(int(strike)): {"CE": leg if side == "ce" else other, "PE": other if side == "ce" else leg}}})[0]


def _q(spread_pct):
    return lambda sym: {"bid": 100.0, "ask": 100.0 * (1 + spread_pct / 100), "spread_pct": spread_pct}


def test_default_filters_accept_real_nifty_weekly_theta_and_banknifty_lot_based_liquidity():
    from risk_management.option_risk import OptionParams
    p = OptionParams()
    nifty = _real_like_chain(24932, 24900, 220.3, 0.584, -28.01, 87758, 4266134)          # theta 12.7%/day (real value)
    c, why = E.select_contract(1, 24932, nifty, "2026-11-09", _q(0.5), p, at(10, 41), False, 65)
    assert c and c["symbol"].endswith("CE"), why
    bank = _real_like_chain(58909, 58900, 827.85, 0.511, -24.46, 2539, 26726)             # OI 2539 units = 84 lots (real value)
    c, why = E.select_contract(1, 58909, bank, "2026-11-23", _q(0.5), p, at(10, 41), False, 30)
    assert c, why


def test_truly_illiquid_or_expiry_day_theta_still_rejected():
    from risk_management.option_risk import OptionParams
    p = OptionParams()
    thin = _real_like_chain(58909, 58900, 827.85, 0.511, -24.46, 300, 900)                # 10 lots
    assert E.select_contract(1, 58909, thin, "x", _q(0.5), p, at(10, 41), False, 30)[0] is None
    expiry_day = _real_like_chain(24932, 24900, 40.0, 0.52, -25.0, 500000, 9000000)       # 62%/day theta
    c, why = E.select_contract(1, 24932, expiry_day, "x", _q(0.5), p, at(10, 41), True, 65)
    assert c is None and "theta" in why


def test_theta_drag_over_hold_time_is_limited_against_risk():
    from risk_management.option_risk import OptionParams
    p = OptionParams(capital=1_000_000)
    c = {"quote": {"bid": 99.0, "ask": 100.0}, "delta": 0.5, "gamma": 0.0, "theta": -300.0}      # absurd theta: 30 min costs 24 points
    uplan = {"entry": 24900.0, "stop": 24880.0, "targets": [{"level": 24930.0, "basis": "x", "rr": 1.5}, {"level": 24950.0, "basis": "x", "rr": 2.5}]}
    gate = gate_for(__import__("pathlib").Path(__import__("tempfile").mkdtemp()), capital=1_000_000)
    plan, why = E.option_plan(1, c, uplan, p, gate, 65, 24900.0, 30)
    assert plan is None and "Theta" in why


def test_market_closed_still_returns_analysis_but_never_a_trade(up, tmp_path):
    closed = {**LIVE, "state": "MARKET_CLOSED", "live": False, "label": "MARKET CLOSED", "reason": "Market is CLOSED"}
    s = run(up, at(10, 41), gate_for(tmp_path, **RELAXED), live=closed)           # a setup exists at this time, but data is not live
    assert s["action"] == "NO TRADE" and s["status"] == "DATA_UNAVAILABLE" and "DATA UNAVAILABLE" in s["reasons"][0]
    assert s["sides"]["CALL"]["score"] > 0 and s["levels"] and s["indicators"]["5m"] and s["chain"]       # analysis is there for the dashboard
    assert "premium" not in s and "id" not in s and gate_for(tmp_path).state["signals"] == []
    stale = {**LIVE, "state": "STALE", "live": False, "reason": "old"}
    assert "sides" not in run(up, at(10, 41), gate_for(tmp_path, **RELAXED), live=stale)                  # stale/disconnected: no analysis shown as current

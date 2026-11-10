import numpy as np
import pandas as pd
import pytest

from config.settings import IST
from market_data.mock_adapter import MockAdapter
from risk_management.gate import SignalGate, SignalParams
from strategies.signal_engine import evaluate, update_signal


def T(hhmm, day="2026-01-12"):
    return pd.Timestamp(f"{day} {hhmm}", tz=IST)


def sig(id_="s1", inst="NIFTY", direction="BUY", setup="ORB", entry=100.0):
    return {"id": id_, "instrument": inst, "direction": direction, "setup": setup, "entry_ref": entry}


# ---------- gate ----------
def test_entry_window():
    g = SignalGate()
    assert g.blocks("NIFTY", T("09:15"))      # before the opening range is done
    assert not g.blocks("NIFTY", T("10:00"))
    assert g.blocks("NIFTY", T("14:50"))
    assert g.blocks("NIFTY", T("10:00", day="2026-01-11"))  # Sunday


def test_cooldown_and_daily_cap():
    g = SignalGate(SignalParams(cooldown_minutes=45, max_signals_per_day=2))
    g.register(sig("a"), T("10:00"))
    assert any("Cooldown" in r for r in g.blocks("NIFTY", T("10:30")))
    assert not g.blocks("NIFTY", T("10:45"))
    assert not g.blocks("BANKNIFTY", T("10:05"))  # per instrument
    g.register(sig("b"), T("10:45"))
    assert any("cap" in r for r in g.blocks("NIFTY", T("13:00")))


def test_daily_loss_limit_and_streak():
    g = SignalGate()
    g.record_result("NIFTY", -1, T("10:00"))
    g.record_result("NIFTY", -1, T("11:00"))
    assert any("consecutive" in r for r in g.blocks("NIFTY", T("13:00")))
    g2 = SignalGate()
    for r in (-2.0, 0.5, -1.6):
        g2.record_result("NIFTY", r, T("10:00"))
    assert any("loss limit" in r for r in g2.blocks("NIFTY", T("13:00")))
    assert not g2.blocks("BANKNIFTY", T("13:00"))


def test_state_resets_next_day_and_persists(tmp_path):
    path = tmp_path / "state.json"
    g = SignalGate(path=path)
    g.register(sig("a"), T("10:00"))
    g.record_result("NIFTY", -3.5, T("10:30"))
    g2 = SignalGate(path=path)  # restart
    assert g2.blocks("NIFTY", T("13:00"))
    assert not g2.blocks("NIFTY", T("10:00", day="2026-01-13"))


def test_duplicates():
    g = SignalGate()
    g.register(sig("a", entry=100), T("10:00"))
    assert g.is_duplicate("NIFTY", "BUY", "ORB", 150, atr=10)           # same setup
    assert g.is_duplicate("NIFTY", "BUY", "REVERSAL", 102, atr=10)       # same area
    assert not g.is_duplicate("NIFTY", "SELL", "ORB", 100, atr=10)
    g.set_status("a", "STOPPED")
    assert not g.is_duplicate("NIFTY", "BUY", "ORB", 100, atr=10)


# ---------- lifecycle ----------
def mk_bars(rows):
    idx = pd.date_range("2026-01-12 10:15", periods=len(rows), freq="15min", tz=IST)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


BUY = {"direction": "BUY", "stop": 95.0, "valid_for_bars": 4,
       "targets": [{"level": 105.0}, {"level": 110.0}]}


def test_update_signal_paths():
    assert update_signal(BUY, mk_bars([(100, 101, 99, 100)])) == "ACTIVE"
    assert update_signal(BUY, mk_bars([(100, 106, 99, 105)])) == "TARGET1_HIT"
    assert update_signal(BUY, mk_bars([(100, 111, 99, 110)])) == "TARGET2_HIT"
    assert update_signal(BUY, mk_bars([(100, 101, 94, 95)])) == "STOPPED"
    assert update_signal(BUY, mk_bars([(100, 106, 94, 100)])) == "STOPPED"  # same bar: stop assumed first
    assert update_signal(BUY, mk_bars([(100, 101, 99, 100)] * 4)) == "EXPIRED"
    sell = {**BUY, "direction": "SELL", "stop": 105.0, "targets": [{"level": 95.0}, {"level": 90.0}]}
    assert update_signal(sell, mk_bars([(100, 101, 94, 96)])) == "TARGET1_HIT"
    assert update_signal(sell, mk_bars([(100, 106, 99, 100)])) == "STOPPED"


# ---------- engine invariants ----------
@pytest.fixture(scope="module")
def run_mock():
    m = MockAdapter(seed=11)
    out = {}
    for inst in ("NIFTY", "BANKNIFTY"):
        intra, daily = m.get_candles(inst, "15m", 40), m.get_candles(inst, "1d", 200)
        gate = SignalGate()
        res = []
        days = sorted(set(intra.index.date))[-4:]
        for day in days:
            for p in np.where(intra.index.date == day)[0][1:]:
                df = intra.iloc[:p + 1]
                df.attrs.update(intra.attrs)
                res.append(evaluate(inst, df, daily, gate))
        out[inst] = (res, gate)
    return out


def test_actions_are_only_the_four_allowed(run_mock):
    for res, _ in run_mock.values():
        assert {r["action"] for r in res} <= {"BUY", "SELL", "WAIT", "NO TRADE"}
        assert all(r["mode_label"].startswith("PAPER MODE") for r in res)
        assert all(r["disclaimer"] for r in res)


def test_not_a_signal_on_every_candle(run_mock):
    for res, _ in run_mock.values():
        n_sig = sum(r["action"] in ("BUY", "SELL") for r in res)
        assert n_sig <= 0.1 * len(res)


def test_every_signal_is_fully_specified(run_mock):
    p = SignalParams()
    for res, _ in run_mock.values():
        for r in (x for x in res if x["action"] in ("BUY", "SELL")):
            s = 1 if r["action"] == "BUY" else -1
            assert s * (r["entry_ref"] - r["stop"]) > 0
            t1, t2 = r["targets"][0]["level"], r["targets"][1]["level"]
            assert s * (t1 - r["entry_ref"]) > 0 and s * (t2 - t1) > 0
            assert r["rr_t1"] >= p.min_rr_t1 and r["rr_t2"] >= p.min_rr_t2
            assert r["quality_score"] >= p.min_quality and r["strong_components"] >= p.min_strong_components
            assert r["entry_zone"][0] < r["entry_zone"][1]
            assert r["invalidation"] and r["reasons"]
            hhmm = pd.Timestamp(r["time"]).time()
            assert p.entry_start <= hhmm <= p.entry_end
            assert "win probability" in r["disclaimer"]


def test_caps_cooldown_and_unique_ids_hold(run_mock):
    p = SignalParams()
    for inst, (res, _) in run_mock.items():
        sigs = [r for r in res if r["action"] in ("BUY", "SELL")]
        assert len({r["id"] for r in sigs}) == len(sigs)
        by_day = {}
        for r in sigs:
            by_day.setdefault(pd.Timestamp(r["time"]).date(), []).append(pd.Timestamp(r["time"]))
        for times in by_day.values():
            assert len(times) <= p.max_signals_per_day
            for a, b in zip(times, times[1:]):
                assert (b - a).total_seconds() / 60 >= p.cooldown_minutes


def test_blocked_returns_no_trade_with_reason():
    m = MockAdapter()
    intra, daily = m.get_candles("NIFTY", "15m", 40), m.get_candles("NIFTY", "1d", 200)
    g = SignalGate()
    g.record_result("NIFTY", -3.5, pd.Timestamp("2026-01-29 10:00", tz=IST))
    out = evaluate("NIFTY", intra.loc[:"2026-01-29 11:00"], daily, g)
    assert out["action"] == "NO TRADE" and out["blocked_by"]


# ---------- retest setup ----------
def _retest_fixture(low_on_pullback=100.2, bounce_close=101.7):
    """Opening range 99-101 (complete 09:30). Break above at 09:45, pullback to the level, bounce."""
    rows = [("09:15", 99.5, 100.5, 99.0, 100.0), ("09:30", 100.0, 101.0, 99.8, 100.8),      # opening range bars
            ("09:45", 100.8, 102.2, 100.7, 102.0),                                         # break (close > 101)
            ("10:00", 102.0, 102.4, 101.5, 101.8), ("10:15", 101.8, 101.9, low_on_pullback, 101.3),   # pullback touches ~101
            ("10:30", 101.3, 102.6, 101.2, bounce_close)]
    idx = [pd.Timestamp(f"2026-01-12 {t}", tz=IST) for t, *_ in rows]
    day = pd.DataFrame([r[1:] for r in rows], index=idx, columns=["open", "high", "low", "close"])
    day["volume"] = 0
    tgts = [{"label": "T1", "level": 104.0}, {"label": "T2", "level": 106.5}, {"label": "T3", "level": 108.0}]
    f = {"timeframe": "15m", "key_levels": {"opening_range": {"or_high": 101.0, "or_low": 99.0, "complete_at": str(idx[1] + pd.Timedelta(minutes=15))}},
         "scenarios": {"bullish": {"targets": tgts}, "bearish": {"targets": []}},
         "bias": {"bias": "BULLISH"}, "momentum": {"macd_bias": "BULLISH", "rsi": 60}, "volume": {"available": False},
         "volatility": {"range_state": "NORMAL"}, "_ema_alignment": "BULLISH", "_structure": "UPTREND"}
    return f, day


def test_retest_long_found_after_break_pullback_and_bounce():
    from strategies.signal_engine import _retest_candidate
    f, day = _retest_fixture()
    c, why = _retest_candidate(f, day, day, day.index[-1] + pd.Timedelta(minutes=15), 1.0, SignalParams(enable_retest=True, min_rr_t1=1.0))
    assert c is not None, why
    assert c["setup"] == "RETEST" and c["s"] == 1
    assert c["plan"]["stop"] < 101.0 < c["entry_ref"]          # stop under the broken level, entry above it
    assert c["plan"]["targets"][0]["level"] > c["entry_ref"]


def test_retest_rejected_when_level_failed_or_no_touch():
    from strategies.signal_engine import _retest_candidate
    p = SignalParams(enable_retest=True, min_rr_t1=1.0)
    f, day = _retest_fixture()
    day.iloc[4, day.columns.get_loc("close")] = 100.4          # closed back below the level (101 - 0.2 ATR)
    assert _retest_candidate(f, day, day, day.index[-1], 1.0, p)[0] is None
    f, day = _retest_fixture(low_on_pullback=101.9)            # never came back to the level
    day.iloc[5, day.columns.get_loc("low")] = 101.6           # the bounce bar did not reach the level either
    assert _retest_candidate(f, day, day, day.index[-1], 1.0, p)[0] is None


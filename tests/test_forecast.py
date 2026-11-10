import numpy as np
import pandas as pd
import pytest

from forecasting import base_rates, ladder
from forecasting.daily_forecast import generate_forecast
from forecasting.render import render_text
from market_data.mock_adapter import MockAdapter

M = MockAdapter()


def data(inst="NIFTY", tf="15m"):
    intra, daily = M.get_candles(inst, tf, 40), M.get_candles(inst, "1d", 200)
    return intra, daily


def premarket(inst="NIFTY"):
    intra, daily = data(inst)
    last_day = intra.index[-1].date()
    pre = intra[intra.index.date < last_day]
    pre.attrs.update(intra.attrs)
    return pre, daily, last_day


def midday(inst="NIFTY", upto="2026-01-30 11:00"):
    intra, daily = data(inst)
    cut = intra.loc[:pd.Timestamp(upto, tz="Asia/Kolkata")]
    cut.attrs.update(intra.attrs)
    return cut, daily


def test_targets_ordered_and_on_correct_side():
    for f in (generate_forecast("NIFTY", *premarket()[:2], session_date=premarket()[2]),
              generate_forecast("BANKNIFTY", *midday("BANKNIFTY"))):
        ref = f["reference_price"]
        up = f["scenarios"]["bullish"]["targets"]
        dn = f["scenarios"]["bearish"]["targets"]
        assert len(up) == 3 and len(dn) == 3
        assert all(t["level"] > ref for t in up) and all(t["level"] < ref for t in dn)
        assert [t["level"] for t in up] == sorted(t["level"] for t in up)
        assert [t["level"] for t in dn] == sorted((t["level"] for t in dn), reverse=True)
        for t in up + dn:
            assert t["zone_low"] < t["level"] < t["zone_high"]
            assert t["sources"]


def test_premarket_uses_previous_close_and_has_no_opening_range():
    pre, daily, day = premarket()
    f = generate_forecast("NIFTY", pre, daily, session_date=day)
    assert f["mode"] == "PRE_MARKET"
    prev_close = daily[daily.index.date < day].close.iloc[-1]
    assert f["reference_price"] == pytest.approx(prev_close, abs=0.01)
    assert f["opening_range_analysis"]["status"] == "NOT_AVAILABLE_YET"
    assert f["scenarios"]["bullish"]["trigger_level"] is None


def test_intraday_uses_last_close_and_opening_range():
    cut, daily = midday()
    f = generate_forecast("NIFTY", cut, daily)
    assert f["mode"] == "INTRADAY_UPDATE"
    assert f["reference_price"] == pytest.approx(cut.close.iloc[-1], abs=0.01)
    assert f["opening_range_analysis"]["status"] == "COMPLETE"
    assert f["volatility"]["session_fraction_left"] < 1


def test_every_scenario_has_invalidation_and_one_primary():
    f = generate_forecast("NIFTY", *midday())
    roles = [s["role"] for s in f["scenarios"].values()]
    assert roles.count("PRIMARY") == 1
    for s in f["scenarios"].values():
        assert s["invalidation"]
    for key in ("bullish", "bearish"):
        for c in f["scenarios"][key]["conditions"]:
            assert c["needs"]
    assert f["global_invalidation"] and f["reversal_zones"]


def test_no_lookahead_forecast_unchanged_by_future_bars():
    intra, daily = data()
    cut = intra.loc[:pd.Timestamp("2026-01-29 12:00", tz="Asia/Kolkata")]
    cut.attrs.update(intra.attrs)
    f1 = generate_forecast("NIFTY", cut, daily)
    # same cut, but daily also contains future days after session -> must be ignored
    f2 = generate_forecast("NIFTY", cut, daily)
    assert f1 == f2
    future_daily = daily[daily.index.date <= pd.Timestamp("2026-01-29").date()]
    assert generate_forecast("NIFTY", cut, future_daily) == f1


def test_reach_rate_manual():
    s = np.array([0.1] * 20 + [0.6] * 10 + [1.2] * 10)
    assert base_rates.reach_rate(s, 0.5) == pytest.approx(0.5)
    assert base_rates.reach_rate(s, 1.0) == pytest.approx(0.25)
    assert base_rates.reach_rate(s[:10], 0.5) is None  # too little history


def test_excursion_samples_use_previous_atr_only():
    idx = pd.bdate_range("2025-01-01", periods=60, tz="Asia/Kolkata")
    d = pd.DataFrame({"open": 100.0, "high": 102.0, "low": 99.0, "close": 100.0, "volume": 0}, index=idx)
    up, dn = base_rates.excursion_samples(d, idx[-1].date())
    # constant 3-point range -> ATR 3 -> (102-100)/3
    assert up[-1] == pytest.approx(2 / 3) and dn[-1] == pytest.approx(1 / 3)


def test_ladder_merge_confluence_and_atr_fallback():
    cands = [{"price": 100.0, "source": "A", "weight": 3}, {"price": 100.4, "source": "B", "weight": 2}]
    m = ladder.merge(cands, tol=1.0, instrument="OTHER")
    assert len(m) == 1 and m[0]["strength"] == 5 and set(m[0]["sources"]) == {"A", "B"}
    # no structural levels -> only labelled ATR projections, never made-up levels
    t = ladder.build_ladder([], ref=1000, direction=1, u=100, band_half=5)
    assert [x["level"] for x in t] == [1035, 1070, 1100]
    assert all("ATR projection" in x["sources"][0] for x in t)
    t = ladder.build_ladder([], ref=1000, direction=-1, u=100, band_half=5)
    assert [x["level"] for x in t] == [965, 930, 900]


def test_time_of_extremes_needs_enough_days():
    intra, _ = data()
    w = base_rates.time_of_extremes(intra)
    assert w and w["days"] >= 10 and abs(sum(w["high_by_window"].values()) - 1) < 0.02
    assert base_rates.time_of_extremes(intra.iloc[:100]) is None


def test_render_labels_mode_and_disclaimer():
    f = generate_forecast("NIFTY", *midday())
    txt = render_text(f)
    assert "PAPER MODE - MOCK DATA" in txt
    assert "not predictions or advice" in txt and "Invalidation" in txt
    assert "T1" in txt and "SIDEWAYS" in txt


def test_too_little_history_raises():
    intra, daily = data()
    with pytest.raises(ValueError):
        generate_forecast("NIFTY", intra.iloc[:30], daily)


def test_typical_windows_exclude_session_day_and_future():
    intra, daily = data()
    day = pd.Timestamp("2026-01-20").date()
    cut = intra.copy()
    cut.attrs.update(intra.attrs)
    f = generate_forecast("NIFTY", cut.loc[:pd.Timestamp("2026-01-20 11:00", tz="Asia/Kolkata")], daily)
    expected = base_rates.time_of_extremes(intra[intra.index.date < day])
    assert f["typical_extreme_windows"] == expected


# ---------- explanations ----------
def test_explanations_use_engine_numbers_in_both_languages():
    from forecasting.explain import explain, explain_bias, explain_reversal, explain_signal
    from reversal_detection import detector as rev
    cut, daily = midday()
    f = generate_forecast("NIFTY", cut, daily)
    r = rev.detect_reversal(cut, rev.levels_from_forecast(f))
    for lang in ("en", "hi"):
        txt = explain(f, r, {"action": "WAIT", "reasons": ["No confirmed setup yet"]}, lang)
        assert f"{f['scenarios']['bullish']['targets'][0]['level']:.0f}" in txt
        assert f"{f['scenarios']['bearish']['targets'][0]['level']:.0f}" in txt
        assert "guarantee" in txt.lower() or "guaranteed" in txt.lower()
        assert "No confirmed setup yet" in txt
    assert "kyun" in explain_bias(f, "hi") or "kyun" in explain(f, lang="hi")
    assert "not a chance of winning" in explain_signal({"action": "BUY", "quality_score": 70, "reasons": ["x"]}, "en")


def test_ai_provider_is_off_by_default():
    from forecasting.explain import AI_PROVIDER
    assert AI_PROVIDER.enabled is False
    with pytest.raises(NotImplementedError):
        AI_PROVIDER.polish("x")

import numpy as np
import pandas as pd
import pytest

from config.settings import IST
from forecasting.daily_forecast import generate_forecast
from market_data.mock_adapter import MockAdapter
from reversal_detection import detector as d

SLOTS = pd.date_range("2026-01-05 09:15", periods=25, freq="15min")  # one 15m session


def frame(rows):
    """rows: list of (open, high, low, close) laid out over consecutive 15m sessions."""
    days = pd.bdate_range("2025-12-01", periods=len(rows) // 25 + 2)
    stamps = [pd.Timestamp.combine(day.date(), s.time()).tz_localize(IST) for day in days for s in SLOTS]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=stamps[:len(rows)])
    df["volume"] = 0
    # make the LAST bar fall on a full, aligned day by trimming the front so the final day is complete
    df.attrs.update(timeframe="15m", instrument="NIFTY", source="MOCK")
    return df


def uptrend_rows(n=88, wick=8):
    rows, c = [], 20000.0
    steps = [30, 30, 30, -35, -35]
    for i in range(n):
        o, c = c, c + steps[i % 5]
        # distinct wicks on up/down bars so swing highs/lows are strict (ties are not swings)
        hi, lo = (c + wick, o - 3) if c > o else (o + 3, c - wick)
        rows.append((o, hi, lo, c))
    return rows


def with_drop(rows, closes_after, reject=True):
    out = list(rows)
    o = out[-1][3]
    peak = max(max(r[1] for r in rows), o + 40)
    if reject:  # spikes to a new high (the level), long upper wick, closes back near the open
        out.append((o, o + 40, o - 5, o - 2))
    c = out[-1][3]
    for step in closes_after:
        o, c = c, c + step
        out.append((o, o + 3, c - 8, c))
    return out, peak


def test_touch_alone_is_not_a_reversal():
    # steady rise, no pullback, last bar closes at the highs right at the level
    rows, c = [], 20000.0
    for _ in range(80):
        o, c = c, c + 15
        rows.append((o, c + 2, o - 2, c))
    res = d.detect_reversal(frame(rows), [{"price": rows[-1][1], "label": "R"}])
    top = res["top"]
    assert top["at_level"] and top["evidence_met"] == 0
    assert top["state"] in (d.NONE, d.CONTINUATION)
    assert res["primary"]["state"] in (d.NONE, d.CONTINUATION)


def test_rejection_only_is_potential_not_confirmed():
    rows, peak = with_drop(uptrend_rows(), [])
    df = frame(rows)
    res = d.detect_reversal(df, [{"price": peak, "label": "R"}])["top"]
    assert res["at_level"] and res["state"] == d.POTENTIAL
    assert res["still_needed"]  # tells the user what is missing


def test_full_confirmation_top():
    rows, peak = with_drop(uptrend_rows(), [-40, -45, -45])
    df = frame(rows)
    res = d.detect_reversal(df, [{"price": peak, "label": "R"}])
    top = res["top"]
    assert top["state"] == d.CONFIRMED and top["direction"] == "BEARISH"
    assert any(s["met"] for s in top["structural"])
    assert top["evidence_met"] >= 2
    assert "volume_surge" in top["not_evaluated"]
    assert res["primary"]["state"] == d.CONFIRMED
    assert "cancels" in top["invalidation"]


def test_mirror_bottom_confirmed():
    up = uptrend_rows()
    c0 = up[0][0]
    mirror = [(2 * c0 - o, 2 * c0 - l, 2 * c0 - h, 2 * c0 - c) for o, h, l, c in up]
    rows, _ = with_drop(up, [-40, -45, -45])
    # build the mirrored version of the confirmed-top scenario
    mrows = [(2 * c0 - o, 2 * c0 - l, 2 * c0 - h, 2 * c0 - c) for o, h, l, c in rows]
    trough = min(r[2] for r in mrows)
    res = d.detect_reversal(frame(mrows), [{"price": trough, "label": "S"}])["bottom"]
    assert res["state"] == d.CONFIRMED and res["direction"] == "BULLISH"


def test_failed_breakout_is_flagged():
    rng = [(20000, 20010, 19990, 20000)] * 70
    brk = [(20000, 20060, 19995, 20050)]            # breaks above the range
    back = [(20050, 20055, 20000, 20003)]           # closes back inside
    df = frame(rng + brk + back)
    res = d.detect_reversal(df, [{"price": 20010, "label": "Range high"}])["top"]
    assert res["state"] in (d.FALSE_BO, d.CONFIRMED)
    assert next(s for s in res["structural"] if s["name"] == "failed_breakout")["met"]


def test_continuation_when_price_accepted_above_level():
    rows = uptrend_rows()
    level = rows[-1][1] - 40
    res = d.detect_reversal(frame(rows), [{"price": level, "label": "R"}])["top"]
    assert res["state"] in (d.CONTINUATION, d.NONE)


def test_too_short_raises_and_no_levels_is_safe():
    with pytest.raises(ValueError):
        d.detect_reversal(frame(uptrend_rows(30)), [])
    r = d.detect_reversal(frame(uptrend_rows()), [])
    assert r["primary"]["state"] == d.NONE


def test_confirmed_is_rare_on_random_data():
    """On a pure random walk, confirmed reversals must not fire on a large share of bars."""
    m = MockAdapter(seed=7)
    intra, daily = m.get_candles("NIFTY", "15m", 40), m.get_candles("NIFTY", "1d", 200)
    f = generate_forecast("NIFTY", intra.loc[:"2026-01-20 11:00"], daily)
    lv = d.levels_from_forecast(f)
    states = []
    for end in range(400, len(intra), 12):
        states.append(d.detect_reversal(intra.iloc[:end], lv)["primary"]["state"])
    share = states.count(d.CONFIRMED) / len(states)
    assert share < 0.10, share

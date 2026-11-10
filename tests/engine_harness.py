"""Builds everything evaluate_option_signal needs from a synthetic 1-minute frame (tests only)."""
from datetime import datetime

import numpy as np

from config.settings import IST
from indicators.market_levels import cpr_and_pivots
from option_chain.chain import analyze_chain, contract_quote_info, parse_chain
from risk_management.option_risk import OptionParams, OptionRiskGate
from strategies import option_engine as E
from strategies.prev_day import analyze_previous_day, compare_with_today
from tests.synth import closed_until, resample

MINS = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30}
LIVE = {"state": "LIVE", "live": True, "label": "LIVE", "reason": "test", "tick_age_s": 0.5, "latency_s": 0.1, "data_time_ist": "t"}


def make_chain(price, oi=150000, vol=60000):
    strikes = {}
    base = int(price // 50) * 50
    for k in range(base - 400, base + 450, 50):
        tv = 90 * np.exp(-abs(k - price) / 120)
        d = 1 / (1 + np.exp(-(price - k) / 60))
        strikes[str(k)] = {
            "CE": {"greeks": {"delta": round(d, 3), "gamma": 0.003, "theta": -9.0, "vega": 5, "iv": 15},
                   "trading_symbol": f"NIFTY26N09{k}CE", "ltp": round(max(price - k, 0) + tv, 2), "open_interest": oi, "volume": vol},
            "PE": {"greeks": {"delta": round(d - 1, 3), "gamma": 0.003, "theta": -9.0, "vega": 5, "iv": 15},
                   "trading_symbol": f"NIFTY26N09{k}PE", "ltp": round(max(k - price, 0) + tv, 2), "open_interest": oi, "volume": vol}}
    return parse_chain({"underlying_ltp": price, "strikes": strikes})[0]


def run(df1, now, gate, params=None, spread=0.004, oi=150000, vol=60000, live=LIVE, chain_ok=True, lot=65, capital=None, zones=None):
    fr = {tf: closed_until(resample(df1, tf), now, m) for tf, m in MINS.items()}
    price = float(fr["1m"].close.iloc[-1])
    cdf = make_chain(price, oi, vol)
    chain_an = analyze_chain(cdf, price)

    def quote_fn(sym):
        row = cdf[(cdf.ce_symbol == sym) | (cdf.pe_symbol == sym)].iloc[0]
        ltp = row.ce_ltp if row.ce_symbol == sym else row.pe_ltp
        half = ltp * spread / 2
        return contract_quote_info({"depth": {"buy": [{"price": round(ltp - half, 2), "quantity": 650}],
                                              "sell": [{"price": round(ltp + half, 2), "quantity": 650}]}})
    f5, today = fr["5m"], now.date()
    prev = analyze_previous_day(resample(df1, "5m")[lambda d: d.index.date < today], None, today)
    cmp_ = compare_with_today(prev, f5[f5.index.date == today], price)
    cpr = cpr_and_pivots(prev["high"], prev["low"], prev["close"])
    return E.evaluate_option_signal("NIFTY", now, live, fr, prev, cmp_, (cdf, price, chain_an) if chain_ok else None,
                                    "2026-11-09", lot, gate, quote_fn, cpr, zones, None, params)


def gate_for(tmp_path, **kw):
    p = OptionParams(capital=kw.pop("capital", 200000), **kw)
    return OptionRiskGate(p, tmp_path / "gate.json")


def at(h, m, s=5):
    return datetime(2026, 11, 6, h, m, s, tzinfo=IST)

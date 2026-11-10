"""Backtest of the Phase 8 signal engine on intraday candles.

Realism rules (to avoid flattering results):
- A signal is decided on a CLOSED bar; the fill is the NEXT bar's open (no fill at the signal close).
- If the stop and a target are both inside one bar, the stop is assumed to hit first.
- Exit plan: sell half at T1 and move the stop to the entry (breakeven); the rest exits at T2,
  the breakeven stop, or the last bar of the day. Nothing is held overnight.
- Costs (CostModel) are subtracted from every trade.
"""
import numpy as np
import pandas as pd

from backtesting.metrics import CostModel
from risk_management.gate import SignalGate, SignalParams
from strategies.signal_engine import evaluate


def simulate_trade(sig: dict, bars: pd.DataFrame, cost: CostModel):
    """bars = candles AFTER the signal bar, same day. Returns dict or None if not fillable."""
    if bars.empty:
        return None
    s = 1 if sig["direction"] == "BUY" else -1
    entry = float(bars["open"].iloc[0])
    stop = sig["stop"]
    risk = s * (entry - stop)
    t1, t2 = sig["targets"][0]["level"], sig["targets"][1]["level"]
    if risk <= 0 or s * (t1 - entry) <= 0:
        return None  # gapped through the stop or the first target: not a valid fill
    rr1, rr2 = s * (t1 - entry) / risk, s * (t2 - entry) / risk
    t1_done, cur_stop, exit_why, r = False, stop, "EOD", None
    for k, (_, b) in enumerate(bars.iterrows()):
        stop_hit = b["low"] <= cur_stop if s > 0 else b["high"] >= cur_stop
        if stop_hit:
            r = -1.0 if not t1_done else 0.5 * rr1
            exit_why = "STOP" if not t1_done else "BREAKEVEN"
            break
        if t1_done:
            t2_hit = b["high"] >= t2 if s > 0 else b["low"] <= t2
            if t2_hit:
                r, exit_why = 0.5 * rr1 + 0.5 * rr2, "TARGET2"
                break
        else:
            t1_hit = b["high"] >= t1 if s > 0 else b["low"] <= t1
            if t1_hit:
                t1_done, cur_stop = True, entry   # T2 is only considered from the NEXT bar on
    if r is None:
        last = s * (float(bars["close"].iloc[-1]) - entry) / risk
        r = last if not t1_done else 0.5 * rr1 + 0.5 * last
    net = r - cost.points(entry) / risk
    return {"time": str(sig["time"]), "instrument": sig["instrument"], "direction": sig["direction"],
            "setup": sig["setup"], "entry": entry, "stop": stop, "risk_pts": risk, "gross_r": r, "net_r": net,
            "exit": exit_why, "score": sig["quality_score"]}


def run_signal_backtest(instrument: str, intraday: pd.DataFrame, daily: pd.DataFrame, days: int = 50,
                        cost: CostModel = None, params: SignalParams = None) -> list:
    cost = cost or CostModel()
    gate = SignalGate(params)
    trades = []
    for day in sorted(set(intraday.index.date))[-days:]:
        pos = np.where(intraday.index.date == day)[0]
        for p in pos[1:-1]:
            df = intraday.iloc[:p + 1]
            df.attrs.update(intraday.attrs)
            sig = evaluate(instrument, df, daily, gate)
            if sig["action"] not in ("BUY", "SELL"):
                continue
            after = intraday.iloc[p + 1:]
            after = after[after.index.date == day]
            t = simulate_trade(sig, after, cost)
            if t is None:
                gate.set_status(sig["id"], "MISSED")
                continue
            gate.set_status(sig["id"], "CLOSED")
            gate.record_result(instrument, t["net_r"], df.index[-1])
            trades.append(t)
    return trades

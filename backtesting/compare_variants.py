"""Compare rule variants on the same candles, same costs, same fill rules (see signal_backtest).

Usage: python -m backtesting.compare_variants [15m|5m] [days]
Several variants are tried on a small sample, so a variant that looks better may just be luck:
the first-half / second-half split is printed so a change that only works in one half can be spotted.
"""
import sys
from dataclasses import replace

from backtesting.metrics import CostModel, summarize
from backtesting.signal_backtest import run_signal_backtest
from market_data.cache import load_cached
from risk_management.gate import SignalParams

BASE = SignalParams()
VARIANTS = {
    "A baseline (now)": BASE,
    "B R:R T1 >= 1.0": replace(BASE, min_rr_t1=1.0),
    "C B + retest": replace(BASE, min_rr_t1=1.0, enable_retest=True),
    "D C + nearest >=1R target": replace(BASE, min_rr_t1=1.0, enable_retest=True, skip_near_targets=True),
    "E D + older breaks (5 bars)": replace(BASE, min_rr_t1=1.0, enable_retest=True, skip_near_targets=True, orb_max_age_bars=4),
    "F retest only (R:R 1.0)": replace(BASE, min_rr_t1=1.0, enable_retest=True, orb_max_age_bars=-1),
}


def line(rs):
    m = summarize(rs)
    if not m.get("trades"):
        return "no trades"
    return f"n={m['trades']:>3} win={m['win_rate']:.0%} exp={m['expectancy']:+.2f}R total={m['total']:+.1f}R maxDD={m['max_drawdown']:.1f}R"


def main(tf="15m", days=50, only=None):
    cost = CostModel()
    data = {}
    for inst in ("NIFTY", "BANKNIFTY"):
        i, d = load_cached(inst, tf), load_cached(inst, "1d")
        i.attrs.update(instrument=inst, timeframe=tf, source="HISTORICAL")
        data[inst] = (i, d)
    for name, params in VARIANTS.items():
        if only and not name.startswith(only):
            continue
        allr, halves = [], ([], [])
        setups = {}
        for inst, (i, d) in data.items():
            tr = run_signal_backtest(inst, i, d, days=days, cost=cost, params=params)
            for t in tr:
                allr.append(t["net_r"])
                halves[0 if str(t["time"])[:10] < sorted(set(str(x)[:10] for x in i.index))[-days // 2] else 1].append(t["net_r"])
                setups.setdefault(t["setup"], []).append(t["net_r"])
        print(f"{name:<30} ALL {line(allr)}", flush=True)
        print(f"{'':<30} 1st half {line(halves[0])} | 2nd half {line(halves[1])}", flush=True)
        print(f"{'':<30} by setup: " + "; ".join(f"{k} n={len(v)} exp={sum(v)/len(v):+.2f}R" for k, v in setups.items()), flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "15m", int(sys.argv[2]) if len(sys.argv) > 2 else 50, sys.argv[3] if len(sys.argv) > 3 else None)

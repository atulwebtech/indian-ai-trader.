"""Run the full validation and write reports/backtest_YYYY-MM-DD.md."""
import time
from datetime import date

import numpy as np
import pandas as pd

from backtesting.forecast_backtest import bias_report, regime_table, run_forecast_backtest, target_calibration
from backtesting.metrics import CostModel, summarize
from backtesting.signal_backtest import run_signal_backtest
from config.settings import ROOT
from market_data.cache import load_cached


def _fmt(m: dict, unit="R") -> str:
    if not m.get("trades"):
        return "no trades"
    pf = f"{m['profit_factor']:.2f}" if m["profit_factor"] is not None else "n/a"
    rr = f"{m['avg_reward_to_risk']:.2f}" if m["avg_reward_to_risk"] is not None else "n/a"
    t = f"{m['t_stat']:.2f}" if m["t_stat"] is not None else "n/a"
    return (f"trades {m['trades']}, win rate {m['win_rate']:.0%}, profit factor {pf}, expectancy {m['expectancy']:+.3f}{unit}, "
            f"total {m['total']:+.2f}{unit}, max drawdown {m['max_drawdown']:.2f}{unit}, avg reward/risk {rr}, "
            f"max consecutive losses {m['max_consecutive_losses']}, t-stat {t}")


def regime_markdown(res: pd.DataFrame, cost: CostModel) -> list:
    t = regime_table(res, cost)
    L = ["### Different market conditions (each day's volatility and 20-day trend are known before the day; the volatility cut-offs come from the whole sample, so treat this table as descriptive)", "",
         "| Regime | Days | Avg day range | Bull T1 stated/actual | Bear T1 stated/actual | Bias trades | Bias net % per trade |",
         "|---|---|---|---|---|---|---|"]
    for r in t.itertuples():
        L.append(f"| {r.regime} | {r.days} | {r.avg_range_pct:.2f}% | {r.bull_T1_stated:.0%} / {r.bull_T1_actual:.0%} | "
                 f"{r.bear_T1_stated:.0%} / {r.bear_T1_actual:.0%} | {r.bias_trades} | {r.bias_net_pct:+.3f} |")
    return L + [""]


def rebuild_regimes(cost: CostModel = None) -> str:
    """Regime tables from the saved per-day CSVs (no need to re-run the 15-minute forecast loop)."""
    cost, L = cost or CostModel(), []
    for inst in ("NIFTY", "BANKNIFTY"):
        res = pd.read_csv(ROOT / "reports" / f"forecast_backtest_{inst}.csv")
        L += [f"## {inst}: market-condition breakdown", ""] + regime_markdown(res, cost)
    return "\n".join(L)


def run_all(cost: CostModel = None, log=print) -> str:
    cost = cost or CostModel()
    L = [f"# Backtest report - {date.today()}", "",
         f"Cost assumption: {cost.round_trip_bps} bps round trip (brokerage + charges + slippage). Verify this yourself.",
         "All results are historical simulations on delayed Yahoo candles. They do not predict future results.", ""]

    for inst in ("NIFTY", "BANKNIFTY"):
        h, d, i15 = load_cached(inst, "1h"), load_cached(inst, "1d"), load_cached(inst, "15m")

        # ---------- 1. forecast, ~3 years, one fresh out-of-sample test per day ----------
        t0 = time.time()
        log(f"[{inst}] forecast backtest on {h.index[0].date()}..{h.index[-1].date()} ...")
        res = run_forecast_backtest(inst, h, d, progress=lambda k, n: log(f"   {k}/{n} days, {time.time() - t0:.0f}s"))
        res.to_csv(ROOT / "reports" / f"forecast_backtest_{inst}.csv", index=False)
        cal, bias = target_calibration(res), bias_report(res, cost)
        L += [f"## {inst}: daily forecast, {len(res)} days ({res.date.iloc[0]} to {res.date.iloc[-1]})", "",
              "### Target hit rates (did the day's high/low reach the target from the pre-market reference?)", "",
              "| Target | Days | 'Past days moved this far' (stated) | Actually reached |", "|---|---|---|---|"]
        L += [f"| {r.target} | {r.days} | {r.stated_rate:.0%} | {r.actual_hit:.0%} |" for r in cal.itertuples()]
        L += ["", "### Does the pre-market bias say anything about open-to-close?", "",
              "| Bias | Days | Day closed above open | Mean open-to-close % |", "|---|---|---|---|"]
        for k in ("all_days", "BULLISH", "NEUTRAL", "BEARISH"):
            b = bias[k]
            if b["n"]:
                L.append(f"| {k} | {b['n']} | {b['up_close_rate']:.0%} | {b['mean_oc_pct']:+.3f}% |")
        L += ["", f"Trading the bias open-to-close, net of costs (units = % of price): {_fmt(bias['trade_bias'], '%')}", "",
              "By quarter (mean net % per trade): " + ", ".join(f"{q}: {v['mean_net_pct']:+.3f} (n={v['n']})"
                                                               for q, v in bias["by_quarter"].items()), ""]

        L += regime_markdown(res, cost)

        # ---------- 2. signal engine on 15m candles ----------
        log(f"[{inst}] signal backtest on 15m ...")
        i15.attrs.update(instrument=inst, timeframe="15m", source="HISTORICAL")
        trades = run_signal_backtest(inst, i15, d, days=50, cost=cost)
        pd.DataFrame(trades).to_csv(ROOT / "reports" / f"signal_backtest_{inst}.csv", index=False)
        net = [t["net_r"] for t in trades]
        L += [f"## {inst}: signal engine, 15m, last ~50 sessions", "", f"All: {_fmt(summarize(net))}",
              f"Before costs: {_fmt(summarize([t['gross_r'] for t in trades]))}"]
        if len(trades) >= 9:
            k = len(trades) // 3
            for name, part in (("first third", trades[:k]), ("middle third", trades[k:2 * k]), ("last third", trades[2 * k:])):
                L.append(f"- {name}: {_fmt(summarize([t['net_r'] for t in part]))}")
        L += ["", "Caution: a few dozen trades cannot establish an edge either way.", ""]
    text = "\n".join(L)
    (ROOT / "reports" / f"backtest_{date.today()}.md").write_text(text)
    return text


if __name__ == "__main__":
    print(run_all())

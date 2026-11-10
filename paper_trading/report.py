"""Plain-text summary of the paper-trading ledger (simulated trades only)."""
from backtesting.metrics import summarize
from config.settings import ROOT
from paper_trading.ledger import Ledger


def report_text(path=None) -> str:
    items = Ledger(path or ROOT / "data" / "paper_ledger.json").items
    if not items:
        return "Paper trading: no signals recorded yet. Run './run.sh telegram' during market hours to collect them."
    done = [i for i in items if i["r"] is not None]
    lines = [f"Paper trading ledger: {len(items)} signals, {len(done)} closed, {len(items) - len(done)} still open",
             "Results are in R (multiples of the risk at the stop), BEFORE brokerage and slippage."]
    if done:
        m = summarize([i["r"] for i in done])
        pf = f"{m['profit_factor']:.2f}" if m["profit_factor"] is not None else "n/a"
        lines.append(f"Win rate {m['win_rate']:.0%} | profit factor {pf} | expectancy {m['expectancy']:+.2f}R | total {m['total']:+.2f}R | "
                     f"max drawdown {m['max_drawdown']:.2f}R | max consecutive losses {m['max_consecutive_losses']}")
        if m["trades"] < 30:
            lines.append(f"Only {m['trades']} closed trades: far too few to judge any edge (aim for 100+).")
    for i in items[-15:]:
        s = i["signal"]
        lines.append(f"  {s['time'][:16]} {s['instrument']:9s} {s['action']:4s} {s['setup']:8s} entry~{s['entry_ref']:.0f} "
                     f"stop {s['stop']:.0f} -> {i['status']}" + (f" ({i['r']:+.2f}R)" if i["r"] is not None else ""))
    return "\n".join(lines)


def print_report():
    print(report_text())

"""Cost model and performance metrics for backtests."""
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CostModel:
    """Round-trip trading cost as basis points of price (1 bp = 0.01%).

    The default is an ASSUMPTION covering brokerage, statutory charges and slippage on an index
    futures trade. Real costs depend on the instrument, broker plan and current tax rules and
    must be checked by the user before trusting any result.
    """
    round_trip_bps: float = 4.0

    def points(self, price: float) -> float:
        return price * self.round_trip_bps / 10_000


def summarize(r) -> dict:
    """Metrics for a list of per-trade results (any consistent unit: R, points or %)."""
    r = np.asarray(list(r), dtype=float)
    n = len(r)
    if n == 0:
        return {"trades": 0}
    wins, losses = r[r > 0], r[r < 0]
    cum = np.cumsum(r)
    dd = float((np.maximum.accumulate(np.maximum(cum, 0)) - cum).max())
    streak = best = 0
    for x in r:
        streak = streak + 1 if x < 0 else 0
        best = max(best, streak)
    gross_loss = -losses.sum()
    return {"trades": n, "win_rate": float(len(wins) / n),
            "avg_win": float(wins.mean()) if len(wins) else 0.0,
            "avg_loss": float(losses.mean()) if len(losses) else 0.0,
            "avg_reward_to_risk": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
            "profit_factor": float(wins.sum() / gross_loss) if gross_loss > 0 else None,
            "expectancy": float(r.mean()), "total": float(r.sum()), "max_drawdown": dd,
            "max_consecutive_losses": best,
            "t_stat": float(r.mean() / (r.std(ddof=1) / np.sqrt(n))) if n > 2 and r.std(ddof=1) > 0 else None}

"""Signal gate: entry window, cooldown, duplicate prevention, per-day caps and daily loss limit.

It is driven by BAR TIMESTAMPS (not the wall clock) so the same code runs live, in paper mode
and in backtests. State can be saved to JSON so a restart does not forget today's signals.
"""
import json
from dataclasses import dataclass
from datetime import time
from pathlib import Path


@dataclass(frozen=True)
class SignalParams:
    entry_start: time = time(9, 30)        # after the 15-minute opening range
    entry_end: time = time(14, 45)         # no fresh entries in the last 45 minutes
    cooldown_minutes: int = 45             # per instrument after any emitted signal
    max_signals_per_day: int = 3           # per instrument
    max_signals_per_day_total: int = 3     # across NIFTY and BANKNIFTY together: only a few, selective alerts
    daily_loss_limit_r: float = -3.0       # total realised R per instrument-day; hit -> stop for the day
    max_consecutive_losses: int = 2        # then stop for the day
    duplicate_atr_tol: float = 0.5         # same direction within this many ATR of an active signal
    min_quality: float = 65.0              # quality score needed for BUY/SELL
    min_strong_components: int = 4         # components scoring >= 50% of their max
    min_rr_t1: float = 1.2
    min_rr_t2: float = 1.8
    min_risk_atr: float = 0.8              # stop at least this far (noise filter)
    max_risk_atr: float = 2.5              # stops wider than this are rejected
    max_chase_atr: float = 0.5             # do not enter this far beyond the trigger
    signal_valid_bars: int = 8             # signal expires if not acted on / resolved
    orb_max_age_bars: int = 2              # opening-range break older than this many bars is stale (0 = break bar only)
    enable_retest: bool = False            # also look for break -> pullback to the level -> bounce
    retest_max_bars: int = 12              # a retest must come within this many bars of the break
    retest_touch_atr: float = 0.3          # pullback must come within this many ATR of the level
    skip_near_targets: bool = False        # T1 = nearest ladder level that is at least min_rr_t1 away


def active_params() -> SignalParams:
    """ACTIVE profile (user's choice): up to 5 signals a day, looser rules. Backtests did NOT show this is better."""
    from dataclasses import replace
    return replace(SignalParams(), cooldown_minutes=30, max_signals_per_day=3, max_signals_per_day_total=5, min_quality=58.0,
                   min_strong_components=3, min_rr_t1=1.0, min_rr_t2=1.5, enable_retest=True, skip_near_targets=True,
                   orb_max_age_bars=4)


def params_for(profile: str) -> SignalParams:
    return active_params() if str(profile).lower() == "active" else SignalParams()


class SignalGate:
    def __init__(self, params: SignalParams = None, path=None, calendar=None):
        from market_data.calendar import default_calendar
        self.p = params or SignalParams()
        self.calendar = calendar or default_calendar()
        self.path = Path(path) if path else None
        self.state = {"date": None, "signals": [], "results": []}
        if self.path and self.path.exists():
            try:
                self.state = json.loads(self.path.read_text())
            except (OSError, ValueError):
                pass

    # ---- housekeeping ----
    def _roll(self, ts):
        d = str(ts.date())
        if self.state.get("date") != d:
            self.state = {"date": d, "signals": [], "results": []}

    def _save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.state, indent=1))

    def in_entry_window(self, decision_time) -> bool:
        t = decision_time.time()
        return self.calendar.is_trading_day(decision_time.date()) and self.p.entry_start <= t <= self.p.entry_end

    # ---- checks ----
    def blocks(self, instrument: str, decision_time) -> list:
        """Reasons new signals are NOT allowed right now (empty list = allowed)."""
        self._roll(decision_time)
        why = []
        if not self.in_entry_window(decision_time):
            kind = self.calendar.day_type(decision_time.date())
            why.append(f"Not a regular trading day ({kind.lower()})" if kind != "REGULAR" else
                       f"Outside entry window {self.p.entry_start:%H:%M}-{self.p.entry_end:%H:%M} IST")
        if len(self.state["signals"]) >= self.p.max_signals_per_day_total:
            why.append(f"Daily limit of {self.p.max_signals_per_day_total} signals across both indices reached")
        mine = [s for s in self.state["signals"] if s["instrument"] == instrument]
        if len(mine) >= self.p.max_signals_per_day:
            why.append(f"Daily signal cap reached ({self.p.max_signals_per_day})")
        if mine:
            last = max(mine, key=lambda s: s["time"])
            mins = (decision_time - _ts(last["time"], decision_time)).total_seconds() / 60
            if mins < self.p.cooldown_minutes:
                why.append(f"Cooldown: {self.p.cooldown_minutes - mins:.0f} min left after last signal")
        res = [r["r"] for r in self.state["results"] if r["instrument"] == instrument]
        if sum(res) <= self.p.daily_loss_limit_r:
            why.append(f"Daily loss limit hit ({sum(res):+.1f}R <= {self.p.daily_loss_limit_r:+.1f}R)")
        streak = 0
        for r in reversed(res):
            if r < 0:
                streak += 1
            else:
                break
        if streak >= self.p.max_consecutive_losses:
            why.append(f"{streak} consecutive losses: stopped for the day")
        return why

    def is_duplicate(self, instrument: str, direction: str, setup: str, entry_ref: float, atr: float) -> bool:
        for s in self.state["signals"]:
            if s["instrument"] != instrument or s["direction"] != direction or s["status"] != "ACTIVE":
                continue
            if s["setup"] == setup or abs(s["entry_ref"] - entry_ref) <= self.p.duplicate_atr_tol * atr:
                return True
        return False

    # ---- state changes ----
    def register(self, signal: dict, decision_time):
        self._roll(decision_time)
        self.state["signals"].append({
            "id": signal["id"], "instrument": signal["instrument"], "direction": signal["direction"],
            "setup": signal["setup"], "entry_ref": signal["entry_ref"], "time": str(decision_time),
            "status": "ACTIVE"})
        self._save()

    def set_status(self, signal_id: str, status: str):
        for s in self.state["signals"]:
            if s["id"] == signal_id:
                s["status"] = status
        self._save()

    def record_result(self, instrument: str, r_multiple: float, decision_time):
        """Paper-trading/backtest reports each closed trade's result in R (reward multiples of risk)."""
        self._roll(decision_time)
        self.state["results"].append({"instrument": instrument, "r": float(r_multiple), "time": str(decision_time)})
        self._save()


def _ts(s, like):
    import pandas as pd
    t = pd.Timestamp(s)
    return t if t.tzinfo else t.tz_localize(like.tzinfo)

"""Risk parameters and gate for OPTION BUYING paper signals. All limits are configurable (OPT_* environment variables).

Money is in rupees and always uses the lot size read from the instrument master, never an assumed one.
The gate is driven by the timestamps it is given (not the wall clock) and persists to JSON so a restart keeps today's state.
"""
import json
import os
from dataclasses import dataclass, fields, replace
from datetime import datetime, time
from pathlib import Path

from config.settings import ROOT


@dataclass(frozen=True)
class OptionParams:
    # capital and risk
    capital: float = 10000.0               # OPT_CAPITAL: capital the paper account trades with (rupees)
    max_risk_pct: float = 1.0              # OPT_MAX_RISK_PCT: max loss per trade, % of capital
    max_daily_loss_pct: float = 3.0        # OPT_MAX_DAILY_LOSS_PCT: stop for the day after this realised loss
    max_trades_per_day: int = 3            # OPT_MAX_TRADES_PER_DAY (both indices together)
    max_consecutive_losses: int = 2        # OPT_MAX_CONSECUTIVE_LOSSES
    max_capital_per_trade_pct: float = 50.0  # premium paid for the position, % of capital
    # sessions
    entry_start: time = time(9, 30)
    entry_end: time = time(14, 45)
    expiry_day_cutoff: time = time(13, 30)  # on expiry day no fresh entries after this (theta/gamma)
    cooldown_minutes: int = 20
    # score thresholds (configurable, to be validated by backtests)
    min_score: float = 75.0                # OPT_MIN_SCORE: BUY CALL/PUT only at or above this
    wait_score: float = 60.0               # between wait_score and min_score: wait for confirmation
    high_quality_score: float = 85.0
    # setup rules (underlying, in ATR of the 5m chart)
    min_risk_atr: float = 0.6
    max_risk_atr: float = 1.8
    max_chase_atr: float = 0.6             # entry price already this far beyond the trigger level -> do not chase
    breakout_max_age_bars: int = 2
    min_rr_t1: float = 1.3                 # on option premium
    min_rr_t2: float = 2.0
    # option contract filters
    delta_min: float = 0.35
    delta_max: float = 0.75
    min_premium: float = 15.0
    min_oi_lots: int = 50                  # open interest in LOTS (Groww reports units; lot sizes differ per index)
    min_option_volume_lots: int = 50       # today's traded volume in lots
    max_spread_pct: float = 3.0            # the real liquidity test: bid-ask spread from get_quote
    max_theta_pct: float = 25.0            # |theta| / premium, % per day (weekly options near expiry are 10-25% in normal conditions)
    max_theta_drag_risk: float = 0.35      # theta lost during hold_minutes may not exceed this fraction of the premium risk
    max_premium_loss_pct: float = 35.0     # premium stop may not be wider than this % of entry
    hold_minutes: int = 30                 # expected holding time used for the theta drag on targets
    trail_to_entry_after_t1: bool = True
    entry_valid_minutes: int = 5
    max_hold_minutes: int = 90
    square_off: time = time(15, 15)        # paper signals still open at this time are closed as EXPIRED


def option_params_from_env(env=None) -> OptionParams:
    env = os.environ if env is None else env
    names = {"OPT_CAPITAL": "capital", "OPT_MAX_RISK_PCT": "max_risk_pct", "OPT_MAX_DAILY_LOSS_PCT": "max_daily_loss_pct",
             "OPT_MAX_TRADES_PER_DAY": "max_trades_per_day", "OPT_MAX_CONSECUTIVE_LOSSES": "max_consecutive_losses",
             "OPT_MIN_SCORE": "min_score", "OPT_WAIT_SCORE": "wait_score", "OPT_MAX_SPREAD_PCT": "max_spread_pct",
             "OPT_MIN_RR_T1": "min_rr_t1", "OPT_MIN_RR_T2": "min_rr_t2", "OPT_MIN_OI_LOTS": "min_oi_lots",
             "OPT_MIN_OPTION_VOLUME_LOTS": "min_option_volume_lots", "OPT_MAX_CHASE_ATR": "max_chase_atr",
             "OPT_MAX_THETA_PCT": "max_theta_pct"}
    types = {f.name: f.type for f in fields(OptionParams)}
    upd = {}
    for var, field in names.items():
        raw = str(env.get(var, "")).strip()
        if raw:
            try:
                upd[field] = (int if types[field] in (int, "int") else float)(raw)
            except ValueError as e:
                raise ValueError(f"{var} must be a number, got {raw!r}") from e
    p = replace(OptionParams(), **upd)
    if p.capital <= 0 or not (0 < p.max_risk_pct <= 5):
        raise ValueError("OPT_CAPITAL must be > 0 and OPT_MAX_RISK_PCT must be between 0 and 5")
    if p.wait_score > p.min_score:
        raise ValueError("OPT_WAIT_SCORE cannot be above OPT_MIN_SCORE")
    return p


class OptionRiskGate:
    def __init__(self, params: OptionParams = None, path=None):
        self.p = params or OptionParams()
        self.path = Path(path) if path else ROOT / "data" / "option_gate.json"
        self.state = {"date": None, "signals": [], "results": []}
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text())
            except (OSError, ValueError):
                pass

    def _roll(self, now: datetime):
        if self.state.get("date") != str(now.date()):
            self.state = {"date": str(now.date()), "signals": [], "results": []}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.state))
        except OSError:
            pass

    @property
    def max_risk_rupees(self) -> float:
        return self.p.capital * self.p.max_risk_pct / 100

    def realised_pnl(self) -> float:
        return float(sum(r["pnl"] for r in self.state["results"]))

    def remaining_daily_loss(self) -> float:
        return max(self.p.capital * self.p.max_daily_loss_pct / 100 + min(self.realised_pnl(), 0), 0.0)

    def consecutive_losses(self) -> int:
        n = 0
        for r in reversed(self.state["results"]):
            if r["pnl"] < 0:
                n += 1
            else:
                break
        return n

    def blocks(self, instrument: str, now: datetime, expiry_today: bool = False) -> list:
        self._roll(now)
        p, out, t = self.p, [], now.time()
        if not (p.entry_start <= t <= p.entry_end):
            out.append(f"Outside entry window {p.entry_start:%H:%M}-{p.entry_end:%H:%M}")
        if expiry_today and t > p.expiry_day_cutoff:
            out.append(f"Expiry day: no fresh entries after {p.expiry_day_cutoff:%H:%M}")
        if len(self.state["signals"]) >= p.max_trades_per_day:
            out.append(f"Daily trade limit reached ({p.max_trades_per_day})")
        if self.remaining_daily_loss() <= 0:
            out.append(f"Daily loss limit reached ({p.max_daily_loss_pct}% of capital)")
        if self.consecutive_losses() >= p.max_consecutive_losses:
            out.append(f"{p.max_consecutive_losses} consecutive losses: stopped for the day")
        for s in reversed(self.state["signals"]):
            if s["instrument"] == instrument:
                mins = (now - datetime.fromisoformat(s["time"])).total_seconds() / 60
                if mins < p.cooldown_minutes:
                    out.append(f"Cooldown: {instrument} signal {mins:.0f} min ago (< {p.cooldown_minutes})")
                break
        return out

    def is_duplicate(self, key: str) -> bool:
        return any(s["key"] == key for s in self.state["signals"])

    def register(self, sig: dict, now: datetime):
        self._roll(now)
        self.state["signals"].append({"id": sig["id"], "instrument": sig["instrument"], "key": sig["setup_key"], "time": now.isoformat()})
        self._save()

    def record_result(self, sig_id: str, pnl_rupees: float, now: datetime):
        self._roll(now)
        self.state["results"].append({"id": sig_id, "pnl": float(pnl_rupees)})
        self._save()

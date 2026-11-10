"""Paper ledger for option signals. PAPER ONLY: nothing here was executed with a broker.
A signal counts as a trade only if ENTRY was triggered. Signals that expired or were invalidated before entry are stored
but never counted as wins, losses or P&L. Writes are atomic and survive a restart."""
import json
import os
import tempfile
from pathlib import Path

from config.settings import ROOT
from paper_trading.costs import CostModel

FINAL = ("TARGET2_HIT", "TARGET3_HIT", "STOP_LOSS", "INVALIDATED", "EXPIRED")


class OptionLedger:
    def __init__(self, path=None, costs: CostModel = None):
        self.path = Path(path) if path else ROOT / "data" / "option_ledger.json"
        self.costs = costs or CostModel.from_env()
        self.data = {"signals": {}, "alerts": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
                self.data.setdefault("alerts", {})
            except (OSError, ValueError):
                pass

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, default=str)
        os.replace(tmp, self.path)

    # ---- signals ----
    def add(self, sig: dict):
        if sig["id"] in self.data["signals"]:
            return False                                   # duplicate id: never store twice
        self.data["signals"][sig["id"]] = {"id": sig["id"], "instrument": sig["instrument"], "action": sig["action"], "created_at": sig["created_at"],
                                           "status": "CONFIRMED", "entered": False, "entry_time": None, "exit_time": None,
                                           "option": sig["option"], "premium": sig["premium"], "underlying": sig["underlying"],
                                           "score": sig["score"], "pattern": sig["pattern"], "regime": sig["regime_name"],
                                           "why": sig["why_trade"], "invalidation": sig["invalidation"], "sig": sig,
                                           "exit_premium": None, "gross": None, "costs": None, "net": None, "events": []}
        self._save()
        return True

    def get(self, sid):
        return self.data["signals"].get(sid)

    def open_signals(self, instrument=None):
        return [s for s in self.data["signals"].values() if s["status"] not in FINAL and (instrument is None or s["instrument"] == instrument)]

    def apply(self, sid: str, result: dict, now, exit_premium: float = None):
        """Record the outcome of advance_signal(). Returns the net P&L (rupees) when the signal closes as a real trade, else None."""
        s = self.data["signals"][sid]
        if result.get("event"):
            s["events"].append({"t": now.isoformat(timespec="seconds"), "event": result["event"]})
        st = result["status"]
        if st == "ACTIVE" and not s["entered"]:
            s["entered"], s["entry_time"] = True, now.isoformat(timespec="seconds")
        if st == "TARGET1_HIT":
            s["entered"] = True
        s["status"] = st if st != "TARGET1_HIT" else "TARGET1_HIT"
        net = None
        if result.get("final"):
            s["exit_time"] = now.isoformat(timespec="seconds")
            s["status"] = st
            if s["entered"] and result.get("pnl_per_unit") is not None:
                qty = s["premium"]["quantity"]
                entry = s["premium"]["entry"]
                ex = exit_premium if exit_premium is not None else entry + result["pnl_per_unit"]
                gross = (ex - entry) * qty
                c = self.costs.round_trip(entry, max(ex, 0.0), qty)
                s["exit_premium"], s["gross"], s["costs"], s["net"] = round(ex, 2), round(gross, 2), c, round(gross - c["total"], 2)
                net = s["net"]
            else:
                s["counted"] = False
        self._save()
        return net

    # ---- alert de-duplication (survives restarts) ----
    def alert_sent(self, key: str) -> bool:
        return key in self.data["alerts"]

    def mark_alert(self, key: str, when):
        self.data["alerts"][key] = when.isoformat(timespec="seconds")
        if len(self.data["alerts"]) > 2000:
            for k in sorted(self.data["alerts"], key=self.data["alerts"].get)[:500]:
                self.data["alerts"].pop(k)
        self._save()

    # ---- metrics ----
    def trades(self):
        return sorted([s for s in self.data["signals"].values() if s["entered"] and s["net"] is not None], key=lambda s: s["exit_time"])

    def metrics(self, day=None) -> dict:
        allsig = list(self.data["signals"].values())
        trades = [t for t in self.trades() if day is None or t["exit_time"][:10] == day]
        nets = [t["net"] for t in trades]
        wins, losses = [n for n in nets if n > 0], [n for n in nets if n <= 0]
        cum, peak, mdd = 0.0, 0.0, 0.0
        for n in nets:
            cum += n
            peak = max(peak, cum)
            mdd = max(mdd, peak - cum)
        gp, gl = sum(wins), -sum(losses)
        rrs = [(t["premium"]["targets"][0]["premium"] - t["premium"]["entry"]) / t["premium"]["risk_per_unit"] for t in trades]
        pool = [s for s in allsig if day is None or s["created_at"][:10] == day]
        return {"signals_total": len(pool), "trades": len(trades),
                "not_entered": sum(1 for s in pool if s["status"] in ("EXPIRED", "INVALIDATED") and not s["entered"]),
                "open": sum(1 for s in pool if s["status"] not in FINAL), "wins": len(wins), "losses": len(losses),
                "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else None,
                "profit_factor": round(gp / gl, 2) if gl > 0 else (None if not gp else "inf"),
                "net_pnl": round(sum(nets), 2), "gross_pnl": round(sum(t["gross"] for t in trades), 2),
                "costs": round(sum(t["costs"]["total"] for t in trades), 2), "max_drawdown": round(mdd, 2),
                "avg_win": round(gp / len(wins), 2) if wins else None, "avg_loss": round(-gl / len(losses), 2) if losses else None,
                "avg_planned_rr_t1": round(sum(rrs) / len(rrs), 2) if rrs else None,
                "note": "PAPER results; only signals whose entry triggered are counted. Fewer than 30 trades proves nothing."}

    def history(self, limit=50):
        rows = sorted(self.data["signals"].values(), key=lambda s: s["created_at"], reverse=True)[:limit]
        return [{k: r[k] for k in ("id", "instrument", "action", "created_at", "status", "entered", "score", "pattern", "net", "events")} |
                {"entry": r["premium"]["entry"], "sl": r["premium"]["stop"], "t1": r["premium"]["targets"][0]["premium"],
                 "t2": r["premium"]["targets"][1]["premium"], "contract": r["option"]["symbol"]} for r in rows]

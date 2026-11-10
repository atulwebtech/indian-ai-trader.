"""Paper-trade ledger: remembers issued signals and their simulated outcomes. No real orders, ever."""
import json
from pathlib import Path

FINAL = ("STOPPED", "TARGET2_HIT", "EXPIRED")


def result_r(sig: dict, status: str, t1_seen: bool) -> float:
    """Simplified paper result in R (same exit plan as the backtest: half at T1 then breakeven)."""
    rr1, rr2 = sig["targets"][0]["rr"], sig["targets"][1]["rr"]
    if status == "STOPPED":
        return 0.5 * rr1 if t1_seen else -1.0
    if status == "TARGET2_HIT":
        return 0.5 * rr1 + 0.5 * rr2
    return 0.5 * rr1 if t1_seen else 0.0


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.items = []
        if self.path.exists():
            try:
                self.items = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self.items = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, indent=1, default=str))

    def add(self, sig: dict):
        if not any(i["signal"]["id"] == sig["id"] for i in self.items):
            self.items.append({"signal": sig, "status": "ACTIVE", "t1_seen": False, "r": None})
            self._save()

    def active(self, instrument=None):
        return [i for i in self.items if i["status"] not in FINAL and
                (instrument is None or i["signal"]["instrument"] == instrument)]

    def update(self, sig_id: str, status: str):
        """Returns the item if this call changed something worth announcing, else None."""
        for i in self.items:
            if i["signal"]["id"] != sig_id or i["status"] in FINAL:
                continue
            if status == "TARGET1_HIT" and not i["t1_seen"]:
                i["t1_seen"], i["status"] = True, "TARGET1_HIT"
                self._save()
                return i
            if status in FINAL:
                i["status"] = status
                i["r"] = result_r(i["signal"], status, i["t1_seen"])
                self._save()
                return i
        return None

    def for_date(self, day: str):
        return [i for i in self.items if str(i["signal"]["time"])[:10] == day]

"""Continuous paper-signal monitor. One step() per poll: data health + alerts, track open paper signals (premium based),
evaluate each instrument once per NEW closed 1-minute candle, write the dashboard state file. Never places orders.
`runner` is duck-typed (LiveOptionRunner or a fake in tests): .instruments .feed .gate .params .evaluate() .quote()"""
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from config.settings import IST, ROOT
from market_data.live_guard import live_state
from market_data.loader import market_state
from strategies import option_engine as E
from telegram_bot import option_alerts as A

log = logging.getLogger(__name__)
STATE_FILE = ROOT / "data" / "option_state.json"
LOSS_COOLDOWN_MINUTES = 45          # no automatic re-entry on the same instrument after a stop loss
OUTAGE_ALERT_AFTER_SECONDS = 45
CLOSED_REFRESH_SECONDS = 120        # market closed: refresh the last-session analysis at most every 2 minutes


def _event_key(sid: str, result: dict) -> str:
    return f"{sid}:{result['status']}:{'T2OPEN' if 'stop moved to Target 1' in (result.get('event') or '') else 'E'}"


class Monitor:
    def __init__(self, runner, ledger, notifier, state_file=None, clock=None, now_epoch=time.time):
        self.r, self.ledger, self.notifier = runner, ledger, notifier
        self.state_file = Path(state_file) if state_file else STATE_FILE
        self.clock = clock or (lambda: datetime.now(IST))
        self.now_epoch = now_epoch
        self._bar, self._live, self._since, self._outage, self._loss_until, self._closed_eval = {}, {}, {}, {}, {}, {}
        self.latest = {}
        self.errors = 0

    # ---- data health ----
    def _data_state(self, inst, now):
        return live_state("LIVE", self.r.feed.ticks.get(inst), self.r.feed.connected, market_state(now), self.now_epoch())

    def _data_alerts(self, inst, ds, now):
        open_mkt = ds["state"] not in ("MARKET_CLOSED", "NOT_LIVE_SOURCE")
        if ds["live"]:
            if self._outage.get(inst) and self._outage[inst]["alerted"]:
                self.notifier.send(f"{inst}:DATA_RESTORED:{self._outage[inst]['id']}", A.fmt_data("RESTORED", inst, "Feed is LIVE again."), now)
            self._outage.pop(inst, None)
            self._live[inst], self._since[inst] = True, None
            return
        if not open_mkt:
            self._outage.pop(inst, None)
            self._live[inst] = False
            return
        o = self._outage.setdefault(inst, {"id": now.strftime("%H%M%S"), "since": self.now_epoch(), "alerted": False})
        if not o["alerted"] and (self._live.get(inst) or self.now_epoch() - o["since"] >= OUTAGE_ALERT_AFTER_SECONDS):
            if self.notifier.send(f"{inst}:DATA_LOST:{o['id']}", A.fmt_data("LOST", inst, f"{ds['label']}: {ds['reason']}"), now):
                o["alerted"] = True
        self._live[inst] = False

    # ---- open signals ----
    def _track(self, inst, underlying, now):
        for rec in self.ledger.open_signals(inst):
            sig = rec["sig"]
            try:
                q = self.r.quote(rec["option"]["symbol"])
            except Exception as e:
                log.warning("quote failed for %s: %s", rec["option"]["symbol"], type(e).__name__)
                continue
            ltp = q.get("ltp") or ((q["bid"] + q["ask"]) / 2 if q.get("bid") and q.get("ask") else None)
            if ltp is None:
                continue
            res = E.advance_signal(sig, float(ltp), underlying, now, self.r.params)
            exit_px = q.get("bid") or ltp                              # a long option is closed at the bid
            net = self.ledger.apply(rec["id"], res, now, exit_premium=float(exit_px) if res["final"] else None)
            if res.get("event"):
                self.notifier.send(_event_key(rec["id"], res), A.fmt_event(self.ledger.get(rec["id"]), res, ltp, underlying, net), now)
            if res["final"]:
                if net is not None:
                    self.r.gate.record_result(rec["id"], net, now)
                    if res["status"] == "STOP_LOSS" and net < 0:
                        self._loss_until[inst] = now + timedelta(minutes=LOSS_COOLDOWN_MINUTES)

    # ---- one poll ----
    def step(self, now: datetime = None) -> dict:
        now = now or self.clock()
        for inst in self.r.instruments:
            try:
                ds = self._data_state(inst, now)
                self._data_alerts(inst, ds, now)
                if not ds["live"]:
                    if ds["state"] == "MARKET_CLOSED" and self.latest.get(inst) and self.now_epoch() - self._closed_eval.get(inst, 0) < CLOSED_REFRESH_SECONDS:
                        continue                                         # pre-market / after hours: the last-session analysis does not change, spare the API
                    self._closed_eval[inst] = self.now_epoch()
                    new = self.r.evaluate(inst, now)                     # the DATA UNAVAILABLE / market closed decision
                    prev = self.latest.get(inst) or {}
                    if prev.get("sides"):                                # keep the last LIVE analysis on screen, clearly marked as old
                        for k in ("option", "premium", "underlying", "why_trade", "id"):
                            prev.pop(k, None)
                        new = {**prev, **new, "stale_analysis": True, "analysis_time": prev.get("analysis_time") or prev.get("time")}
                    self.latest[inst] = new
                    continue
                self._track(inst, self.r.feed.ticks[inst]["price"], now)
                bar = now.replace(second=0, microsecond=0)
                if self._bar.get(inst) == bar or now.second < 2:        # once per new closed 1m candle (2s grace for the candle)
                    continue
                self._bar[inst] = bar
                if self.ledger.open_signals(inst):
                    self.latest.setdefault(inst, {})["holding"] = True    # one position per instrument; keep managing it
                    continue
                if self._loss_until.get(inst) and now < self._loss_until[inst]:
                    sig = self.r.evaluate(inst, now)
                    sig = {**sig, "action": "NO TRADE", "status": "COOLDOWN_AFTER_LOSS",
                           "reasons": [f"No automatic re-entry for {LOSS_COOLDOWN_MINUTES} min after a stop loss (until {self._loss_until[inst]:%H:%M})"]}
                    self.latest[inst] = sig
                    continue
                sig = self.r.evaluate(inst, now)
                self.latest[inst] = sig
                if sig["action"] in ("BUY CALL", "BUY PUT"):
                    if self.ledger.add(sig):
                        self.notifier.send(f"{sig['id']}:SIGNAL", A.fmt_signal(sig), now)
                elif sig["action"] == "WAIT FOR CONFIRMATION":
                    side = "CALL" if sig["candidate"] == "BUY CALL" else "PUT"
                    trig = sig["sides"][side]["trigger"]
                    self.notifier.send(f"{inst}:{now.date()}:SETUP:{sig['candidate']}:{trig}",
                                       A.fmt_setup(inst, sig["candidate"], sig["wait_for"], sig["sides"][side]["score"]), now)
            except Exception as e:                                          # never silently ignore data errors
                self.errors += 1
                log.exception("monitor step failed for %s", inst)
                self.latest[inst] = {"instrument": inst, "action": "NO TRADE", "status": "ERROR", "reasons": [f"Engine error: {type(e).__name__}: {e}"],
                                     "why_no_trade": [f"Engine error: {type(e).__name__}: {e}"], "time": now.isoformat(timespec="seconds"), "data": {}}
        self.write_state(now)
        return self.latest

    # ---- dashboard state ----
    def write_state(self, now):
        keep = ("instrument", "time", "action", "status", "reasons", "why_no_trade", "why_trade", "wait_for", "candidate", "sides", "levels", "mtf",
                "indicators", "chain", "chain_view", "previous_session", "today_vs_previous", "sessions", "underlying_price", "regime", "data",
                "option", "premium", "underlying", "score", "classification", "pattern", "id", "invalidation", "expiry", "zones", "cpr",
                "entry_valid_until", "expires_at", "volume_source", "oi_change_chain", "mode", "quote", "spark", "change", "stale_analysis", "analysis_time", "regime_detail")
        inst = {k: {x: v.get(x) for x in keep if x in v} for k, v in self.latest.items()}
        open_rows = [{"id": r["id"], "instrument": r["instrument"], "action": r["action"], "status": r["status"], "entered": r["entered"],
                      "contract": r["option"]["symbol"], "entry": r["premium"]["entry"], "sl": r["sig"].get("sl_current", r["premium"]["stop"]),
                      "targets": [t["premium"] for t in r["premium"]["targets"]], "events": r["events"][-3:]} for r in self.ledger.open_signals()]
        state = {"updated": now.isoformat(timespec="seconds"), "updated_epoch": self.now_epoch(), "mode": "PAPER SIGNAL (no real orders)",
                 "feed": self.r.feed.status(), "instruments": inst, "open_signals": open_rows,
                 "metrics_all": self.ledger.metrics(), "metrics_today": self.ledger.metrics(str(now.date())),
                 "history": self.ledger.history(40), "limits": {"capital": self.r.params.capital, "max_risk_rupees": self.r.gate.max_risk_rupees,
                                                                "remaining_daily_loss": self.r.gate.remaining_daily_loss(),
                                                                "trades_today": len(self.r.gate.state.get("signals", [])),
                                                                "max_trades": self.r.params.max_trades_per_day}}
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_file.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, default=str)
        os.replace(tmp, self.state_file)

    def run_forever(self, interval=2.0, stop_after_close=True):
        log.info("option monitor started (paper only)")
        while True:
            t0 = time.time()
            now = self.clock()
            try:
                self.step(now)
            except Exception:                         # step() already isolates per-instrument errors; this is the last guard
                log.exception("monitor loop error")
            if stop_after_close and now.time() > datetime(2000, 1, 1, 15, 35).time() and not self.ledger.open_signals():
                break
            time.sleep(max(0.2, interval - (time.time() - t0)))

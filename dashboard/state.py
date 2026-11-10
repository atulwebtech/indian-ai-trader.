"""Builds the dashboard data (plain dict). No secrets ever go in here."""
import copy
import time
from dataclasses import replace
from datetime import datetime

from backtesting.metrics import summarize
from config.settings import IST, ROOT
from forecasting.daily_forecast import generate_forecast
from market_data.calendar import default_calendar
from market_data.loader import data_status, load_data
from paper_trading.ledger import Ledger
from reversal_detection import detector as rev
from risk_management.gate import SignalGate, params_for
from strategies.signal_engine import evaluate

INSTRUMENTS = ("NIFTY", "BANKNIFTY")
DATA_LABEL = {"MOCK": "MOCK (invented test data)", "HISTORICAL": "HISTORICAL (delayed Yahoo candles)",
              "LIVE": "LIVE (Groww)"}


class DashboardState:
    def __init__(self, settings, mode, loader=load_data, ttl=30, clock=None, state_dir=None, telegram_client=None):
        self.settings, self.mode, self.loader, self.ttl = settings, mode, loader, ttl
        self.clock = clock or (lambda: datetime.now(IST))
        self.dir = state_dir or (ROOT / "data")
        self.telegram_client = telegram_client
        self._cache, self._tg_cache = None, (0, None)

    # ---------- connections ----------
    def telegram_status(self):
        s = self.settings
        if not (s.telegram_bot_token and s.telegram_chat_id):
            return {"state": "NOT CONFIGURED", "detail": "Fill TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"}
        ts, cached = self._tg_cache
        if cached and time.time() - ts < 300:
            return cached
        try:
            client = self.telegram_client
            if client is None:
                from telegram_bot.client import TelegramClient
                client = TelegramClient(s.telegram_bot_token, s.telegram_chat_id, retries=1)
            me = client.get_me()
            out = {"state": "CONNECTED", "detail": f"Bot @{me['username']} (token valid)"}
        except Exception as e:
            out = {"state": "ERROR", "detail": f"{type(e).__name__}: could not reach Telegram"}
        self._tg_cache = (time.time(), out)
        return out

    def groww_status(self):
        s = self.settings
        if not s.groww_api_key:
            return {"state": "NOT CONNECTED", "detail": "Groww API not purchased/configured yet (planned)"}
        if self.mode == "LIVE":
            return {"state": "CONFIGURED", "detail": "Keys present; data mode LIVE"}
        return {"state": "CONFIGURED", "detail": "Keys present but data mode is not LIVE"}

    # ---------- per-instrument ----------
    def _instrument(self, inst, gate, now):
        intra, daily = self.loader(inst, self.mode)
        ds = data_status(intra, now)
        has_today = ds["has_today"]
        before_open = now.time().hour < 9 or (now.time().hour == 9 and now.time().minute < 30)
        f = generate_forecast(inst, intra, daily,
                              session_date=now.date() if (not has_today and before_open and ds["market"] != "HOLIDAY") else None)
        dry = SignalGate(gate.p, calendar=gate.calendar)
        dry.state = copy.deepcopy(gate.state)
        sig = evaluate(inst, intra, daily, dry)
        rv = rev.detect_reversal(intra, rev.levels_from_forecast(f))["primary"]
        day = intra[intra.index.date == intra.index[-1].date()]
        last = intra.tail(60)
        return {
            "name": inst, "price": f["reference_price"], "price_note": f["reference_note"],
            "data_source": DATA_LABEL.get(intra.attrs.get("source"), intra.attrs.get("source")),
            "data_source_key": intra.attrs.get("source"),
            "freshness": {"status": ds["status"], "market": ds["market"], "last_candle_end": str(ds["last_candle_end"])[:16],
                          "age_minutes": ds["age_minutes"]},
            "bias": f["bias"], "support": f["support"], "resistance": f["resistance"],
            "scenarios": {k: {"role": v["role"], "trigger": v["trigger"], "invalidation": v["invalidation"],
                              "targets": v.get("targets", []), "range": v.get("range")} for k, v in f["scenarios"].items()},
            "reversal_zones": f["reversal_zones"],
            "reversal_now": {"state": rv["state"], "direction": rv["direction"], "still_needed": rv["still_needed"][:4]},
            "opening_range": f["opening_range_analysis"], "volatility": f["volatility"],
            "volume_available": f["volume"]["available"],
            "signal_now": {"action": sig["action"], "reasons": sig.get("reasons", [])[:3],
                           "entry_zone": sig.get("entry_zone"), "stop": sig.get("stop"),
                           "targets": [t["level"] for t in sig.get("targets", [])], "score": sig.get("quality_score")},
            "chart": {"t": [str(x)[11:16] for x in last.index], "close": [round(float(x), 2) for x in last["close"]]},
            "day": {"high": float(day.high.max()), "low": float(day.low.min())},
            "forecast_as_of": f["data_as_of"][:16],
        }

    def _paper(self, now):
        items = Ledger(self.dir / "paper_ledger.json").items
        done = [i["r"] for i in items if i["r"] is not None]
        hist = [{"time": i["signal"]["time"][:16], "instrument": i["signal"]["instrument"], "action": i["signal"]["action"],
                 "setup": i["signal"]["setup"], "entry": i["signal"]["entry_ref"], "stop": i["signal"]["stop"],
                 "status": i["status"], "r": i["r"]} for i in reversed(items[-30:])]
        today = [h for h in hist if h["time"][:10] == str(now.date())]
        return {"summary": summarize(done) if done else {"trades": 0}, "history": hist, "today": today}

    def build(self):
        now = self.clock()
        if self._cache and time.time() - self._cache[0] < self.ttl:
            return self._cache[1]
        gate = SignalGate(params_for(self.settings.signal_profile), path=self.dir / "signal_state.json")
        data = {"generated_at": now.strftime("%d %b %Y %H:%M:%S IST"), "mode_label": replace(self.settings, data_mode=self.mode).mode_label,
                "data_mode": self.mode, "orders": "DISABLED - this system never places orders",
                "market": {"today": str(now.date()), "type": default_calendar().day_type(now.date()),
                           "holiday": default_calendar().holiday_name(now.date()),
                           "calendar_verified": default_calendar().verified(now.date())},
                "telegram": self.telegram_status(), "groww": self.groww_status(), "instruments": [], "errors": [],
                "backtest_warning": "Backtests (2024-2026) found NO trading edge in the bias or signal engine. "
                                    "Targets are rough range estimates; signals are experiments only.",
                "paper": self._paper(now)}
        for inst in INSTRUMENTS:
            try:
                data["instruments"].append(self._instrument(inst, gate, now))
            except Exception as e:
                data["errors"].append(f"{inst}: {type(e).__name__}: {str(e)[:150]}")
        self._cache = (time.time(), data)
        return data

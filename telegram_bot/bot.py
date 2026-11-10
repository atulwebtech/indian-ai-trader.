"""Telegram bot: commands + scheduled/event alerts. Paper mode only; never places orders."""
import copy
import json
import logging
import signal as os_signal
import time
from pathlib import Path
from datetime import datetime, time as dtime, timedelta

import pandas as pd

from config.settings import IST, ROOT
from datetime import timezone
from forecasting.daily_forecast import generate_forecast
from news import context as news_context, feeds as news_feeds, impact as news_impact
from forecasting.explain import explain
from market_data.calendar import default_calendar
from market_data.loader import data_status, load_data, market_state
from paper_trading.ledger import FINAL, Ledger
from paper_trading.scorecard import ScoreStore, backtest_reference, cumulative, day_scorecard, render_eod
from reversal_detection import detector as rev
from risk_management.gate import SignalGate, params_for
from strategies.signal_engine import evaluate, update_signal
from telegram_bot import hindi as H
from telegram_bot import check as idea_check
from telegram_bot import messages as M

log = logging.getLogger(__name__)
INSTRUMENTS = ("NIFTY", "BANKNIFTY")

JOBS = [  # name, time, minutes the job may still run after its time
    ("premarket", dtime(9, 0), 30), ("context", dtime(9, 5), 25), ("opening", dtime(9, 35), 30), ("plan_open", dtime(9, 40), 30), ("plan_mid", dtime(11, 30), 40), ("plan_pm", dtime(13, 30), 40), ("summary", dtime(15, 20), 40),
    ("eod", dtime(15, 50), 70)]
SCAN_SLOTS = [(datetime(2000, 1, 1, 9, 47) + timedelta(minutes=15 * k)).time() for k in range(0, 21)
              if (datetime(2000, 1, 1, 9, 47) + timedelta(minutes=15 * k)).time() <= dtime(15, 17)]


NEWS_SLOTS = [(datetime(2000, 1, 1, 8, 52) + timedelta(minutes=5 * k)).time() for k in range(0, 80)
              if (datetime(2000, 1, 1, 8, 52) + timedelta(minutes=5 * k)).time() <= dtime(15, 32)]
NEWS_ALERT_AGE_MIN = 90        # only headlines this fresh can trigger an alert
NEWS_MIN_GAP_MIN = 20          # at most one news alert per 20 minutes
NEWS_MAX_PER_DAY = 6
NEWS_TOPIC_COOLDOWN_H = 3       # the same story (same cause words) is not alerted again for 3 hours


class AlertLog:
    """Remembers which alerts were already sent today so nothing is sent twice (survives restarts)."""
    def __init__(self, path=None):
        self.path, self.data = (Path(path) if path else None), {"date": None, "keys": []}
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                pass

    def _roll(self, day):
        if self.data.get("date") != day:
            self.data = {"date": day, "keys": []}

    def seen(self, day, key):
        self._roll(day)
        return key in self.data["keys"]

    def mark(self, day, key):
        self._roll(day)
        if key not in self.data["keys"]:
            self.data["keys"].append(key)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.data))


class Bot:
    def __init__(self, client, settings, mode="HISTORICAL", loader=load_data, state_dir=None, clock=None):
        d = state_dir or (ROOT / "data")
        self.client, self.settings, self.mode, self.loader = client, settings, mode, loader
        self.gate = SignalGate(params_for(settings.signal_profile), path=d / "signal_state.json")
        self.alerts = AlertLog(d / "alert_state.json")
        self.ledger = Ledger(d / "paper_ledger.json")
        self.calendar = default_calendar()
        self.clock = clock or (lambda: datetime.now(IST))
        self.stop = False
        self.offset = None
        self.state_dir = d
        self.lang = self._load_lang(getattr(settings, "telegram_lang", "hi"))
        self._retry_at = {}
        self.news_fetcher = news_feeds.fetch_all
        self.context_fetcher = news_context.fetch_context
        self.last_news_alert = None
        self.topic_times = {}

    # ---------------- language ----------------
    def _load_lang(self, default):
        try:
            v = json.loads((self.state_dir / "prefs.json").read_text()).get("lang")
            if v in M.LANGS:
                return v
        except (OSError, ValueError):
            pass
        return default if default in M.LANGS else "hi"

    def set_lang(self, lang):
        self.lang = lang
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "prefs.json").write_text(json.dumps({"lang": lang}))

    def tt(self, en, hi):
        """Pick the English or Hinglish wording for a fixed sentence."""
        return hi if self.lang == "hi" else en

    # ---------------- helpers ----------------
    def _data(self, inst):
        intra, daily = self.loader(inst, self.mode)
        return intra, daily, data_status(intra, self.clock())

    def _forecast(self, inst, intra, daily, now):
        has_today = (intra.index.date == now.date()).any()
        before_open = now.weekday() < 5 and now.time() < dtime(9, 30) and not has_today
        return generate_forecast(inst, intra, daily, session_date=now.date() if before_open else None)

    def _extra_ids(self):
        return [c for c in getattr(self.settings, "telegram_extra_chat_ids", ()) if c != self.client.chat_id]

    def _broadcast(self, text):
        """Your own chat first (a failure there is an error); the extra people are best-effort."""
        self.client.send(text)
        for cid in self._extra_ids():
            try:
                self.client.send(text, chat_id=cid)
            except Exception as e:
                log.warning("could not send to an extra chat: %s", type(e).__name__)

    def _send(self, text, key=None):
        now = self.clock()
        if key and self.alerts.seen(str(now.date()), key):
            return False
        self._broadcast(text)
        if key:
            self.alerts.mark(str(now.date()), key)
        return True

    # ---------------- commands ----------------
    def _check_idea(self, text, now):
        idea = idea_check.parse(text)
        if not idea:
            return self.tt("Send your idea like: /check nifty call 23300 sl 23270 target 23360 (entry, sl and target are optional).",
                           "Apna idea aise likhein: /check nifty call 23300 sl 23270 target 23360 (entry, sl aur target likhna zaroori nahi).")
        intra, daily, ds = self._data(idea["instrument"])
        f = self._forecast(idea["instrument"], intra, daily, now)
        head = f"[{self.settings.mode_label}]\n"
        stale = "" if ds["status"] == "FRESH" else "\n" + self.tt(f"Note: data is {ds['status']} (newest candle ended {ds['last_candle_end']:%H:%M}).",
                                                                 f"Dhyan: data {H.DATA_STATE.get(ds['status'], ds['status'])} hai (sabse nayi candle {ds['last_candle_end']:%H:%M} par band hui).")
        return head + idea_check.run(idea, f, intra, now, self.lang) + stale

    def handle_command(self, text: str) -> str:
        cmd = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
        now = self.clock()
        try:
            if cmd == "/check" or (not cmd.startswith("/") and idea_check.parse(text)):
                return self._check_idea(text, now)
            if cmd in ("/start", "/help"):
                return (self.tt(f"Indian AI Trader ({self.settings.mode_label}). I never place orders.",
                                f"Indian AI Trader ({self.settings.mode_label}). Main kabhi order nahi lagata.") + "\n\n" + M.help_text(self.lang))
            if cmd == "/lang":
                arg = (text.lower().split() + [""])[1]
                if arg not in M.LANGS:
                    return self.tt("Use /lang hi (Hinglish) or /lang en (English).", "Aise likhein: /lang hi (Hinglish) ya /lang en (English).")
                self.set_lang(arg)
                return "Ab se saare messages Hinglish (Hindi) mein aayenge." if arg == "hi" else "From now on all messages will be in English."
            if cmd == "/status":
                rows = []
                for inst in INSTRUMENTS:
                    intra, _, ds = self._data(inst)
                    rows.append((inst, ds, float(intra.close.iloc[-1])))
                return M.status_message(self.settings, rows, now, self.lang)
            if cmd in ("/forecast", "/nifty", "/banknifty"):
                insts = INSTRUMENTS if cmd == "/forecast" else (("NIFTY",) if cmd == "/nifty" else ("BANKNIFTY",))
                out = []
                for inst in insts:
                    intra, daily, ds = self._data(inst)
                    out.append(M.forecast_message(self._forecast(inst, intra, daily, now), ds, now, lang=self.lang))
                return "\n\n".join(out)
            if cmd == "/plan":
                return self._plan_text(now)
            if cmd == "/news":
                heads, errs = self.news_fetcher(now=now.astimezone(timezone.utc), max_age_hours=12)
                rel = [(h, news_impact.classify(h.title)) for h in heads]
                rel = [x for x in rel if x[1]["severity"] != "LOW"][:10]
                if not rel:
                    return self.tt("No market-level headlines in the last 12 hours.", "Pichle 12 ghante mein market-level headlines nahi mili.") + (
                        f" ({self.tt('feeds unavailable', 'feeds nahi chali')}: {', '.join(errs[:3])})" if errs else "")
                dl = (lambda x: H.DIRECTION.get(x, x)) if self.lang == "hi" else (lambda x: x)
                return "\n".join([f"[{self.settings.mode_label}]", self.tt(f"MARKET HEADLINES | {now:%d %b %H:%M} IST (keyword-read, may be wrong)",
                                                                           f"MARKET HEADLINES | {now:%d %b %H:%M} IST (keywords se padhi, galat ho sakti hain)"), ""] +
                                  [f"[{c['severity']}/{dl(c['direction'])}] {h.title} ({h.source}, {M._ago(h, now.astimezone(timezone.utc), self.lang)})" for h, c in rel] +
                                  ["", M.news_caveat(self.lang)])
            if cmd == "/context":
                return self._context_text(now)
            if cmd == "/explain":
                args = text.lower().split()[1:]
                lang = "hi" if "hi" in args else "en" if "en" in args else self.lang
                out = []
                for inst in INSTRUMENTS:
                    intra, daily, ds = self._data(inst)
                    f = self._forecast(inst, intra, daily, now)
                    dry = SignalGate(self.gate.p, calendar=self.gate.calendar)
                    dry.state = copy.deepcopy(self.gate.state)
                    out.append(f"[{M.label(f['source'])}]\n" + explain(f, rev.detect_reversal(intra, rev.levels_from_forecast(f)),
                                                                       evaluate(inst, intra, daily, dry), lang, ds))
                return "\n\n".join(out)
            if cmd == "/signals":
                return self._signals_text(now)
            if cmd == "/report":
                return self._eod_report(now)[0]
            return self.tt("Unknown command. Send /help.", "Yeh command samajh nahi aaya. /help bhejein.")
        except Exception as e:  # keep the bot alive; tell the user plainly
            log.exception("command %s failed", cmd)
            return self.tt(f"Sorry, {cmd} failed: {type(e).__name__}: {str(e)[:200]}. Data may be unavailable; try again shortly.",
                           f"Maaf kijiye, {cmd} nahi chala: {type(e).__name__}: {str(e)[:200]}. Data uplabdh nahi ho sakta; thodi der baad dobara koshish karein.")

    def _context_text(self, now):
        now_utc = now.astimezone(timezone.utc)
        rows, cerr = self.context_fetcher()
        heads, herr = self.news_fetcher(now=now_utc, max_age_hours=12)
        top = [(h, c) for h, c in ((h, news_impact.classify(h.title)) for h in heads) if c["severity"] != "LOW"][:5]
        return M.context_message(rows, news_context.read_context(rows), top, now, now_utc, self.settings.mode_label, cerr + herr, self.lang)

    def _eod_report(self, now):
        """(text, complete). Full scorecard for today; only complete days are added to the running totals."""
        day = now.date()
        cards, notes, complete_all = [], [], True
        for inst in INSTRUMENTS:
            try:
                intra, daily, ds = self._data(inst)
                if not ds["has_today"]:
                    notes.append(f"{inst}: no candles for today yet (holiday or data feed delay)")
                    continue
                self._track_active(inst, intra, now)
                sc = day_scorecard(inst, intra, daily, day, self.ledger.items)
                if sc["complete"]:           # the session is over: settle anything still open, then re-score
                    for item in list(self.ledger.active(inst)):
                        if str(item["signal"]["time"])[:10] == str(day):
                            done = self.ledger.update(item["signal"]["id"], "EXPIRED")
                            if done:
                                self.gate.set_status(item["signal"]["id"], "EXPIRED")
                                self.gate.record_result(inst, done["r"], now)
                    sc = day_scorecard(inst, intra, daily, day, self.ledger.items)
                complete_all &= sc["complete"]
                cards.append(sc)
            except Exception as e:
                log.warning("eod %s failed: %s", inst, e)
                notes.append(f"{inst}: report unavailable ({type(e).__name__})")
                complete_all = False
        if not cards:
            return (f"[{self.settings.mode_label}]\n" + self.tt("END-OF-DAY REPORT", "DIN KI REPORT") + f" | {now:%d %b %Y %H:%M} IST\n" + "\n".join(notes or [self.tt("No data for today.", "Aaj ka data nahi mila.")]),
                    not notes or all("no candles" in n for n in notes))
        store = ScoreStore(self.state_dir / "scorecard.json")
        for sc in cards:
            store.add(sc)
        cum = cumulative(store.records())
        refs = {i: backtest_reference(i) for i in INSTRUMENTS}
        note = "" if complete_all else (self.tt("Day not finished yet: figures so far (last candle ", "Din abhi khatam nahi hua: ab tak ke aankde (aakhri candle ") + ", ".join(
            f"{c['instrument']} {c['last_candle_end']}" for c in cards) + self.tt("). Not added to the running totals.", "). Jod ke chalne wale totals mein nahi joda gaya."))
        text = render_eod(cards, cum, refs, self.settings.mode_label, f"{now:%d %b %Y %H:%M} IST", note, self.lang)
        if notes:
            text += "\n" + "\n".join(notes)
        return text, complete_all

    def _signals_text(self, now):
        today = str(now.date())
        hi = self.lang == "hi"
        lines = [f"[{self.settings.mode_label}]", f"SIGNALS | {now:%d %b %Y %H:%M} IST"]
        items = self.ledger.for_date(today)
        if items:
            for i in items:
                s = i["signal"]
                word = (("CALL (CE)" if s["action"] == "BUY" else "PUT (PE)") if hi else s["action"])
                lines.append(f"- {s['time'][11:16]} {s['instrument']} {word} {s['setup']} entry~{s['entry_ref']:.0f} "
                             f"stop {s['stop']:.0f}: {H.STATUS.get(i['status'].replace('_', ' '), i['status'].replace('_', ' ')) if hi else i['status'].replace('_', ' ')}"
                             + (f" ({i['r']:+.2f}R)" if i["r"] is not None else ""))
        else:
            lines.append(self.tt("No signals issued today.", "Aaj abhi tak koi signal nahi aaya."))
        lines.append("")
        for inst in INSTRUMENTS:
            intra, daily, ds = self._data(inst)
            dry = SignalGate(self.gate.p)
            dry.state = copy.deepcopy(self.gate.state)
            s = evaluate(inst, intra, daily, dry)
            lines.append(f"{inst} " + self.tt("now", "abhi") + f": {s['action']} - {'; '.join(M._x(self.lang, r) for r in s.get('reasons', [])[:2])}"
                         + (self.tt(f" [data {ds['status']}]", " [data purana]") if ds["status"] == "STALE" else ""))
        lines += ["", M.not_advice(self.lang)]
        return "\n".join(lines)

    def _summary_text(self, now, title=None):
        today = now.date()
        title = title or self.tt("DAILY SUMMARY", "DIN KA CHHOTA SUMMARY")
        lines = [f"[{self.settings.mode_label}]", f"{title} | {now:%d %b %Y %H:%M} IST"]
        for inst in INSTRUMENTS:
            intra, daily, ds = self._data(inst)
            day = intra[intra.index.date == today]
            if day.empty:
                lines.append(f"{inst}: " + self.tt("no candles for today (holiday or data not available yet)", "aaj ki candles nahi (chhutti ya data abhi nahi aaya)"))
                continue
            o, h, l, c = day.open.iloc[0], day.high.max(), day.low.min(), day.close.iloc[-1]
            pre = intra[intra.index.date < today]
            pre.attrs.update(intra.attrs)
            line = f"{inst}: open {o:.0f} high {h:.0f} low {l:.0f} close {c:.0f} ({100 * (c - o) / o:+.2f}% " + self.tt("from open", "open se") + ")"
            try:
                f = generate_forecast(inst, pre, daily, session_date=today)
                up = [t for t in f["scenarios"]["bullish"]["targets"] if h >= t["level"]]
                dn = [t for t in f["scenarios"]["bearish"]["targets"] if l <= t["level"]]
                none = self.tt("none", "koi nahi")
                line += self.tt(f"\n   Pre-market bias was {f['bias']['bias']}. Upside zones reached: "
                                f"{', '.join(t['label'] for t in up) or none}; downside: {', '.join(t['label'] for t in dn) or none}",
                                f"\n   Subah ka bias {H.BIAS[f['bias']['bias']]} tha. Upar ke zones jo mile: "
                                f"{', '.join(t['label'] for t in up) or none}; neeche ke: {', '.join(t['label'] for t in dn) or none}")
            except ValueError:
                pass
            lines.append(line)
        items = self.ledger.for_date(str(today))
        if items:
            rs = [i["r"] for i in items if i["r"] is not None]
            lines.append(self.tt(f"Paper signals today: {len(items)}; closed {len(rs)}, net {sum(rs):+.2f}R (before costs)",
                                 f"Aaj ke paper signals: {len(items)}; band {len(rs)}, net {sum(rs):+.2f}R (kharch se pehle)"))
        else:
            lines.append(self.tt("Paper signals today: none", "Aaj ke paper signals: koi nahi"))
        lines += ["", M.not_advice(self.lang)]
        return "\n".join(lines)

    # ---------------- scheduled work ----------------
    def due_jobs(self, now: datetime) -> list:
        """Names of jobs that should run now and have not yet been done. Weekdays only."""
        if not self.calendar.is_trading_day(now.date()):
            return []
        day, due = str(now.date()), []
        trades_only = self.settings.trades_only
        for name, t, window in ([] if trades_only else JOBS):
            start = datetime.combine(now.date(), t, tzinfo=IST)
            if (start <= now < start + timedelta(minutes=window) and not self.alerts.seen(day, f"job:{name}")
                    and self._retry_at.get(name, now) <= now):
                due.append(name)
        for t in ([] if trades_only else NEWS_SLOTS):
            start = datetime.combine(now.date(), t, tzinfo=IST)
            if start <= now < start + timedelta(minutes=4) and not self.alerts.seen(day, f"job:news:{t:%H%M}"):
                due.append(f"news:{t:%H%M}")
        for t in SCAN_SLOTS:
            start = datetime.combine(now.date(), t, tzinfo=IST)
            if start <= now < start + timedelta(minutes=10) and not self.alerts.seen(day, f"job:scan:{t:%H%M}"):
                due.append(f"scan:{t:%H%M}")
        return due

    def _plan_text(self, now):
        out = []
        for inst in INSTRUMENTS:
            intra, daily, ds = self._data(inst)
            out.append(M.plan_message(self._forecast(inst, intra, daily, now), ds, now, self.lang))
        return "\n\n".join(out)

    def _send_plans(self, now):
        for inst in INSTRUMENTS:
            intra, daily, ds = self._data(inst)
            if not ds["has_today"]:
                continue
            self._send(M.plan_message(self._forecast(inst, intra, daily, now), ds, now, self.lang))

    def run_job(self, name: str):
        now, day = self.clock(), str(self.clock().date())
        try:
            if name == "premarket":
                for inst in INSTRUMENTS:
                    intra, daily, ds = self._data(inst)
                    self._send(M.forecast_message(self._forecast(inst, intra, daily, now), ds, now, title="PRE-MARKET FORECAST", lang=self.lang))
            elif name == "opening":
                sent = False
                for inst in INSTRUMENTS:
                    intra, daily, ds = self._data(inst)
                    if not ds["has_today"]:
                        continue
                    f = self._forecast(inst, intra, daily, now)
                    if f["opening_range_analysis"]["status"] != "COMPLETE":
                        continue
                    sig = {"action": "WAIT", "reasons": ["Scanning every 15 minutes; alerts only for confirmed setups"]}
                    self._send(M.forecast_message(f, ds, now, signal=sig, title="OPENING RANGE UPDATE", lang=self.lang))
                    sent = True
                if not sent and now.time() < dtime(9, 55):
                    self._retry_at[name] = now + timedelta(minutes=2)   # data not there yet: try again shortly, not every second
                    return
                if not sent:
                    self._send(f"[{self.settings.mode_label}]\n" + self.tt("No market data for today yet (holiday or data feed delay). Opening update skipped. ",
                                                                         "Aaj ka market data abhi nahi aaya (chhutti ya data feed mein deri). Opening update chhod diya gaya. ") + M.not_advice(self.lang))
            elif name.startswith("plan_"):
                self._send_plans(now)
            elif name == "context":
                self._send(self._context_text(now))
            elif name.startswith("news:"):
                self.news_scan(now)
            elif name == "eod":
                text, complete = self._eod_report(now)
                if not complete and now.time() < dtime(16, 40):
                    self._retry_at[name] = now + timedelta(minutes=5)    # last candle not published yet
                    return
                self._send(text)
            elif name == "summary":
                self._send(self._summary_text(now))
            elif name.startswith("scan:"):
                self.scan(now)
        except Exception:
            log.exception("job %s failed", name)
            return
        self.alerts.mark(day, f"job:{name}")

    def news_scan(self, now):
        """Alert on NEW, fresh, market-level HIGH-severity headlines. Never creates a trade signal."""
        day, now_utc = str(now.date()), now.astimezone(timezone.utc)
        heads, errs = self.news_fetcher(now=now_utc, max_age_hours=NEWS_ALERT_AGE_MIN / 60)
        fresh = []
        for h in heads:
            c = news_impact.classify(h.title)
            if c["severity"] == "HIGH" and not self.alerts.seen(day, f"newsitem:{h.id}"):
                fresh.append((h, c))
        fresh = news_impact.qualifies(fresh)
        fresh = [(h, c) for h, c in fresh if not c.get("topics") or not all(
            self.topic_times.get(t) and (now - self.topic_times[t]).total_seconds() < NEWS_TOPIC_COOLDOWN_H * 3600 for t in c["topics"])]
        if not fresh:
            return
        sent_today = sum(1 for k in self.alerts.data.get("keys", []) if k.startswith("newsalert:"))
        if sent_today >= NEWS_MAX_PER_DAY:
            return
        if self.last_news_alert and (now - self.last_news_alert).total_seconds() < NEWS_MIN_GAP_MIN * 60:
            return                      # try again next slot; nothing is marked as sent
        fresh = fresh[:3]
        dirs = [c["direction"] for _, c in fresh]
        d = "NEGATIVE" if dirs.count("NEGATIVE") > dirs.count("POSITIVE") else "POSITIVE" if dirs.count("POSITIVE") > dirs.count("NEGATIVE") else "UNCLEAR"
        reactions, forecasts = {}, {}
        for inst in INSTRUMENTS:
            try:
                intra, daily, ds = self._data(inst)
                forecasts[inst] = self._forecast(inst, intra, daily, now)
                reactions[inst] = news_impact.price_reaction(intra, d) if ds["has_today"] and ds["status"] == "FRESH" else {"known": False}
            except Exception as e:
                log.warning("news price context for %s failed: %s", inst, e)
                reactions[inst] = {"known": False}
        self.client.send(M.news_alert_message(fresh, reactions, forecasts, now, now_utc, self.settings.mode_label, self.lang))
        self.last_news_alert = now
        self.alerts.mark(day, f"newsalert:{now:%H%M}")
        for _, c in fresh:
            for t in c.get("topics", ()):
                self.topic_times[t] = now
        for h, _ in fresh:
            self.alerts.mark(day, f"newsitem:{h.id}")

    def scan(self, now):
        day = str(now.date())
        for inst in INSTRUMENTS:
            try:
                intra, daily, ds = self._data(inst)
            except Exception as e:
                log.warning("scan %s data error: %s", inst, e)
                self._send(f"[{self.settings.mode_label}]\n" + self.tt(f"{inst}: market data unavailable ({type(e).__name__}). No alerts can be produced until it returns.",
                                                                     f"{inst}: market data nahi mil raha ({type(e).__name__}). Data wapas aane tak alerts nahi aa sakte."),
                           key=f"datafail:{inst}:{now:%H}")
                continue
            if ds["status"] != "FRESH" or not ds["has_today"]:
                self._send(f"[{self.settings.mode_label}]\n" + self.tt(f"{inst}: DATA STALE (newest candle ended {ds['last_candle_end']:%H:%M}). Alerts paused until fresh data arrives.",
                                                                     f"{inst}: DATA PURANA (sabse nayi candle {ds['last_candle_end']:%H:%M} par band hui). Naya data aane tak alerts ruke hain."),
                           key=f"stale:{inst}:{now:%H}")
                continue
            self._track_active(inst, intra, now)
            sig = evaluate(inst, intra, daily, self.gate)
            f = self._forecast(inst, intra, daily, now)
            if sig["action"] in ("BUY", "SELL"):
                self.ledger.add(sig)
                self._send(M.compact_signal_message(sig, now, self.lang) if self.settings.trades_only else M.signal_message(sig, f, ds, now, self.lang),
                           key=f"signal:{sig['id']}")
            r = rev.detect_reversal(intra, rev.levels_from_forecast(f))
            p = r["primary"]
            if p["state"] == rev.CONFIRMED and p["level"]:
                self._send(M.reversal_message(inst, r, f, ds, now, self.lang), key=f"rev:{inst}:{p['direction']}:{p['level']['price']:.0f}")
            close = float(intra.close.iloc[-1])
            for side, s in (("BULLISH", f["scenarios"]["bullish"]), ("BEARISH", f["scenarios"]["bearish"])):
                if f["bias"]["bias"] == side or s["role"] == "PRIMARY":
                    lvl = s["invalidation_level"]
                    broken = close < lvl if side == "BULLISH" else close > lvl
                    if broken:
                        self._send(M.invalidation_message(inst, side, lvl, f, ds, now, self.lang), key=f"inval:{inst}:{side}")

    def _flip_text(self, inst, sig, now):
        """After a stop-out, the opposite trade: the stop level becomes the entry of the other side."""
        try:
            intra, daily, ds = self._data(inst)
            f = self._forecast(inst, intra, daily, now)
            lines = M.flip_block(f, sig["direction"] == "BUY", sig["stop"], float(intra.close.iloc[-1]), self.lang)
            return "\n".join(lines + ["", M.not_advice(self.lang)])
        except Exception:
            log.exception("flip text failed")
            return M.not_advice(self.lang)

    def _track_active(self, inst, intra, now):
        for item in self.ledger.active(inst):
            sig = item["signal"]
            after = intra[intra.index >= pd.Timestamp(sig["time"])]
            status = update_signal(sig, after)
            if status == "ACTIVE":
                continue
            changed = self.ledger.update(sig["id"], status)
            if changed:
                text = M.status_update_message(sig, changed["status"], now, self.lang)
                if changed["status"] == "STOPPED":
                    text = text.replace("\n\n" + M.not_advice(self.lang), "") + "\n\n" + self._flip_text(inst, sig, now)
                self._send(text, key=f"upd:{sig['id']}:{changed['status']}")
                if changed["status"] in FINAL:
                    self.gate.set_status(sig["id"], changed["status"])
                    self.gate.record_result(inst, changed["r"], now)

    # ---------------- main loop ----------------
    def process_updates(self, timeout=5):
        for u in self.client.get_updates(self.offset, timeout=timeout):
            self.offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat = str(msg.get("chat", {}).get("id", ""))
            if msg.get("text", "").strip().lower().split("@")[0] == "/myid":
                # works for anybody and reveals only the asker's own number, so a new person can be added to .env
                self.client.send(f"Aapka chat id / Your chat id: {chat}\n"
                                 "Ye number bot chalane wale ko dein (chat mein public na likhein). Isse alerts nahi milenge jab tak woh ise add na kare.",
                                 chat_id=chat)
                continue
            if chat != self.client.chat_id and chat not in self._extra_ids():
                log.warning("Ignored message from unauthorised chat")
                continue
            txt = msg.get("text", "")
            if txt.startswith("/") or idea_check.parse(txt):
                self.client.send(self.handle_command(txt), chat_id=chat)         # the answer goes to whoever asked

    def run_forever(self):
        os_signal.signal(os_signal.SIGINT, lambda *a: setattr(self, "stop", True))
        os_signal.signal(os_signal.SIGTERM, lambda *a: setattr(self, "stop", True))
        self._broadcast(f"[{self.settings.mode_label}]\n" + self.tt(f"Bot started {self.clock():%d %b %Y %H:%M} IST. Send /help. I never place orders.",
                                                                    f"Bot shuru hua {self.clock():%d %b %Y %H:%M} IST. /help bhejein. Main kabhi order nahi lagata."))
        while not self.stop:
            try:
                self.process_updates()
                for name in self.due_jobs(self.clock()):
                    self.run_job(name)
            except Exception as e:
                log.warning("loop error: %s", e)
                time.sleep(10)
        try:
            self.client.send("Bot stopped.")
        except Exception:
            pass

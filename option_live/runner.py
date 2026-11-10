"""Live evaluation of the option engine with Groww data (read-only). One REST history load (1m index + futures volume),
live WebSocket ticks for the freshness guard, the option chain and a quote for each contract that is evaluated.
Every decision is appended to data/option_decisions.jsonl with the reasons. Never places orders."""
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from config.settings import IST, ROOT
from groww_integration.historical import parse_candles
from groww_integration.instruments import load_master, nearest_future, validate_indices
from groww_integration.live_feed import LiveIndexFeed
from indicators.market_levels import cpr_and_pivots, repeated_zones, session_summaries
from market_data.live_guard import live_state
from market_data.loader import market_state
from option_chain.chain import SnapshotStore, UNAVAILABLE, analyze_chain, contract_quote_info, parse_chain
from risk_management.option_risk import OptionRiskGate, option_params_from_env
from strategies import option_engine as E
from strategies.prev_day import analyze_previous_day, compare_with_today

log = logging.getLogger(__name__)
MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
DECISIONS = ROOT / "data" / "option_decisions.jsonl"


def resample_closed(df1: pd.DataFrame, tf: str, now: datetime) -> pd.DataFrame:
    """Candles of `tf` built from 1m bars aligned to 09:15, containing ONLY bars that had closed at `now`."""
    m = MINUTES[tf]
    d = df1[df1.index + pd.Timedelta(minutes=1) <= now]            # 1m bars that are complete
    if tf != "1m":
        d = d.resample(f"{m}min", origin="start_day", offset="9h15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    return d[d.index + pd.Timedelta(minutes=m) <= now]


class LiveOptionRunner:
    def __init__(self, session, instruments=("NIFTY", "BANKNIFTY"), params=None, gate=None, snapshots=None):
        self.session = session
        self.raw = session.client.unwrap_for_feed()           # raw client: used ONLY for the read-only data calls below
        self.instruments = tuple(instruments)
        self.params = params or option_params_from_env()
        self.gate = gate or OptionRiskGate(self.params)
        self.snapshots = snapshots or SnapshotStore()
        self.master = load_master(session.client)
        self.indices = validate_indices(self.master)
        self.feed = LiveIndexFeed(self.raw, self.instruments, market_open=lambda: True)
        self.history, self._quote_cache = {}, {}

    # ---- data ----
    def start_feed(self, wait_seconds: float = 15.0):
        self.feed.start()
        t0 = time.time()
        while time.time() - t0 < wait_seconds and not all(i in self.feed.ticks for i in self.instruments):
            time.sleep(0.3)

    def _candles(self, symbol, segment, start, end):
        g = self.raw
        seg = g.SEGMENT_CASH if segment == "CASH" else g.SEGMENT_FNO
        payload = self.session.call("get_historical_candles", exchange=g.EXCHANGE_NSE, segment=seg, groww_symbol=symbol,
                                    start_time=start.strftime("%Y-%m-%d %H:%M:%S"), end_time=end.strftime("%Y-%m-%d %H:%M:%S"),
                                    candle_interval=g.CANDLE_INTERVAL_MIN_1)
        return parse_candles(payload)

    def load_history(self, instrument: str, now: datetime, calendar_days: int = 29) -> pd.DataFrame:
        """1m index candles for ~20 sessions with futures volume merged in. 2 REST calls (documented limit: 30 days for 1m)."""
        end = now.replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
        start = (end - timedelta(days=calendar_days)).replace(hour=9, minute=15)
        idx = self._candles(self.indices[instrument]["groww_symbol"], "CASH", start, end).drop(columns=["oi"])
        fut = nearest_future(self.master, instrument, now.date())
        vol_note = "none"
        if fut:
            f = self._candles(fut["groww_symbol"], "FNO", start, end)
            idx["volume"] = f["volume"].reindex(idx.index).fillna(0)
            vol_note = f"futures {fut['trading_symbol']}"
        idx.attrs["volume_source"] = vol_note
        self.history[instrument] = idx
        return idx

    def expiry(self, instrument: str, now: datetime) -> str:
        r = self.raw.get_expiries(self.raw.EXCHANGE_NSE, instrument, now.year, now.month)
        ex = sorted(e for e in r.get("expiries", []) if e >= now.strftime("%Y-%m-%d"))
        if not ex:
            r = self.raw.get_expiries(self.raw.EXCHANGE_NSE, instrument, (now + timedelta(days=31)).year, (now + timedelta(days=31)).month)
            ex = sorted(e for e in r.get("expiries", []) if e >= now.strftime("%Y-%m-%d"))
        if not ex:
            raise RuntimeError(f"No upcoming expiry returned for {instrument}")
        return ex[0]

    def quote(self, symbol: str):
        c = self._quote_cache.get(symbol)
        if c and time.time() - c[0] < 3:                      # at most one quote call per contract per 3 seconds
            return c[1]
        q = contract_quote_info(self.raw.get_quote(trading_symbol=symbol, exchange="NSE", segment=self.raw.SEGMENT_FNO))
        self._quote_cache[symbol] = (time.time(), q)
        return q

    def lot_size(self, symbol: str):
        row = self.master[self.master["trading_symbol"] == symbol]
        if row.empty or pd.isna(row.iloc[0]["lot_size"]):
            return None
        return int(row.iloc[0]["lot_size"])

    def refresh_history(self, instrument: str, now: datetime) -> pd.DataFrame:
        """First call (or a new day): full load. Afterwards only the last few minutes are fetched and merged, so the newest
        CLOSED 1m bar is never more than one poll old (2 small REST calls per refresh)."""
        df = self.history.get(instrument)
        if df is None or df.index[-1].date() != now.date():
            return self.load_history(instrument, now)
        end = now.replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
        start = df.index[-1].tz_localize(None).to_pydatetime() - timedelta(minutes=10)
        new = self._candles(self.indices[instrument]["groww_symbol"], "CASH", start, end).drop(columns=["oi"])
        fut = nearest_future(self.master, instrument, now.date())
        if fut:
            f = self._candles(fut["groww_symbol"], "FNO", start, end)
            new["volume"] = f["volume"].reindex(new.index).fillna(0)
        merged = pd.concat([df, new])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        merged.attrs.update(df.attrs)
        self.history[instrument] = merged
        return merged

    @staticmethod
    def chain_view(cdf, price, n=6):
        i = int((cdf["strike"] - price).abs().argmin())
        rows = cdf.iloc[max(i - n, 0): i + n + 1]
        return [{"strike": float(r.strike), "atm": j == min(i, n), "ce_ltp": r.ce_ltp, "ce_oi": r.ce_oi, "ce_vol": r.ce_vol, "ce_iv": r.ce_iv,
                 "pe_ltp": r.pe_ltp, "pe_oi": r.pe_oi, "pe_vol": r.pe_vol, "pe_iv": r.pe_iv} for j, r in enumerate(rows.itertuples())]

    # ---- one decision ----
    def evaluate(self, instrument: str, now: datetime = None) -> dict:
        now = now or datetime.now(IST)
        tick = self.feed.ticks.get(instrument)
        ds = live_state("LIVE", tick, self.feed.connected, market_state(now), time.time())
        closed = ds["state"] == "MARKET_CLOSED"            # after hours: analyse the last session for the dashboard, never a trade
        if not ds["live"] and not closed:
            return self._log(E.evaluate_option_signal(instrument, now, ds, {}, {}, {}, None, "", 0, self.gate))
        df1 = self.history.get(instrument)
        df1 = self.load_history(instrument, now) if (closed and df1 is None) else self.refresh_history(instrument, now)
        now_eval = now if not closed else now.replace(hour=15, minute=31, second=0, microsecond=0)
        fr = {tf: resample_closed(df1, tf, now_eval) for tf in MINUTES}
        price = tick["price"] if (tick and not closed) else float(fr["1m"]["close"].iloc[-1])
        ds = ds if not closed else {**ds, "data_time_ist": f"{fr['1m'].index[-1] + pd.Timedelta(minutes=1):%Y-%m-%d %H:%M:%S} (last session close)",
                                    "label": "MARKET CLOSED"}
        expiry = self.expiry(instrument, now)
        cdf, und = parse_chain(self.raw.get_option_chain(self.raw.EXCHANGE_NSE, instrument, expiry))
        chain_an = analyze_chain(cdf, price)
        self.snapshots.save(instrument, expiry, now, cdf)
        oi_change = self.snapshots.change(instrument, expiry, now, cdf)
        f5, today = fr["5m"], now.date()
        eod = now.replace(hour=23, minute=59)
        now = now_eval if closed else now
        past5 = resample_closed(df1, "5m", eod)
        prev = analyze_previous_day(past5[past5.index.date < today], None, today)
        cmp_ = compare_with_today(prev, f5[f5.index.date == today], price)
        cpr = cpr_and_pivots(prev["high"], prev["low"], prev["close"]) if prev.get("available") else None
        atr5 = E.analyse_frame(f5)["atr"] if len(f5) >= 40 else None
        zones = repeated_zones(past5, price, atr5) if atr5 else None
        first_sym = next((x for x in cdf["ce_symbol"].dropna()), None)
        lot = self.lot_size(first_sym) if first_sym else None
        if lot is None:
            ds = {**ds, "live": False, "state": "UNAVAILABLE", "reason": "Lot size not found in the instrument master"}
        sig = E.evaluate_option_signal(instrument, now, ds, fr, prev, cmp_, (cdf, price, chain_an), expiry, lot or 0,
                                       self.gate, self.quote, cpr, zones, oi_change if oi_change != UNAVAILABLE else None, self.params)
        sig.update(volume_source=df1.attrs.get("volume_source"), oi_change_chain=oi_change, previous_session=prev, today_vs_previous=cmp_,
                   chain_view=self.chain_view(cdf, price), expiry=expiry, zones=zones, cpr=cpr,
                   sessions=session_summaries(past5, 5, today),
                   spark=[round(float(x), 2) for x in f5[f5.index.date == today]["close"]],
                   change=({"abs": round(price - prev["close"], 2), "pct": round(100 * (price - prev["close"]) / prev["close"], 2)} if prev.get("available") else None), last_closed_1m=str(fr["1m"].index[-1]) if len(fr["1m"]) else None)
        return self._log(sig)

    def _log(self, sig: dict) -> dict:
        try:
            DECISIONS.parent.mkdir(parents=True, exist_ok=True)
            with open(DECISIONS, "a") as f:
                slim = {k: sig.get(k) for k in ("time", "instrument", "action", "status", "reasons", "score", "id", "data")}
                slim["sides"] = {k: {"score": v["score"], "trigger": v["trigger"], "missing": v["missing"]} for k, v in sig.get("sides", {}).items()}
                f.write(json.dumps(slim, default=str) + "\n")
        except OSError as e:
            log.warning("Could not write decision log: %s", e)
        return sig


def render(sig: dict) -> str:
    d = sig["data"]
    head = [f"{sig['instrument']} | {sig['action']}   [{sig.get('mode', 'PAPER SIGNAL')}]",
            f"Time: {sig['time']} IST   Data: {d.get('label')} (tick age {d.get('tick_age_s')}s, latency {d.get('latency_s')}s, data time {d.get('data_time_ist')})"]
    if sig["action"].startswith("BUY"):
        o, u, p = sig["option"], sig["underlying"], sig["premium"]
        t = p["targets"]
        return "\n".join(head + [
            f"Underlying LTP: {sig['underlying_price']}   Regime: {sig['regime_name']}   Pattern: {sig['pattern']}",
            f"Contract: {o['symbol']}  ({o['type']} {o['strike']:.0f}, expiry {o['expiry']})  delta {o['delta']}  IV {o['iv']}  OI {o['oi']}",
            f"Premium  -> Entry {p['entry']} (zone {p['entry_zone'][0]}-{p['entry_zone'][1]})  SL {p['stop']}  " +
            "  ".join(f"T{i + 1} {x['premium']} (RR {x['rr']})" for i, x in enumerate(t)),
            f"Underlying-> Entry {u['entry']}  SL {u['stop']}  " + "  ".join(f"T{i + 1} {x['level']} [{x['basis']}]" for i, x in enumerate(u['targets'])),
            f"Size: {p['lots']} lot(s) x {p['lot_size']} = {p['quantity']} qty   max loss Rs {p['max_loss_rupees']}   cost Rs {p['premium_cost_rupees']}",
            f"Quality score: {sig['score']}/100 ({sig['classification']})   Invalidation: {sig['invalidation']}",
            "WHY THIS TRADE: " + " | ".join(sig["why_trade"][:8]), f"Signal ID: {sig['id']}", sig["premium"]["estimate_note"]])
    lines = head
    if sig.get("sides"):
        for k, v in sig["sides"].items():
            lines.append(f"  {k}: score {v['score']} ({v['classification']}), regime {v['regime']}, trigger: {v['trigger']}")
    lines.append("WHY NO TRADE / WAITING FOR: " if sig["action"] != "BUY" else "")
    lines += [f"  - {r}" for r in sig.get("reasons", [])[:8]]
    return "\n".join(lines)

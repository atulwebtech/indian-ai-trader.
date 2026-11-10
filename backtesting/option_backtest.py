"""Replay of the option engine over historical 1-minute index candles. READ THIS BEFORE TRUSTING ANY NUMBER:

* Underlying candles are REAL (Groww history). Option PREMIUMS ARE MODELLED (Black-Scholes, fixed IV, fixed time to expiry,
  assumed bid-ask spread) because Groww does not provide history for expired option contracts. Open interest, option volume,
  PCR and OI walls are NOT available historically, so those parts of the score are unavailable and liquidity is not tested.
* Futures volume (VWAP, relative volume) exists only for days covered by the current futures contract; other days run without it.
* Costs (brokerage, STT, GST, exchange, stamp) and slippage are applied. Stops/targets are checked against each 1m bar's worst
  price first (stop before target), so results lean conservative.
* Walk-forward = fixed rules (nothing is fitted) evaluated on consecutive session blocks. Parameter sensitivity filters the
  lowest-threshold run by score (approximate: it ignores daily-limit interactions).
No result here is evidence of future profit. The report states how much evidence there is.
"""
import math
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from config.settings import IST, ROOT
from indicators.market_levels import cpr_and_pivots, repeated_zones, session_summaries
from option_chain.chain import analyze_chain, contract_quote_info, parse_chain
from paper_trading.costs import CostModel
from paper_trading.option_ledger import OptionLedger
from risk_management.option_risk import OptionParams, OptionRiskGate
from strategies import option_engine as E
from strategies.prev_day import analyze_previous_day, compare_with_today

LIVE = {"state": "LIVE", "live": True, "label": "BACKTEST", "reason": "historical replay", "tick_age_s": 0, "latency_s": 0, "data_time_ist": None}
MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100}
TAIL = {"1m": 300, "3m": 200, "5m": 500, "15m": 300, "30m": 200, "1h": 150}
RATE = 0.065


# ---------------- Black-Scholes (modelled premiums) ----------------
def _cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs(S, K, T, sigma, cp):
    """Returns dict(price, delta, gamma, theta_per_day, vega_per_1pct). cp = 'C' or 'P'. T in years."""
    T = max(T, 1 / 365 / 24)
    d1 = (math.log(S / K) + (RATE + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    disc = math.exp(-RATE * T)
    if cp == "C":
        price, delta = S * _cdf(d1) - K * disc * _cdf(d2), _cdf(d1)
        theta = -S * _pdf(d1) * sigma / (2 * math.sqrt(T)) - RATE * K * disc * _cdf(d2)
    else:
        price, delta = K * disc * _cdf(-d2) - S * _cdf(-d1), _cdf(d1) - 1
        theta = -S * _pdf(d1) * sigma / (2 * math.sqrt(T)) + RATE * K * disc * _cdf(-d2)
    return {"price": price, "delta": delta, "gamma": _pdf(d1) / (S * sigma * math.sqrt(T)), "theta": theta / 365, "vega": S * _pdf(d1) * math.sqrt(T) / 100}


def years_to_expiry(days_to_expiry: float, ts) -> float:
    """ONE time function for chain building and trade simulation, so a flat market decays only by theta (no artificial gap).
    days_to_expiry is measured at the 09:15 open; the session is 375 minutes."""
    minutes = (ts.hour * 60 + ts.minute) - (9 * 60 + 15)
    return max(days_to_expiry - minutes / 375.0, 1 / 375.0) / 365.0


def modelled_chain(instrument, spot, sigma, T):
    """T in years (use years_to_expiry)."""
    step = STRIKE_STEP[instrument]
    base = round(spot / step) * step
    strikes = {}
    for k in range(int(base - 8 * step), int(base + 9 * step), step):
        leg = {}
        for side, cp in (("CE", "C"), ("PE", "P")):
            g = bs(spot, k, T, sigma, cp)
            leg[side] = {"greeks": {"delta": round(g["delta"], 4), "gamma": g["gamma"], "theta": g["theta"], "vega": g["vega"], "iv": sigma * 100},
                         "trading_symbol": f"{instrument}-MODEL-{k}{side}", "ltp": round(max(g["price"], 0.05), 2), "open_interest": 0, "volume": 0}
        strikes[str(k)] = leg
    return parse_chain({"underlying_ltp": spot, "strikes": strikes})[0]


def make_quote_fn(cdf, spread_pct):
    def quote(sym):
        row = cdf[(cdf.ce_symbol == sym) | (cdf.pe_symbol == sym)].iloc[0]
        ltp = row.ce_ltp if row.ce_symbol == sym else row.pe_ltp
        half = ltp * spread_pct / 200
        return contract_quote_info({"depth": {"buy": [{"price": round(ltp - half, 2), "quantity": 1}], "sell": [{"price": round(ltp + half, 2), "quantity": 1}]},
                                    "last_price": ltp})
    return quote


# ---------------- replay ----------------
def precompute(df1):
    """Resample the WHOLE series once. Safe against look-ahead because frames_at() only returns bars that had fully closed at
    `now`, and such a bar is built only from minutes before `now`."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return {tf: (df1 if tf == "1m" else df1.resample(f"{m}min", origin="start_day", offset="9h15min").agg(agg).dropna(subset=["close"]))
            for tf, m in MINUTES.items()}


def frames_at(full, now):
    ts, out = pd.Timestamp(now), {}
    for tf, m in MINUTES.items():
        x = full[tf]
        k = x.index.searchsorted(ts - pd.Timedelta(minutes=m), side="right")        # bars whose end <= now
        out[tf] = x.iloc[max(0, k - TAIL[tf]):k]
    return out


def _frames(df1, now):                                                             # kept for tests / small inputs
    return frames_at(precompute(df1), now)


def backtest_params(**kw) -> OptionParams:
    """Liquidity filters are off because historical OI/volume do not exist; they are untested, not passed."""
    base = dict(capital=100000.0, min_oi_lots=0, min_option_volume_lots=0, max_spread_pct=5.0, max_theta_pct=60.0, max_theta_drag_risk=1.0)
    base.update(kw)
    return OptionParams(**base)


def replay(df1_map: dict, params: OptionParams = None, iv: float = 15.0, days_to_expiry: float = 3.0, spread_pct: float = 0.6,
           step_minutes: int = 5, warmup_sessions: int = 5, lot_sizes=None, costs: CostModel = None, first_day=None) -> dict:
    """df1_map: {instrument: 1-minute DataFrame (open, high, low, close, volume), IST index}. Returns {'ledger', 'rows', 'sessions'}."""
    params = params or backtest_params()
    lot_sizes = lot_sizes or {"NIFTY": 65, "BANKNIFTY": 30}
    tmp = Path(tempfile.mkdtemp())
    ledger = OptionLedger(tmp / "ledger.json", costs or CostModel())
    gate = OptionRiskGate(params, tmp / "gate.json")
    sigma, rows, sessions = iv / 100.0, [], set()
    for inst, df1 in df1_map.items():
        full = precompute(df1)
        full5 = full["5m"]
        days = sorted(set(df1.index.date))
        for d in days[warmup_sessions:]:
            if first_day and d < first_day:
                continue
            sessions.add(str(d))
            day = df1[df1.index.date == d]
            past5 = full5[full5.index.date < d].tail(2500)                       # once per day
            prev = analyze_previous_day(past5, None, d)
            cpr = cpr_and_pivots(prev["high"], prev["low"], prev["close"]) if prev.get("available") else None
            t = datetime(d.year, d.month, d.day, 9, 30, 5, tzinfo=IST)
            end = datetime(d.year, d.month, d.day, 14, 45, 5, tzinfo=IST)
            blocked_until = None
            while t <= end:
                now = t
                t = t + timedelta(minutes=step_minutes)
                if blocked_until and now < blocked_until:
                    continue
                fr = frames_at(full, now)
                if any(len(fr[tf]) < E.MIN_BARS[tf] for tf in E.TFS) or len(fr["1m"]) < 300:
                    continue
                spot = float(fr["1m"]["close"].iloc[-1])
                cdf = modelled_chain(inst, spot, sigma, years_to_expiry(days_to_expiry, now))
                chain_an = analyze_chain(cdf, spot)
                f5 = fr["5m"]
                cmp_ = compare_with_today(prev, f5[f5.index.date == d], spot)
                atr5 = E.analyse_frame(f5)["atr"] if len(f5) >= 40 else None
                zones = repeated_zones(past5, spot, atr5) if atr5 else None
                sig = E.evaluate_option_signal(inst, now, LIVE, fr, prev, cmp_, (cdf, spot, chain_an), "modelled", lot_sizes[inst], gate,
                                               make_quote_fn(cdf, spread_pct), cpr, zones, None, params)
                rows.append({"time": now.isoformat(timespec="minutes"), "instrument": inst, "action": sig["action"], "status": sig.get("status"),
                             "call": sig.get("sides", {}).get("CALL", {}).get("score"), "put": sig.get("sides", {}).get("PUT", {}).get("score")})
                if not sig["action"].startswith("BUY"):
                    continue
                sig["pattern_group"] = sig["setup_type"]
                ledger.add(sig)
                end_time = _simulate(sig, ledger, gate, day, now, inst, sigma, days_to_expiry, spread_pct, params)
                blocked_until = end_time + timedelta(minutes=45) if ledger.get(sig["id"])["net"] is not None and ledger.get(sig["id"])["net"] < 0 else end_time
    return {"ledger": ledger, "rows": rows, "sessions": sorted(sessions)}


def _simulate(sig, ledger, gate, day, now, inst, sigma, dte, spread_pct, params):
    """Walk forward minute by minute with modelled premiums; worst price of the bar first (stop before target)."""
    o = sig["option"]
    cp = "C" if o["type"] == "CE" else "P"
    K = o["strike"]
    s = 1 if cp == "C" else -1
    bars = day[day.index >= pd.Timestamp(now).floor("min") + pd.Timedelta(minutes=1)]
    t_end = now
    for ts, b in bars.iterrows():
        t_end = ts.to_pydatetime() + timedelta(minutes=1)
        T = years_to_expiry(dte, ts)
        worst, best = (b["low"], b["high"]) if s > 0 else (b["high"], b["low"])
        for px in (worst, best, b["close"]):
            prem = bs(px, K, T, sigma, cp)["price"]
            res = E.advance_signal(sig, float(prem), float(px), t_end, params)
            exit_px = prem * (1 - spread_pct / 200)
            net = ledger.apply(sig["id"], res, t_end, exit_premium=exit_px if res["final"] else None)
            if res["final"]:
                if net is not None:
                    gate.record_result(sig["id"], net, t_end)
                return t_end
    if not ledger.get(sig["id"])["status"] in ("TARGET2_HIT", "TARGET3_HIT", "STOP_LOSS", "INVALIDATED", "EXPIRED"):
        last = bars.iloc[-1] if len(bars) else None
        if last is not None:
            prem = bs(float(last["close"]), K, years_to_expiry(dte, last.name), sigma, cp)["price"]
            res = {"status": "EXPIRED", "event": "End of data", "final": True, "pnl_per_unit": prem - sig["premium"]["entry"]}
            net = ledger.apply(sig["id"], res, t_end, exit_premium=prem * (1 - spread_pct / 200))
            if net is not None:
                gate.record_result(sig["id"], net, t_end)
    return t_end


# ---------------- reporting ----------------
def _m(trades):
    nets = [float(t["net"]) for t in trades]
    if not nets:
        return {"trades": 0}
    w, l = [x for x in nets if x > 0], [x for x in nets if x <= 0]
    cum = peak = mdd = streak = worst_streak = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
        streak = streak + 1 if x <= 0 else 0
        worst_streak = max(worst_streak, streak)
    gl = -sum(l)
    return {"trades": len(nets), "win_rate": round(100 * len(w) / len(nets), 1), "avg_win": float(round(sum(w) / len(w), 0)) if w else 0,
            "avg_loss": float(round(-gl / len(l), 0)) if l else 0, "expectancy": float(round(sum(nets) / len(nets), 0)),
            "profit_factor": round(sum(w) / gl, 2) if gl > 0 else None, "net": float(round(sum(nets), 0)), "max_dd": float(round(mdd, 0)),
            "max_consec_losses": int(worst_streak), "costs": float(round(sum(t["costs"]["total"] for t in trades), 0))}


def evidence_verdict(trades, oos_trades) -> str:
    n, o = len(trades), len(oos_trades)
    if n < 30:
        return f"INSUFFICIENT EVIDENCE: only {n} trades. Nothing can be concluded."
    m, mo = _m(trades), _m(oos_trades)
    if n < 100 or o < 30:
        return f"WEAK EVIDENCE: {n} trades ({o} out-of-sample). Not enough to call the strategy profitable or not."
    if m["expectancy"] > 0 and mo.get("expectancy", 0) > 0 and (m["profit_factor"] or 0) >= 1.2 and (mo.get("profit_factor") or 0) >= 1.1:
        return "POSITIVE in-sample and out-of-sample on MODELLED premiums. Still not proof: paper-trade live before risking money."
    return "NOT PROVEN: expectancy or profit factor is not positive both in-sample and out-of-sample (modelled premiums)."


def build_report(result: dict, meta: dict, params: OptionParams) -> str:
    L = result["ledger"]
    trades = L.trades()
    split = int(len(result["sessions"]) * 0.6)
    cut = result["sessions"][split] if result["sessions"] else "9999"
    ins = [t for t in trades if t["created_at"][:10] < cut]
    oos = [t for t in trades if t["created_at"][:10] >= cut]
    lines = ["# Option engine backtest (MODELLED premiums)", "", f"Generated {datetime.now(IST):%Y-%m-%d %H:%M} IST", "",
             "> Underlying candles are real. Option premiums are modelled (Black-Scholes, "
             f"IV {meta['iv']}%, {meta['dte']} days to expiry, spread {meta['spread']}%). OI, option volume, PCR/OI walls and liquidity are not testable historically.",
             f"> Data: {meta.get('data', '')}", f"> Sessions replayed: {len(result['sessions'])}  ({result['sessions'][:1]} .. {result['sessions'][-1:]}), step {meta['step']} min, score threshold {params.min_score}", "",
             f"## Verdict\n\n**{evidence_verdict(trades, oos)}**", "", "## Overall (net of brokerage, taxes, slippage)", "", f"{_m(trades)}", "",
             f"## Walk-forward split (fixed rules): in-sample before {cut}, out-of-sample from {cut}", "", f"- in-sample: {_m(ins)}", f"- out-of-sample: {_m(oos)}", ""]
    blocks = np.array_split(np.array(result["sessions"], dtype=object), 4) if len(result["sessions"]) >= 8 else []
    if blocks:
        lines += ["## Rolling blocks (consecutive session groups)", ""]
        for b in blocks:
            if len(b):
                lines.append(f"- {b[0]} .. {b[-1]}: {_m([t for t in trades if b[0] <= t['created_at'][:10] <= b[-1]])}")
        lines.append("")
    for title, key in (("By setup type", lambda t: t["sig"].get("setup_type")), ("By regime", lambda t: t["regime"]),
                       ("By instrument", lambda t: t["instrument"]), ("By direction", lambda t: t["action"])):
        lines += [f"## {title}", ""]
        for g in sorted({key(t) for t in trades}, key=str):
            lines.append(f"- {g}: {_m([t for t in trades if key(t) == g])}")
        lines.append("")
    lines += ["## Score threshold sensitivity (approximate: filters the run by score)", ""]
    for th in (60, 65, 70, 75, 80, 85):
        lines.append(f"- score >= {th}: {_m([t for t in trades if t['score'] >= th])}")
    lines += ["", f"## Signals not entered / invalidated before entry: {sum(1 for s in L.data['signals'].values() if not s['entered'])} (not counted as trades)", "",
              "## Decisions", ""]
    from collections import Counter
    c = Counter((r["action"], r["status"]) for r in result["rows"])
    lines += [f"- {a} / {s}: {n}" for (a, s), n in c.most_common()]
    return "\n".join(lines)


# ---------------- CLI entry (real data) ----------------
def fetch_history(session, master, indices, instrument, months):
    """1m index candles in <=29 day chunks going back `months` months, stops when Groww returns nothing. Futures volume where available."""
    from groww_integration.historical import parse_candles
    from groww_integration.instruments import nearest_future
    import time as _t
    g = session.client.unwrap_for_feed()
    end = datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
    parts, fparts, depth_note = [], [], ""
    cur_end = end
    fut = nearest_future(master, instrument, end.date())
    for i in range(max(1, int(months * 30 // 29) + 1)):
        start = (cur_end - timedelta(days=29)).replace(hour=9, minute=15)
        try:
            p = session.call("get_historical_candles", exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_CASH, groww_symbol=indices[instrument]["groww_symbol"],
                             start_time=start.strftime("%Y-%m-%d %H:%M:%S"), end_time=cur_end.strftime("%Y-%m-%d %H:%M:%S"), candle_interval=g.CANDLE_INTERVAL_MIN_1)
            df = parse_candles(p)
        except Exception as e:                                         # depth limit or API error: report, do not hide
            depth_note = f"stopped at chunk {i + 1}: {type(e).__name__}: {str(e)[:80]}"
            break
        if df.empty:
            depth_note = f"no data before {cur_end:%Y-%m-%d}"
            break
        parts.append(df)
        if fut and i < 2:
            try:
                fparts.append(parse_candles(session.call("get_historical_candles", exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_FNO, groww_symbol=fut["groww_symbol"],
                                                         start_time=start.strftime("%Y-%m-%d %H:%M:%S"), end_time=cur_end.strftime("%Y-%m-%d %H:%M:%S"),
                                                         candle_interval=g.CANDLE_INTERVAL_MIN_1)))
            except Exception:
                pass
        cur_end = start
        _t.sleep(0.25)
    idx = pd.concat(parts).drop(columns=["oi"])
    idx = idx[~idx.index.duplicated(keep="last")].sort_index()
    idx["volume"] = 0.0
    if fparts:
        f = pd.concat(fparts)
        f = f[~f.index.duplicated(keep="last")]
        idx["volume"] = f["volume"].reindex(idx.index).fillna(0)
    idx.attrs["depth_note"] = depth_note
    return idx


def run_option_backtest(settings, months=6, iv=15.0, step=5):
    from groww_integration.auth import GrowwAuthError, GrowwSession
    from groww_integration.instruments import load_master, validate_indices
    try:
        sess = GrowwSession(settings.groww_api_key, settings.groww_api_secret, settings.groww_totp_secret)
        sess.connect()
    except GrowwAuthError as e:
        print(f"DATA UNAVAILABLE: {e}")
        return
    master = load_master(sess.client)
    indices = validate_indices(master)
    data, notes, lots = {}, [], {}
    for inst in ("NIFTY", "BANKNIFTY"):
        df = fetch_history(sess, master, indices, inst, months)
        data[inst] = df
        notes.append(f"{inst}: {len(df)} 1m bars, {len(set(df.index.date))} sessions {df.index[0]:%Y-%m-%d}..{df.index[-1]:%Y-%m-%d} {df.attrs.get('depth_note', '')}")
        row = master[(master["underlying_symbol"] == inst) & (master["instrument_type"].isin(["CE", "PE"]))]
        lots[inst] = int(row["lot_size"].iloc[0]) if len(row) else {"NIFTY": 65, "BANKNIFTY": 30}[inst]
    print("\n".join(notes))
    params = backtest_params(min_score=60.0, wait_score=50.0)
    print(f"Replaying (this takes a while)... threshold {params.min_score}, step {step} min")
    res = replay(data, params, iv=iv, step_minutes=step, lot_sizes=lots)
    meta = {"iv": iv, "dte": 3.0, "spread": 0.6, "step": step, "data": "; ".join(notes)}
    rep = build_report(res, meta, params)
    out = ROOT / "reports" / f"option_backtest_{datetime.now(IST):%Y-%m-%d}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(rep)
    print(rep[:3500])
    print(f"\nFull report: {out}")

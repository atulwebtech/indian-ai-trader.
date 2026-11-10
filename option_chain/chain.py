"""Option chain parsing and analysis. Uses only fields Groww actually returns; anything else is None / UNAVAILABLE.

get_option_chain gives per strike and side: ltp, open_interest, volume, greeks (delta, gamma, theta, vega, rho, iv).
It has NO bid/ask and NO change in OI. Those come from get_quote(contract) (depth, previous_open_interest) and are
fetched only for the few contracts we actually evaluate, to keep API calls low.
"""
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from config.settings import ROOT

UNAVAILABLE = "UNAVAILABLE"
COLS = ["strike", "ce_symbol", "ce_ltp", "ce_oi", "ce_vol", "ce_iv", "ce_delta", "ce_gamma", "ce_theta", "ce_vega",
        "pe_symbol", "pe_ltp", "pe_oi", "pe_vol", "pe_iv", "pe_delta", "pe_gamma", "pe_theta", "pe_vega"]


class ChainError(Exception):
    pass


def parse_chain(payload: dict):
    """Return (DataFrame sorted by strike, underlying_ltp). Raises ChainError on an unexpected shape (never guesses)."""
    try:
        und = float(payload["underlying_ltp"])
        strikes = payload["strikes"]
    except (KeyError, TypeError, ValueError) as e:
        raise ChainError(f"Unexpected option chain shape: {e}") from e
    rows = []
    for k, v in strikes.items():
        row = {"strike": float(k)}
        for side, p in (("CE", "ce"), ("PE", "pe")):
            leg = (v or {}).get(side) or {}
            g = leg.get("greeks") or {}
            row.update({f"{p}_symbol": leg.get("trading_symbol"), f"{p}_ltp": leg.get("ltp"), f"{p}_oi": leg.get("open_interest"),
                        f"{p}_vol": leg.get("volume"), f"{p}_iv": g.get("iv"), f"{p}_delta": g.get("delta"),
                        f"{p}_gamma": g.get("gamma"), f"{p}_theta": g.get("theta"), f"{p}_vega": g.get("vega")})
        rows.append(row)
    if not rows:
        raise ChainError("Option chain has no strikes")
    df = pd.DataFrame(rows).reindex(columns=COLS).sort_values("strike").reset_index(drop=True)
    return df, und


def analyze_chain(df: pd.DataFrame, underlying: float, band: int = 10) -> dict:
    """ATM, PCR, OI walls, ATM IV. Walls are the highest-OI strikes above (calls) / below (puts) the price."""
    atm = float(df.iloc[(df["strike"] - underlying).abs().argmin()]["strike"])
    i = int(df.index[df["strike"] == atm][0])
    near = df.iloc[max(i - band, 0): i + band + 1]
    ce_tot, pe_tot = df["ce_oi"].fillna(0).sum(), df["pe_oi"].fillna(0).sum()
    ce_n, pe_n = near["ce_oi"].fillna(0).sum(), near["pe_oi"].fillna(0).sum()
    above, below = df[df["strike"] >= underlying], df[df["strike"] <= underlying]
    res = above.loc[above["ce_oi"].fillna(0).idxmax()] if len(above) and above["ce_oi"].fillna(0).max() > 0 else None
    sup = below.loc[below["pe_oi"].fillna(0).idxmax()] if len(below) and below["pe_oi"].fillna(0).max() > 0 else None
    row = df.iloc[i]
    ivs = [x for x in (row["ce_iv"], row["pe_iv"]) if pd.notna(x)]
    pcr = round(pe_tot / ce_tot, 3) if ce_tot else None
    pcr_near = round(pe_n / ce_n, 3) if ce_n else None
    lean = None
    if pcr_near is not None:
        lean = "BULLISH" if pcr_near >= 1.2 else "BEARISH" if pcr_near <= 0.8 else "NEUTRAL"
    return {"atm_strike": atm, "pcr_total": pcr, "pcr_near_atm": pcr_near, "pcr_lean": lean,
            "resistance_oi": ({"strike": float(res["strike"]), "oi": int(res["ce_oi"])} if res is not None else None),
            "support_oi": ({"strike": float(sup["strike"]), "oi": int(sup["pe_oi"])} if sup is not None else None),
            "atm_iv": round(sum(ivs) / len(ivs), 2) if ivs else None,
            "total_ce_oi": int(ce_tot), "total_pe_oi": int(pe_tot)}


def contract_quote_info(quote: dict) -> dict:
    """Bid/ask, spread, and the exchange-reported change in OI from a get_quote response. None where not provided."""
    depth = (quote or {}).get("depth") or {}
    buy, sell = depth.get("buy") or [], depth.get("sell") or []
    bid = float(buy[0]["price"]) if buy and buy[0].get("price") else None
    ask = float(sell[0]["price"]) if sell and sell[0].get("price") else None
    spread = round(ask - bid, 2) if bid is not None and ask is not None and ask >= bid else None
    mid = (bid + ask) / 2 if spread is not None else None
    oi, prev = (quote or {}).get("open_interest"), (quote or {}).get("previous_open_interest")
    chg = (oi - prev) if (oi is not None and prev is not None) else None
    return {"bid": bid, "ask": ask, "spread": spread, "spread_pct": round(100 * spread / mid, 2) if mid else None,
            "bid_qty": buy[0].get("quantity") if buy else None, "ask_qty": sell[0].get("quantity") if sell else None,
            "oi": oi, "previous_oi": prev, "oi_change": chg,
            "oi_change_pct": round(100 * chg / prev, 2) if chg is not None and prev else None,
            "ltp": (quote or {}).get("last_price"), "volume": (quote or {}).get("volume"), "last_trade_time": (quote or {}).get("last_trade_time")}


class SnapshotStore:
    """Saves chain OI snapshots so intraday change in OI can be computed from OUR OWN history. Until a snapshot at least
    `min_age_minutes` old exists for the same expiry and day, the change is UNAVAILABLE."""

    def __init__(self, folder=None):
        self.folder = Path(folder) if folder else ROOT / "data" / "oi_snapshots"

    def _file(self, instrument, expiry, day):
        return self.folder / f"{instrument}_{expiry}_{day}.json"

    def save(self, instrument: str, expiry: str, now: datetime, df: pd.DataFrame) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        f = self._file(instrument, expiry, now.date().isoformat())
        try:
            data = json.loads(f.read_text()) if f.exists() else []
        except (OSError, ValueError):
            data = []
        if data and (now - datetime.fromisoformat(data[-1]["t"])).total_seconds() < 120:
            return                                  # at most one snapshot per 2 minutes
        data.append({"t": now.isoformat(), "oi": {str(int(r.strike)): [r.ce_oi, r.pe_oi] for r in df.itertuples()}})
        f.write_text(json.dumps(data[-400:]))

    def change(self, instrument: str, expiry: str, now: datetime, df: pd.DataFrame, min_age_minutes: int = 15):
        """Total CE/PE OI change vs the oldest snapshot of today that is at least min_age_minutes old, else UNAVAILABLE."""
        f = self._file(instrument, expiry, now.date().isoformat())
        if not f.exists():
            return UNAVAILABLE
        try:
            snaps = json.loads(f.read_text())
        except (OSError, ValueError):
            return UNAVAILABLE
        old = [s for s in snaps if (now - datetime.fromisoformat(s["t"])).total_seconds() >= min_age_minutes * 60]
        if not old:
            return UNAVAILABLE
        base = old[0]
        ce = pe = 0
        for r in df.itertuples():
            b = base["oi"].get(str(int(r.strike)))
            if b and b[0] is not None and b[1] is not None and pd.notna(r.ce_oi) and pd.notna(r.pe_oi):
                ce += r.ce_oi - b[0]
                pe += r.pe_oi - b[1]
        return {"since": base["t"], "ce_oi_change": int(ce), "pe_oi_change": int(pe),
                "lean": "BULLISH" if pe > ce and pe > 0 else "BEARISH" if ce > pe and ce > 0 else "NEUTRAL"}

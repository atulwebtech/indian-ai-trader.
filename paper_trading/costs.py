"""Round-trip cost of an INTRADAY OPTION BUY (rupees). Rates are ASSUMPTIONS (typical NSE/SEBI/broker charges, 2025-26) and
must be checked against your own broker contract note. Override with OPT_COST_* in .env if your broker differs."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    brokerage_per_order: float = 20.0       # flat per executed order (buy and sell)
    stt_sell_pct: float = 0.10              # on sell premium value
    exchange_txn_pct: float = 0.03503       # NSE options, on premium turnover (both legs)
    sebi_pct: float = 0.0001                # Rs 10 per crore
    stamp_buy_pct: float = 0.003            # on buy premium value
    gst_pct: float = 18.0                   # on brokerage + exchange + SEBI
    slippage_pct: float = 0.30              # of premium, applied against us on each leg (entry already at ask; this is extra)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        kw = {}
        for var, field in (("OPT_COST_BROKERAGE", "brokerage_per_order"), ("OPT_COST_SLIPPAGE_PCT", "slippage_pct")):
            if str(env.get(var, "")).strip():
                kw[field] = float(env[var])
        return cls(**kw)

    def round_trip(self, entry: float, exit_: float, qty: int) -> dict:
        buy, sell = entry * qty, exit_ * qty
        brokerage = 2 * self.brokerage_per_order
        stt = sell * self.stt_sell_pct / 100
        exch = (buy + sell) * self.exchange_txn_pct / 100
        sebi = (buy + sell) * self.sebi_pct / 100
        stamp = buy * self.stamp_buy_pct / 100
        gst = (brokerage + exch + sebi) * self.gst_pct / 100
        slip = (buy + sell) * self.slippage_pct / 100
        total = brokerage + stt + exch + sebi + stamp + gst + slip
        return {"brokerage": round(brokerage, 2), "stt": round(stt, 2), "exchange": round(exch, 2), "sebi": round(sebi, 2),
                "stamp": round(stamp, 2), "gst": round(gst, 2), "slippage": round(slip, 2), "total": round(total, 2)}

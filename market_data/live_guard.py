"""Decides whether data may be treated as LIVE. Mock/historical/stale/disconnected data never produces a live signal.

States: LIVE, STALE, DISCONNECTED, UNAVAILABLE (no tick yet / unparsed), NOT_LIVE_SOURCE (MOCK/HISTORICAL mode),
MARKET_CLOSED. Only LIVE allows a live trade signal.
"""
from datetime import datetime, timezone

from config.settings import IST

MAX_TICK_AGE_SECONDS = 10.0
MAX_LATENCY_SECONDS = 15.0     # exchange timestamp vs our receive time (also catches a stale exchange clock)


def live_state(data_mode: str, tick: dict, connected: bool, market: str, now_epoch: float,
               max_age: float = MAX_TICK_AGE_SECONDS, max_latency: float = MAX_LATENCY_SECONDS) -> dict:
    """tick = {'price', 'exchange_ts_ms', 'received_at'} from LiveIndexFeed.ticks[instrument] (or None)."""
    base = {"state": "UNAVAILABLE", "live": False, "reason": "", "tick_age_s": None, "latency_s": None, "data_time_ist": None,
            "label": "DATA UNAVAILABLE"}

    def out(state, reason, live=False, label=None):
        return {**base, "state": state, "live": live, "reason": reason, "label": label or state.replace("_", " ")}

    if data_mode != "LIVE":
        return out("NOT_LIVE_SOURCE", f"DATA_MODE={data_mode}: not a live source", label="NOT LIVE DATA")
    if market != "OPEN":
        return out("MARKET_CLOSED", f"Market is {market}", label="MARKET CLOSED")
    if not connected:
        return out("DISCONNECTED", "WebSocket is not connected", label="DATA UNAVAILABLE")
    if not tick:
        return out("UNAVAILABLE", "No tick received yet", label="DATA UNAVAILABLE")
    age = now_epoch - tick["received_at"]
    ex_ms = tick.get("exchange_ts_ms")
    res = {**base, "tick_age_s": round(age, 2)}
    if ex_ms:
        res["latency_s"] = round(tick["received_at"] - ex_ms / 1000.0, 2)
        res["data_time_ist"] = datetime.fromtimestamp(ex_ms / 1000.0, timezone.utc).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    else:
        res["data_time_ist"] = datetime.fromtimestamp(tick["received_at"], timezone.utc).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S") + " (receive time)"
    if age > max_age:
        return {**res, "state": "STALE", "live": False, "label": "DATA STALE", "reason": f"Last tick {age:.0f}s ago (max {max_age:.0f}s)"}
    if res["latency_s"] is not None and abs(res["latency_s"]) > max_latency:
        return {**res, "state": "STALE", "live": False, "label": "DATA STALE",
                "reason": f"Exchange timestamp is {res['latency_s']:.0f}s away from receive time (max {max_latency:.0f}s)"}
    return {**res, "state": "LIVE", "live": True, "label": "LIVE", "reason": "Fresh tick from the live feed"}

"""One call that gathers every indicator into a single market snapshot."""
import pandas as pd

from indicators import levels, momentum, price_action, trend, volatility, volume


def analyze(intraday: pd.DataFrame, daily: pd.DataFrame) -> dict:
    """Snapshot as of the LAST bar of `intraday`. Uses only data up to that bar."""
    tf = intraday.attrs.get("timeframe", "")
    session_date = intraday.index[-1].date()
    day_df = intraday[intraday.index.date == session_date]
    prev = levels.previous_day_levels(daily, session_date)
    snap = {
        "instrument": intraday.attrs.get("instrument"),
        "timeframe": tf,
        "source": intraday.attrs.get("source"),
        "as_of": str(intraday.index[-1]),
        "price": float(intraday.close.iloc[-1]),
        "trend": trend.trend_summary(intraday),
        "momentum": momentum.momentum_summary(intraday),
        "volume": volume.volume_summary(intraday),
        "volatility": volatility.volatility_summary(intraday, daily),
        "price_action": price_action.price_action_summary(intraday),
        "previous_day": prev,
        "pivots": levels.classic_pivots(prev["prev_high"], prev["prev_low"], prev["prev_close"]) if prev else {},
        "opening_range": levels.opening_range(day_df, tf),
        "support_resistance": levels.support_resistance(intraday),
        "session_high": float(day_df.high.max()),
        "session_low": float(day_df.low.min()),
    }
    if volume.has_volume(day_df):
        snap["vwap"] = {"value": float(levels.session_vwap(day_df).iloc[-1]), "kind": "VWAP"}
    else:
        snap["vwap"] = {"value": float(levels.session_avg_price(day_df).iloc[-1]),
                        "kind": "PROXY (time-weighted average price; no index volume)"}
    return snap

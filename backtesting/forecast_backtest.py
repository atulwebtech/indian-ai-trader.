"""Out-of-sample test of the daily forecast on ~3 years of 1-hour candles.

For every past day, the pre-market forecast is built ONLY from data before that day, then compared
with what the day actually did (daily open/high/low/close). No parameters are fitted, so there is
no training set to leak: each day is a fresh out-of-sample test.
"""
import numpy as np
import pandas as pd

from backtesting.metrics import CostModel, summarize
from forecasting.daily_forecast import generate_forecast


def run_forecast_backtest(instrument: str, hourly: pd.DataFrame, daily: pd.DataFrame,
                          start="2024-01-15", progress=None) -> pd.DataFrame:
    hourly = hourly.copy()
    hourly.attrs.update(instrument=instrument, timeframe="1h", source="HISTORICAL")
    rows = []
    days = [d for d in daily.index if d >= pd.Timestamp(start, tz=daily.index.tz)]
    for k, ts in enumerate(days):
        day = ts.date()
        pre = hourly[hourly.index.date < day]
        pre.attrs.update(hourly.attrs)
        try:
            f = generate_forecast(instrument, pre, daily, session_date=day)
        except ValueError:
            continue
        row = daily.loc[ts]
        rec = {"date": day, "ref": f["reference_price"], "open": row.open, "high": row.high, "low": row.low,
               "close": row.close, "bias": f["bias"]["bias"], "score": f["bias"]["score"],
               "daily_atr": f["volatility"]["daily_atr"]}
        for side, key in (("up", "bullish"), ("dn", "bearish")):
            for t in f["scenarios"][key]["targets"]:
                i = t["label"][1]
                rec[f"{side}{i}_level"] = t["level"]
                rec[f"{side}{i}_rate"] = t["historical_reach_rate"]
                rec[f"{side}{i}_hit"] = bool(row.high >= t["level"]) if side == "up" else bool(row.low <= t["level"])
        rows.append(rec)
        if progress and k % 100 == 0:
            progress(k, len(days))
    return pd.DataFrame(rows)


def target_calibration(df: pd.DataFrame) -> pd.DataFrame:
    """Stated 'past days moved this far' rate vs how often the day's range actually reached it."""
    out = []
    for side in ("up", "dn"):
        for i in (1, 2, 3):
            sub = df.dropna(subset=[f"{side}{i}_rate"])
            if len(sub):
                out.append({"target": f"{'bull' if side == 'up' else 'bear'} T{i}", "days": len(sub),
                            "stated_rate": sub[f"{side}{i}_rate"].mean(), "actual_hit": sub[f"{side}{i}_hit"].mean()})
    return pd.DataFrame(out)


def bias_report(df: pd.DataFrame, cost: CostModel = None) -> dict:
    """Does the pre-market bias say anything about open->close? Compared with doing nothing."""
    cost = cost or CostModel()
    d = df.copy()
    d["oc"] = d["close"] - d["open"]
    d["oc_pct"] = 100 * d["oc"] / d["open"]
    out = {"all_days": {"n": len(d), "up_close_rate": float((d.oc > 0).mean()), "mean_oc_pct": float(d.oc_pct.mean())}}
    for b in ("BULLISH", "NEUTRAL", "BEARISH"):
        s = d[d.bias == b]
        out[b] = {"n": len(s), "up_close_rate": float((s.oc > 0).mean()) if len(s) else None,
                  "mean_oc_pct": float(s.oc_pct.mean()) if len(s) else None}
    # trade the bias open->close, net of costs (percent of price)
    tr = d[d.bias != "NEUTRAL"].copy()
    tr["net_pct"] = np.where(tr.bias == "BULLISH", tr.oc_pct, -tr.oc_pct) - cost.round_trip_bps / 100
    out["trade_bias"] = summarize(tr["net_pct"])
    tr["period"] = pd.to_datetime(tr["date"]).dt.to_period("Q").astype(str)
    out["by_quarter"] = {q: {"n": len(g), "mean_net_pct": float(g.net_pct.mean())} for q, g in tr.groupby("period")}
    return out


def add_regimes(df: pd.DataFrame, trend_days: int = 20, trend_pct: float = 2.0) -> pd.DataFrame:
    """Label each day by volatility (tercile of daily ATR / price) and by prior trend (20-day return).
    Each day's own values are known before it starts, but the volatility tercile cut-offs are computed
    over the whole sample, so this is a descriptive breakdown, not a tradable rule."""
    d = df.copy().reset_index(drop=True)
    d["vol_pct"] = d["daily_atr"] / d["ref"] * 100
    q1, q2 = d["vol_pct"].quantile([1 / 3, 2 / 3])
    d["vol_regime"] = np.where(d.vol_pct <= q1, "LOW volatility", np.where(d.vol_pct <= q2, "MEDIUM volatility", "HIGH volatility"))
    prior = (d["ref"] / d["ref"].shift(trend_days) - 1) * 100
    d["trend_regime"] = np.where(prior > trend_pct, "UPTREND (20d > +2%)", np.where(prior < -trend_pct, "DOWNTREND (20d < -2%)", "SIDEWAYS"))
    d.loc[prior.isna(), "trend_regime"] = "n/a (warm-up)"
    return d


def regime_table(df: pd.DataFrame, cost: CostModel = None) -> pd.DataFrame:
    cost = cost or CostModel()
    d = add_regimes(df)
    rows = []
    for col in ("vol_regime", "trend_regime"):
        for name, g in d.groupby(col):
            if name.startswith("n/a"):
                continue
            oc = 100 * (g["close"] - g["open"]) / g["open"]
            tr = g[g.bias != "NEUTRAL"]
            net = np.where(tr.bias == "BULLISH", 100 * (tr.close - tr.open) / tr.open, -100 * (tr.close - tr.open) / tr.open) - cost.round_trip_bps / 100
            rows.append({"regime": name, "days": len(g), "bull_T1_stated": g["up1_rate"].mean(), "bull_T1_actual": g["up1_hit"].mean(),
                         "bear_T1_stated": g["dn1_rate"].mean(), "bear_T1_actual": g["dn1_hit"].mean(),
                         "avg_range_pct": float(100 * ((g["high"] - g["low"]) / g["open"]).mean()),
                         "bias_trades": len(tr), "bias_net_pct": float(net.mean()) if len(net) else float("nan")})
    return pd.DataFrame(rows)

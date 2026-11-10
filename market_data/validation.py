"""Candle validation: duplicates, ordering, bad OHLC, out-of-session bars, missing candles."""
from dataclasses import dataclass, field
from datetime import time

import pandas as pd

from config.settings import IST, MARKET_CLOSE, MARKET_OPEN
from market_data.base import COLUMNS, MarketDataError

STEP_MINUTES = {"5m": 5, "15m": 15, "1h": 60}


@dataclass
class ValidationReport:
    rows_in: int = 0
    rows_out: int = 0
    duplicates_removed: int = 0
    null_rows_removed: int = 0
    bad_ohlc_removed: int = 0
    out_of_session_removed: int = 0
    missing_candles: int = 0
    gap_days: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.rows_out > 0 and self.missing_candles == 0


def _expected_slots(day: pd.Timestamp, timeframe: str) -> list:
    step = STEP_MINUTES[timeframe]
    start = pd.Timestamp.combine(day.date(), MARKET_OPEN).tz_localize(IST)
    end = pd.Timestamp.combine(day.date(), MARKET_CLOSE).tz_localize(IST)
    return list(pd.date_range(start, end, freq=f"{step}min", inclusive="left"))


def validate_candles(df: pd.DataFrame, timeframe: str) -> tuple:
    """Return (clean_df, report). Never invents or fills prices."""
    rep = ValidationReport(rows_in=len(df))
    if df is None or df.empty:
        raise MarketDataError("No candles received")
    if df.index.tz is None:
        raise MarketDataError("Candle index must be timezone-aware (IST)")
    missing_cols = [c for c in COLUMNS if c not in df.columns]
    if missing_cols:
        raise MarketDataError(f"Missing columns: {missing_cols}")

    attrs = dict(df.attrs)
    out = df[COLUMNS].copy()
    out.index = out.index.tz_convert(IST)

    n = len(out)
    out = out.dropna(subset=["open", "high", "low", "close"])
    rep.null_rows_removed = n - len(out)
    out["volume"] = out["volume"].fillna(0)

    n = len(out)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    rep.duplicates_removed = n - len(out)

    n = len(out)
    bad = (
        (out["high"] < out[["open", "close", "low"]].max(axis=1))
        | (out["low"] > out[["open", "close", "high"]].min(axis=1))
        | (out[["open", "high", "low", "close"]] <= 0).any(axis=1)
    )
    out = out[~bad]
    rep.bad_ohlc_removed = n - len(out)

    if timeframe in STEP_MINUTES:
        n = len(out)
        t = out.index.time
        in_session = (t >= MARKET_OPEN) & (t < MARKET_CLOSE) & (out.index.dayofweek < 5)
        out = out[in_session]
        rep.out_of_session_removed = n - len(out)

        # Missing candles inside each trading day that has data (holidays have no data, so ignored)
        for day, grp in out.groupby(out.index.normalize()):
            slots = _expected_slots(day, timeframe)
            missing = [s for s in slots if s not in grp.index]
            # last day may be in progress: ignore slots after the latest bar
            missing = [s for s in missing if s < grp.index.max()]
            if missing:
                rep.missing_candles += len(missing)
                rep.gap_days.append((str(day.date()), len(missing)))

    rep.rows_out = len(out)
    out.attrs.update(attrs)
    return out, rep

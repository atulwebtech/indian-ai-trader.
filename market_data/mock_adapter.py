"""Deterministic synthetic candles for development and tests.

These prices are INVENTED by a random walk and always labelled MOCK. They must never
be shown as real market data.
"""
import numpy as np
import pandas as pd

from config.settings import IST, MARKET_CLOSE, MARKET_OPEN
from market_data.base import SOURCE_MOCK, MarketDataAdapter
from market_data.validation import STEP_MINUTES, validate_candles

START_LEVEL = {"NIFTY": 23000.0, "BANKNIFTY": 54000.0}  # arbitrary seeds, not real quotes


class MockAdapter(MarketDataAdapter):
    source = SOURCE_MOCK

    def __init__(self, seed: int = 42, end_date: str = "2026-01-30"):
        self.seed, self.end_date = seed, pd.Timestamp(end_date)

    def get_candles(self, instrument: str, timeframe: str, lookback_days: int) -> pd.DataFrame:
        self.check_args(instrument, timeframe)
        rng = np.random.default_rng(self.seed + (0 if instrument == "NIFTY" else 1))
        days = pd.bdate_range(end=self.end_date, periods=lookback_days)
        price = START_LEVEL[instrument]
        rows = []
        for day in days:
            if timeframe == "1d":
                slots = [day.tz_localize(IST)]
                vol = 0.009
            else:
                start = pd.Timestamp.combine(day.date(), MARKET_OPEN).tz_localize(IST)
                end = pd.Timestamp.combine(day.date(), MARKET_CLOSE).tz_localize(IST)
                step = STEP_MINUTES[timeframe]
                slots = list(pd.date_range(start, end, freq=f"{step}min", inclusive="left"))
                vol = 0.0009 * np.sqrt(step / 5)
            for s in slots:
                o = price
                c = o * (1 + rng.normal(0, vol))
                h = max(o, c) * (1 + abs(rng.normal(0, vol / 3)))
                lo = min(o, c) * (1 - abs(rng.normal(0, vol / 3)))
                rows.append((s, o, h, lo, c, 0))
                price = c
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")
        df, rep = validate_candles(df, timeframe)
        df.attrs.update(source=self.source, instrument=instrument, timeframe=timeframe, report=rep)
        return df

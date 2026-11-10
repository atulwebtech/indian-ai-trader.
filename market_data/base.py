"""Common types for all market-data adapters.

Candles are a pandas DataFrame indexed by tz-aware IST bar-START time with columns
open, high, low, close, volume. `df.attrs["source"]` is one of SOURCE_* below so the
dashboard and alerts can always label where data came from.
"""
from abc import ABC, abstractmethod

import pandas as pd

SOURCE_MOCK = "MOCK"
SOURCE_HISTORICAL = "HISTORICAL"
SOURCE_LIVE = "LIVE"

COLUMNS = ["open", "high", "low", "close", "volume"]
INSTRUMENTS = ("NIFTY", "BANKNIFTY")
TIMEFRAMES = ("5m", "15m", "1h", "1d")


class MarketDataError(Exception):
    """Data could not be fetched or is unusable."""


class MarketDataAdapter(ABC):
    source: str = ""

    @abstractmethod
    def get_candles(self, instrument: str, timeframe: str, lookback_days: int) -> pd.DataFrame:
        """Return validated candles, oldest first."""

    @staticmethod
    def check_args(instrument: str, timeframe: str) -> None:
        if instrument not in INSTRUMENTS:
            raise MarketDataError(f"Unknown instrument {instrument!r}; use one of {INSTRUMENTS}")
        if timeframe not in TIMEFRAMES:
            raise MarketDataError(f"Unknown timeframe {timeframe!r}; use one of {TIMEFRAMES}")

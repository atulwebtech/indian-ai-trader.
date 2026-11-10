"""Historical candles from Yahoo Finance's public chart endpoint.

Unofficial and delayed: labelled HISTORICAL, never LIVE. Index volume is always 0
(indices have no volume), so volume-based analysis must treat it as unavailable.
Yahoo limits: 5m/15m ~60 days, 1h ~730 days, 1d many years.
"""
import logging
import time

import pandas as pd
import requests

from config.settings import IST
from market_data.base import (SOURCE_HISTORICAL, MarketDataAdapter, MarketDataError)
from market_data.validation import validate_candles

log = logging.getLogger(__name__)

SYMBOLS = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}
INTERVALS = {"5m": "5m", "15m": "15m", "1h": "60m", "1d": "1d"}
MAX_DAYS = {"5m": 59, "15m": 59, "1h": 729, "1d": 3650}
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


class YahooHistoricalAdapter(MarketDataAdapter):
    source = SOURCE_HISTORICAL

    def __init__(self, retries: int = 3, timeout: int = 15, session=None):
        self.retries, self.timeout = retries, timeout
        self.session = session or requests.Session()

    def _fetch_json(self, symbol: str, interval: str, days: int) -> dict:
        last_err = None
        for attempt in range(1, self.retries + 1):
            try:
                r = self.session.get(
                    URL.format(symbol=symbol),
                    params={"interval": interval, "range": f"{days}d"},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=self.timeout,
                )
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                last_err = e
                log.warning("Yahoo fetch attempt %d/%d failed: %s", attempt, self.retries, e)
                time.sleep(min(2 ** attempt, 8))
        raise MarketDataError(f"Yahoo unreachable after {self.retries} tries: {last_err}")

    @staticmethod
    def parse(payload: dict) -> pd.DataFrame:
        try:
            result = payload["chart"]["result"]
            if not result:
                raise MarketDataError(f"Yahoo error: {payload['chart'].get('error')}")
            res = result[0]
            q = res["indicators"]["quote"][0]
            idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(IST)
        except (KeyError, IndexError, TypeError) as e:
            raise MarketDataError(f"Unexpected Yahoo response shape: {e}") from e
        df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close", "volume")}, index=idx)
        return df

    def get_candles(self, instrument: str, timeframe: str, lookback_days: int) -> pd.DataFrame:
        self.check_args(instrument, timeframe)
        days = min(lookback_days, MAX_DAYS[timeframe])
        if days < lookback_days:
            log.warning("%s limits %s data to %d days; requested %d", "Yahoo", timeframe, days, lookback_days)
        payload = self._fetch_json(SYMBOLS[instrument], INTERVALS[timeframe], days)
        df = self.parse(payload)
        if timeframe == "1d":
            # daily bars are stamped at 09:15 IST; normalise to the date
            df.index = df.index.normalize()
        df, rep = validate_candles(df, timeframe)
        if rep.missing_candles:
            log.warning("%s %s: %d missing candles on %s", instrument, timeframe, rep.missing_candles, rep.gap_days[:5])
        df.attrs.update(source=self.source, instrument=instrument, timeframe=timeframe, report=rep)
        return df

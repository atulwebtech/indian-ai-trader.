"""Historical candles from Groww's documented get_historical_candles().

Documented request limits (Backtesting page): 1-5 min = 30 days per request, 10-30 min = 90 days,
1 hour and 1 day = 180 days. Candle rows are [timestamp, open, high, low, close, volume, open_interest].
Index (CASH) candles may carry no volume; the nearest-month index future (FNO) does, and can be
merged in with attach_futures_volume().
"""
import logging
import time
from datetime import datetime, timedelta

import pandas as pd

from config.settings import IST
from groww_integration.instruments import EXPECTED, load_master, nearest_future, validate_indices
from market_data.base import SOURCE_LIVE, MarketDataAdapter, MarketDataError
from market_data.validation import validate_candles

log = logging.getLogger(__name__)
INTERVAL_CONST = {"5m": "CANDLE_INTERVAL_MIN_5", "15m": "CANDLE_INTERVAL_MIN_15",
                  "1h": "CANDLE_INTERVAL_HOUR_1", "1d": "CANDLE_INTERVAL_DAY"}
MAX_DAYS = {"5m": 30, "15m": 90, "1h": 180, "1d": 180}
PAUSE_BETWEEN_CALLS = 0.2   # stays far under the documented 10 requests/second


def parse_candles(payload: dict, daily: bool = False) -> pd.DataFrame:
    """Groww response -> DataFrame indexed by IST bar-start time. Accepts epoch seconds/ms or text stamps."""
    try:
        rows = payload["candles"]
    except (KeyError, TypeError) as e:
        raise MarketDataError(f"Unexpected Groww candle response: {e}") from e
    idx, data = [], []
    for r in rows:
        ts = r[0]
        if isinstance(ts, (int, float)):
            t = pd.Timestamp(ts, unit="ms" if ts > 1e11 else "s", tz="UTC").tz_convert(IST)
        else:
            t = pd.Timestamp(str(ts).replace("T", " "))
            t = t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)
        idx.append(t)
        vol = r[5] if len(r) > 5 and r[5] is not None else 0
        oi = r[6] if len(r) > 6 and r[6] is not None else 0
        data.append((r[1], r[2], r[3], r[4], vol, oi))
    df = pd.DataFrame(data, index=pd.DatetimeIndex(idx), columns=["open", "high", "low", "close", "volume", "oi"])
    # A candle with a missing or non-positive price is a vendor placeholder, not a market print: drop it, never use it.
    ohlc = df[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    df = df[(ohlc > 0).all(axis=1)]
    if daily:
        df.index = df.index.normalize()
    return df


class GrowwHistoricalAdapter(MarketDataAdapter):
    source = SOURCE_LIVE

    def __init__(self, session, sleep=time.sleep, today=None):
        self.session, self.sleep, self.today = session, sleep, today
        self._master = None
        self._indices = None

    def _ensure_master(self):
        if self._indices is None:
            self._master = load_master(self.session.client)
            self._indices = validate_indices(self._master)
        return self._indices

    def _fetch(self, groww_symbol, segment, timeframe, start: datetime, end: datetime) -> pd.DataFrame:
        g = self.session.client
        const = getattr(g, INTERVAL_CONST[timeframe])
        chunk = timedelta(days=MAX_DAYS[timeframe])
        parts, cur = [], start
        while cur < end:
            nxt = min(cur + chunk, end)
            payload = self.session.call(
                "get_historical_candles", exchange=g.EXCHANGE_NSE,
                segment=getattr(g, "SEGMENT_CASH" if segment == "CASH" else "SEGMENT_FNO"),
                groww_symbol=groww_symbol, start_time=cur.strftime("%Y-%m-%d %H:%M:%S"),
                end_time=nxt.strftime("%Y-%m-%d %H:%M:%S"), candle_interval=const)
            parts.append(parse_candles(payload, daily=timeframe == "1d"))
            cur = nxt
            self.sleep(PAUSE_BETWEEN_CALLS)
        if not parts:
            raise MarketDataError("No candle request was made")
        df = pd.concat(parts)
        return df[~df.index.duplicated(keep="last")].sort_index()

    def get_candles(self, instrument: str, timeframe: str, lookback_days: int, with_futures_volume: bool = True) -> pd.DataFrame:
        self.check_args(instrument, timeframe)
        idx = self._ensure_master()[instrument]
        end = datetime.now(IST).replace(tzinfo=None).replace(second=0, microsecond=0)
        start = (end - timedelta(days=lookback_days)).replace(hour=9, minute=15, second=0)
        df = self._fetch(idx["groww_symbol"], "CASH", timeframe, start, end)
        df = df.drop(columns=["oi"])
        df.attrs.update(volume_source="none (spot index)")
        if with_futures_volume and timeframe != "1d" and not (df["volume"] > 0).any():
            try:
                df = self.attach_futures_volume(df, instrument, timeframe, start, end)
            except Exception as e:     # volume is optional; never fail the whole fetch because of it
                log.warning("Futures volume unavailable for %s: %s", instrument, e)
        df, rep = validate_candles(df, timeframe)
        if rep.missing_candles:
            log.warning("%s %s: %d missing candles", instrument, timeframe, rep.missing_candles)
        df.attrs.update(source=self.source, instrument=instrument, timeframe=timeframe, report=rep,
                        volume_source=df.attrs.get("volume_source", "none (spot index)"))
        return df

    def attach_futures_volume(self, df, instrument, timeframe, start, end):
        fut = nearest_future(self._master, instrument, self.today)
        if fut is None:
            raise MarketDataError("No unexpired index future found in the instrument master")
        fstart = max(start, end - timedelta(days=MAX_DAYS[timeframe] * 3))
        fdf = self._fetch(fut["groww_symbol"], "FNO", timeframe, fstart, end)
        out = df.copy()
        vol = fdf["volume"].reindex(out.index)
        out["volume"] = vol.fillna(0)
        out.attrs.update(volume_source=f"index future {fut['trading_symbol']} (volume only where it traded)")
        return out

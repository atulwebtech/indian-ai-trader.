"""Build candles from live ticks for 1/3/5/15/30 minutes, aligned to the NSE open (09:15 IST).

Rules: a candle exists only if at least one tick arrived in it (nothing is invented). A candle is CLOSED once a tick
from a later bucket arrives or flush(now) passes its end. Skipped buckets are counted as gaps. Late/out-of-order ticks
and ticks outside the session are counted and ignored. Each tick is processed once (duplicate timestamps with the same
price are dropped).
"""
from collections import deque
from datetime import datetime, time, timedelta

import pandas as pd

from config.settings import IST, MARKET_CLOSE, MARKET_OPEN

TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30}


def bucket_start(ts: datetime, minutes: int) -> datetime:
    open_dt = ts.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0)
    n = int((ts - open_dt).total_seconds() // (60 * minutes))
    return open_dt + timedelta(minutes=n * minutes)


class CandleBuilder:
    def __init__(self, timeframes=("1m", "3m", "5m", "15m", "30m"), keep: int = 600):
        for tf in timeframes:
            if tf not in TF_MINUTES:
                raise ValueError(f"Unsupported timeframe {tf}")
        self.tfs = tuple(timeframes)
        self.forming, self.closed = {}, {}
        self.keep = keep
        self.gaps, self.late_ticks, self.out_of_session, self.duplicates = {}, 0, 0, 0
        self._last_tick = {}

    # ---- ingest ----
    def on_tick(self, instrument: str, price: float, ts: datetime, volume: float = None) -> list:
        """Process one tick. `ts` must be tz-aware (converted to IST). Returns [(timeframe, closed_candle_dict), ...]."""
        if ts.tzinfo is None:
            raise ValueError("Tick timestamp must be timezone-aware")
        ts = ts.astimezone(IST)
        t = ts.time()
        if not (MARKET_OPEN <= t < MARKET_CLOSE) or ts.weekday() >= 5:
            self.out_of_session += 1
            return []
        last = self._last_tick.get(instrument)
        if last and ts < last[0]:
            self.late_ticks += 1
            return []
        if last and ts == last[0] and price == last[1]:
            self.duplicates += 1
            return []
        self._last_tick[instrument] = (ts, price)
        events = []
        for tf in self.tfs:
            m = TF_MINUTES[tf]
            b = bucket_start(ts, m)
            key = (instrument, tf)
            cur = self.forming.get(key)
            if cur is not None and b > cur["start"]:
                events.append((tf, self._close(key, cur)))
                skipped = int((b - cur["start"]).total_seconds() // (60 * m)) - 1
                if skipped > 0 and b.date() == cur["start"].date():
                    self.gaps[key] = self.gaps.get(key, 0) + skipped
                cur = None
            if cur is None:
                self.forming[key] = {"start": b, "open": price, "high": price, "low": price, "close": price, "ticks": 1}
            else:
                cur["high"], cur["low"], cur["close"] = max(cur["high"], price), min(cur["low"], price), price
                cur["ticks"] += 1
        return events

    def _close(self, key, cur) -> dict:
        c = dict(cur, tf=key[1])
        self.closed.setdefault(key, deque(maxlen=self.keep)).append(c)
        self.forming.pop(key, None)
        return c

    def flush(self, now: datetime) -> list:
        """Close forming candles whose time has ended even if no later tick has arrived."""
        out = []
        for key, cur in list(self.forming.items()):
            if cur["start"] + timedelta(minutes=TF_MINUTES[key[1]]) <= now:
                out.append((key[1], self._close(key, cur)))
        return out

    def seed(self, instrument: str, tf: str, df: pd.DataFrame) -> None:
        """Load already-CLOSED REST candles (index = candle start, IST) so indicators have history at start/after reconnect."""
        dq = self.closed.setdefault((instrument, tf), deque(maxlen=self.keep))
        have = {c["start"] for c in dq}
        rows = [dict(start=i.to_pydatetime(), open=r.open, high=r.high, low=r.low, close=r.close,
                     ticks=0, tf=tf, volume=getattr(r, "volume", None)) for i, r in df.iterrows() if i.to_pydatetime() not in have]
        merged = sorted(list(dq) + rows, key=lambda c: c["start"])
        dq.clear()
        dq.extend(merged)

    # ---- read ----
    def closed_df(self, instrument: str, tf: str) -> pd.DataFrame:
        rows = list(self.closed.get((instrument, tf), []))
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows).set_index("start")
        if "volume" not in df:
            df["volume"] = None
        df.index = pd.DatetimeIndex(df.index)
        return df[["open", "high", "low", "close", "volume"]]

    def missing(self, instrument: str, tf: str) -> int:
        return self.gaps.get((instrument, tf), 0)

"""Live index feed using the documented GrowwFeed.subscribe_index_value() / get_index_value() / consume().

Supervised: reconnects with exponential backoff, tracks tick age, flags stale data, and reports a
status dict for the dashboard and Telegram. The documentation does not describe the shape of the
index payload or any built-in reconnect, so (a) the parser searches the payload defensively and
reports UNPARSED instead of inventing a price, and (b) reconnection is our own responsibility.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)
STABLE_SECONDS = 30.0
VALUE_KEYS = ("value", "ltp", "indexValue", "index_value", "lastPrice", "last_price")
TS_KEYS = ("tsInMillis", "ts", "timestamp")
INDEX_TOKENS = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY"}   # exchange_token == symbol for NSE indices (verified in the master)


def parse_index_payload(payload, wanted=INDEX_TOKENS):
    """Walk the payload; return {instrument: (price, ts_ms_or_None)} for tokens found, else {}."""
    found = {}

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                for inst, token in wanted.items():
                    if str(k) == token and isinstance(v, dict):
                        price = next((v[x] for x in VALUE_KEYS if isinstance(v.get(x), (int, float))), None)
                        ts = next((v[x] for x in TS_KEYS if isinstance(v.get(x), (int, float))), None)
                        if price is not None:
                            found[inst] = (float(price), ts)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(payload)
    return found


class LiveIndexFeed:
    def __init__(self, groww_api, instruments=("NIFTY", "BANKNIFTY"), feed_cls=None, clock=time.time,
                 sleep=time.sleep, stale_after=30.0, max_backoff=60.0, market_open=lambda: True):
        self.groww_api, self.instruments = groww_api, tuple(instruments)
        self._feed_cls, self.clock, self.sleep = feed_cls, clock, sleep
        self.stale_after, self.max_backoff, self.market_open = stale_after, max_backoff, market_open
        self.ticks, self.errors, self.reconnects = {}, 0, 0
        self.last_error, self.unparsed_logged = None, False
        self.connected, self._stop = False, threading.Event()
        self._thread, self._feed = None, None
        self._lock = threading.Lock()

    def _class(self):
        if self._feed_cls is None:
            from growwapi import GrowwFeed
            self._feed_cls = GrowwFeed
        return self._feed_cls

    def _subscription(self):
        return [{"exchange": "NSE", "segment": "CASH", "exchange_token": INDEX_TOKENS[i]} for i in self.instruments]

    def _on_data(self, meta=None):
        try:
            payload = self._feed.get_index_value()
        except Exception as e:
            self.last_error = f"get_index_value failed: {type(e).__name__}"
            return
        got = parse_index_payload(payload, {i: INDEX_TOKENS[i] for i in self.instruments})
        if not got and not self.unparsed_logged:
            self.unparsed_logged = True
            log.warning("Index payload not understood; top-level keys: %s", list(payload)[:5] if isinstance(payload, dict) else type(payload))
        now = self.clock()
        with self._lock:
            for inst, (price, ts) in got.items():
                self.ticks[inst] = {"price": price, "exchange_ts_ms": ts, "received_at": now}

    # ---- supervision ----
    def _run(self):
        backoff, connected_at = 1.0, None
        while not self._stop.is_set():
            try:
                self._feed = self._class()(self.groww_api)
                self._feed.subscribe_index_value(self._subscription(), on_data_received=self._on_data)
                self.connected = True
                connected_at = self.clock()
                self._feed.consume()          # blocking call per the documentation
                self.connected = False
                if self._stop.is_set():
                    break
                self.last_error = "feed.consume() returned; reconnecting"
            except Exception as e:           # connection loss, auth problems, SDK errors
                self.connected = False
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {str(getattr(e, 'msg', e))[:120]}"
                log.warning("Groww feed error: %s", self.last_error)
            if self._stop.is_set():
                break
            self.reconnects += 1
            # only a connection that stayed up for a while earns a reset; flapping keeps backing off
            if connected_at is not None and self.clock() - connected_at >= STABLE_SECONDS:
                backoff = 1.0
            connected_at = None
            self.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="groww-feed", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._feed is not None:
                self._feed.unsubscribe_index_value(self._subscription())
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=5)

    # ---- reading ----
    def latest(self, instrument):
        """(price, age_seconds) or None when nothing arrived or the tick is stale."""
        with self._lock:
            t = self.ticks.get(instrument)
        if not t:
            return None
        age = self.clock() - t["received_at"]
        if age > self.stale_after and self.market_open():
            return None
        return t["price"], age

    def status(self) -> dict:
        ages = {i: (self.clock() - t["received_at"]) for i, t in self.ticks.items()}
        stale = [i for i in self.instruments if i not in ages or ages[i] > self.stale_after] if self.market_open() else []
        return {"connected": self.connected, "stale": stale, "tick_age_seconds": ages, "errors": self.errors,
                "reconnects": self.reconnects, "last_error": self.last_error}

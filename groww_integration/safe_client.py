"""Read-only wrapper around the Groww SDK client.

Only the methods listed in ALLOWED can be called through it. Anything else, in particular every
order-related method (place_order, modify_order, cancel_order, ...), raises PermissionError.
This is the technical guarantee behind 'orders are disabled'.
"""
ALLOWED = frozenset({
    "get_all_instruments", "get_instrument_by_groww_symbol", "get_instrument_by_exchange_and_trading_symbol",
    "get_instrument_by_exchange_token", "get_historical_candles", "get_ltp", "get_ohlc", "get_quote",
    # read-only option data (never orders)
    "get_option_chain", "get_expiries", "get_greeks"})
CONSTANT_PREFIXES = ("EXCHANGE_", "SEGMENT_", "CANDLE_INTERVAL_")
CONSTANT_NAMES = frozenset({"INSTRUMENT_CSV_URL"})


class SafeGroww:
    def __init__(self, client):
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name):
        if name in ALLOWED or name in CONSTANT_NAMES or name.startswith(CONSTANT_PREFIXES):
            return getattr(self._client, name)
        raise PermissionError(f"Groww method '{name}' is disabled: this project is read-only and never places orders.")

    def __setattr__(self, name, value):
        raise PermissionError("SafeGroww is immutable")

    def unwrap_for_feed(self):
        """The raw client is needed only to construct GrowwFeed (market data streaming)."""
        return self._client

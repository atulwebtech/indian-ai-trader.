"""Groww-backed adapter (DATA_MODE=LIVE). Needs credentials in .env; read-only, never trades."""
from config.settings import load_settings
from market_data.base import MarketDataAdapter, MarketDataError


class GrowwAdapter(MarketDataAdapter):
    source = "LIVE"

    def __init__(self, historical=None):
        self._hist = historical

    def _adapter(self):
        if self._hist is None:
            from groww_integration.auth import GrowwAuthError, GrowwSession
            from groww_integration.historical import GrowwHistoricalAdapter
            s = load_settings()
            try:
                self._hist = GrowwHistoricalAdapter(GrowwSession(s.groww_api_key, s.groww_api_secret, s.groww_totp_secret))
            except GrowwAuthError as e:
                raise MarketDataError(str(e)) from e
        return self._hist

    def get_candles(self, instrument, timeframe, lookback_days):
        from groww_integration.auth import GrowwAuthError
        from groww_integration.instruments import InstrumentError
        self.check_args(instrument, timeframe)
        try:
            return self._adapter().get_candles(instrument, timeframe, lookback_days)
        except (GrowwAuthError, InstrumentError) as e:
            raise MarketDataError(str(e)) from e

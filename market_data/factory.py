from market_data.base import MarketDataAdapter, MarketDataError
from market_data.groww_adapter import GrowwAdapter
from market_data.mock_adapter import MockAdapter
from market_data.yahoo_adapter import YahooHistoricalAdapter


def get_adapter(data_mode: str) -> MarketDataAdapter:
    if data_mode == "MOCK":
        return MockAdapter()
    if data_mode == "HISTORICAL":
        return YahooHistoricalAdapter()
    if data_mode == "LIVE":
        return GrowwAdapter()
    raise MarketDataError(f"Unknown data mode {data_mode!r}")

"""Save/merge candles on disk so the short Yahoo 5m/15m window grows over time."""
import pandas as pd

from config.settings import IST

from config.settings import ROOT
from market_data.validation import validate_candles

CACHE_DIR = ROOT / "data"


def _path(instrument: str, timeframe: str):
    return CACHE_DIR / f"{instrument}_{timeframe}.csv"


def load_cached(instrument: str, timeframe: str):
    p = _path(instrument, timeframe)
    if not p.exists():
        return None
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(IST)
    return df


def save_merged(df: pd.DataFrame, instrument: str, timeframe: str) -> pd.DataFrame:
    """Merge new candles into the cache (new data wins on duplicates) and return the union."""
    old = load_cached(instrument, timeframe)
    df = df.copy()
    df.index = df.index.tz_convert(IST)  # same tz type as the cache, or concat breaks the index
    merged = df if old is None else pd.concat([old, df])
    merged, _ = validate_candles(merged, timeframe)
    CACHE_DIR.mkdir(exist_ok=True)
    merged.to_csv(_path(instrument, timeframe))
    merged.attrs.update(df.attrs)
    return merged

"""Volume analysis. Index candles (NIFTY / BANKNIFTY spot) carry no volume, so every
function reports `available=False` instead of producing fake numbers."""
import pandas as pd


def has_volume(df: pd.DataFrame) -> bool:
    return bool((df["volume"] > 0).any())


def relative_volume(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Volume divided by the average of the previous `window` bars (excludes current bar)."""
    base = df["volume"].shift().rolling(window).mean()
    return df["volume"] / base.where(base > 0)


def volume_summary(df: pd.DataFrame, breakout_rvol: float = 1.5) -> dict:
    if not has_volume(df):
        return {"available": False, "reason": "Spot index has no volume; needs futures data (Groww)."}
    rv = relative_volume(df).iloc[-1]
    return {"available": True, "relative_volume": None if pd.isna(rv) else float(rv),
            "volume_breakout": bool(pd.notna(rv) and rv >= breakout_rvol)}

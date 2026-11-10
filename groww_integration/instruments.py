"""Instrument master handling and NIFTY / BANKNIFTY validation.

Nothing is hard-coded as a token. The expected rows are looked up in Groww's instrument master
(groww.get_all_instruments() or the public CSV at groww.INSTRUMENT_CSV_URL) and must match:
exchange NSE, segment CASH, instrument_type IDX, groww_symbol NSE-NIFTY / NSE-BANKNIFTY.
"""
from datetime import date

import pandas as pd

EXPECTED = {"NIFTY": "NSE-NIFTY", "BANKNIFTY": "NSE-BANKNIFTY"}
REQUIRED_COLUMNS = ("exchange", "exchange_token", "trading_symbol", "groww_symbol", "instrument_type", "segment")


class InstrumentError(Exception):
    """The instrument master does not contain what we need."""


def load_master(groww) -> pd.DataFrame:
    """Instrument master as a DataFrame via the documented get_all_instruments()."""
    df = groww.get_all_instruments()
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise InstrumentError("Groww returned an empty instrument master")
    return df


def validate_indices(df: pd.DataFrame) -> dict:
    """Return {'NIFTY': {...row...}, 'BANKNIFTY': {...}} or raise InstrumentError naming what is wrong."""
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        raise InstrumentError(f"Instrument master is missing columns {missing_cols}; its format may have changed")
    out = {}
    for name, gsym in EXPECTED.items():
        rows = df[(df["exchange"] == "NSE") & (df["segment"] == "CASH") & (df["instrument_type"] == "IDX")
                  & (df["groww_symbol"] == gsym)]
        if len(rows) != 1:
            raise InstrumentError(f"Expected exactly one NSE index row for {gsym}, found {len(rows)}. "
                                  f"Not guessing a replacement; check Groww's instrument list.")
        out[name] = {k: str(rows.iloc[0][k]) for k in REQUIRED_COLUMNS}
    return out


def nearest_future(df: pd.DataFrame, underlying: str, today: date = None):
    """The index future with the earliest expiry that has not expired yet (None if none listed)."""
    today = today or date.today()
    f = df[(df["exchange"] == "NSE") & (df["segment"] == "FNO") & (df["instrument_type"] == "FUT")
           & (df["underlying_symbol"] == underlying)].copy()
    if f.empty:
        return None
    f["_exp"] = pd.to_datetime(f["expiry_date"], errors="coerce").dt.date
    f = f[f["_exp"] >= today].sort_values("_exp")
    if f.empty:
        return None
    r = f.iloc[0]
    return {"groww_symbol": str(r["groww_symbol"]), "trading_symbol": str(r["trading_symbol"]),
            "expiry": str(r["_exp"]), "lot_size": int(r["lot_size"]) if pd.notna(r["lot_size"]) else None}

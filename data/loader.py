"""Tick data loader.

Supports the RPT tick CSV layout
(trading_date, contract, time, price, quantity) and a generic
(timestamp, price, volume) layout. Returns a DataFrame with columns
[timestamp, price, volume] suitable for Environment.
"""
from pathlib import Path
from typing import Optional, Union

import pandas as pd


def load_rpt_ticks(
    path: Union[str, Path],
    *,
    contract: Optional[str] = None,
    nrows: Optional[int] = None,
) -> pd.DataFrame:
    """Load the RPT tick CSV (trading_date, contract, time, price, quantity)."""
    # Tech: read the CSV forcing trading_date/time/contract to strings, then check
    #       the four required columns are present.
    # Why:  date/time fields must stay strings — pandas would otherwise strip the
    #       leading zeros that the fixed-width HHMMSSff parsing below depends on; the
    #       column check fails fast with the exact missing set on a malformed file.
    df = pd.read_csv(path, nrows=nrows, dtype={"trading_date": str, "time": str, "contract": str})
    required = {"trading_date", "time", "price", "quantity"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"RPT tick CSV missing columns: {missing}")

    # Tech: optionally filter to a single contract.
    # Why:  one file can hold several contract months; a backtest should run on one
    #       near-month series (SPEC §10 defers continuous-contract stitching).
    if contract is not None and "contract" in df.columns:
        df = df[df["contract"] == contract]

    # time field is HHMMSSff (8 chars). Pad if needed.
    # Tech: zero-pad the time to 8 chars, concatenate date+time, and parse the fixed
    #       format into a real timestamp.
    # Why:  early-morning times can lose their leading zero, breaking the fixed-width
    #       parse; padding restores the HHMMSSff shape so a single format string
    #       reliably yields datetimes.
    t = df["time"].str.zfill(8)
    ts_str = df["trading_date"] + t
    timestamp = pd.to_datetime(ts_str, format="%Y%m%d%H%M%S%f")

    # Tech: project to the canonical [timestamp, price, volume] schema with coerced
    #       dtypes, then sort chronologically and reindex.
    # Why:  Environment requires exactly these columns in time order; quantity is
    #       renamed to volume so the rest of the framework sees one tick schema
    #       regardless of which loader produced it.
    out = pd.DataFrame({
        "timestamp": timestamp.values,
        "price": df["price"].astype(float).values,
        "volume": df["quantity"].astype(int).values,
    })
    return out.sort_values("timestamp").reset_index(drop=True)


def load_ticks(path: Union[str, Path], *, nrows: Optional[int] = None) -> pd.DataFrame:
    """Generic loader: expects columns timestamp, price, volume."""
    # Tech: read the CSV, verify the three canonical columns exist, coerce the
    #       timestamp to datetime, and return it sorted chronologically.
    # Why:  this is the pass-through path for data already in the framework's schema;
    #       it still sorts because Environment's whole model assumes time order, and
    #       upstream files aren't guaranteed to be sorted.
    df = pd.read_csv(path, nrows=nrows)
    required = {"timestamp", "price", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"tick CSV missing columns: {missing}")
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)

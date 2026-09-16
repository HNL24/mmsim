"""Canonical event schema.

Every loader emits one time-ordered stream of these records. Prices are integer
ticks, quantities are integer base units, timestamps are integer nanoseconds
since epoch. Floats never appear here.
"""

from __future__ import annotations

from dataclasses import astuple, dataclass
from typing import Final

import numpy as np
import pandas as pd
import pyarrow as pa

# ``kind`` values
LEVEL_SET: Final[int] = 0
TRADE: Final[int] = 1

# ``side`` / ``aggressor`` values
BID: Final[int] = 1  # bid side / buy
ASK: Final[int] = -1  # ask side / sell
NO_AGGRESSOR: Final[int] = 0

EVENT_COLUMNS: Final[tuple[str, ...]] = (
    "ts_ns",
    "seq",
    "kind",
    "side",
    "price_ticks",
    "qty",
    "aggressor",
)

EVENT_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("ts_ns", pa.int64(), nullable=False),
        pa.field("seq", pa.int64(), nullable=False),
        pa.field("kind", pa.uint8(), nullable=False),
        pa.field("side", pa.int8(), nullable=False),
        pa.field("price_ticks", pa.int64(), nullable=False),
        pa.field("qty", pa.int64(), nullable=False),
        pa.field("aggressor", pa.int8(), nullable=False),
    ]
)

EVENT_DTYPES: Final[dict[str, str]] = {
    "ts_ns": "int64",
    "seq": "int64",
    "kind": "uint8",
    "side": "int8",
    "price_ticks": "int64",
    "qty": "int64",
    "aggressor": "int8",
}


@dataclass(frozen=True, slots=True)
class Event:
    """One canonical book/trade event.

    Units: ``ts_ns`` nanoseconds since epoch; ``price_ticks`` integer ticks;
    ``qty`` integer base units (shares). For ``LEVEL_SET`` ``qty`` is the new
    resting quantity at the level (0 = level removed); for ``TRADE`` it is the
    traded quantity. ``aggressor`` is ``BID``/``ASK`` for trades, ``NO_AGGRESSOR``
    otherwise.
    """

    ts_ns: int
    seq: int
    kind: int
    side: int
    price_ticks: int
    qty: int
    aggressor: int = NO_AGGRESSOR

    def __post_init__(self) -> None:
        if self.kind not in (LEVEL_SET, TRADE):
            raise ValueError(f"kind must be LEVEL_SET or TRADE, got {self.kind}")
        if self.side not in (BID, ASK):
            raise ValueError(f"side must be +1 or -1, got {self.side}")
        if self.qty < 0:
            raise ValueError(f"qty must be non-negative, got {self.qty}")
        if self.kind == TRADE:
            if self.aggressor not in (BID, ASK):
                raise ValueError("TRADE events must set aggressor to +1 or -1")
            if self.qty == 0:
                raise ValueError("TRADE events must have qty > 0")
        elif self.aggressor != NO_AGGRESSOR:
            raise ValueError("LEVEL_SET events must have aggressor == 0")


def validate_events(df: pd.DataFrame) -> None:
    """Raise ``ValueError`` if ``df`` is not a valid canonical event stream.

    Checks column set and dtypes, non-decreasing ``ts_ns``, strictly increasing
    ``seq``, and the per-row invariants enforced by :class:`Event`.
    """
    missing = [c for c in EVENT_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    for col, dtype in EVENT_DTYPES.items():
        if str(df[col].dtype) != dtype:
            raise ValueError(f"column {col!r} has dtype {df[col].dtype}, expected {dtype}")
    if len(df) == 0:
        return
    ts = df["ts_ns"].to_numpy()
    seq = df["seq"].to_numpy()
    if np.any(np.diff(ts) < 0):
        raise ValueError("ts_ns must be non-decreasing")
    if np.any(np.diff(seq) <= 0):
        raise ValueError("seq must be strictly increasing")
    kind = df["kind"].to_numpy()
    side = df["side"].to_numpy()
    qty = df["qty"].to_numpy()
    agg = df["aggressor"].to_numpy()
    if not np.all(np.isin(kind, (LEVEL_SET, TRADE))):
        raise ValueError("kind must be 0 (LEVEL_SET) or 1 (TRADE)")
    if not np.all(np.isin(side, (BID, ASK))):
        raise ValueError("side must be +1 or -1")
    if np.any(qty < 0):
        raise ValueError("qty must be non-negative")
    is_trade = kind == TRADE
    if not np.all(np.isin(agg[is_trade], (BID, ASK))):
        raise ValueError("TRADE rows must have aggressor +1 or -1")
    if np.any(qty[is_trade] == 0):
        raise ValueError("TRADE rows must have qty > 0")
    if np.any(agg[~is_trade] != NO_AGGRESSOR):
        raise ValueError("LEVEL_SET rows must have aggressor == 0")


def events_to_frame(events: list[Event]) -> pd.DataFrame:
    """Build a canonical, dtype-correct ``DataFrame`` from a list of events."""
    if not events:
        return pd.DataFrame({c: pd.Series(dtype=d) for c, d in EVENT_DTYPES.items()})
    df = pd.DataFrame([astuple(e) for e in events], columns=list(EVENT_COLUMNS))
    return df.astype(EVENT_DTYPES)

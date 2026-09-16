"""Source-specific loaders that emit the canonical event stream.

Only LOBSTER is implemented. Floats are converted to integer ticks / integer
nanoseconds here and nowhere else.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from mm.events import ASK, BID, EVENT_COLUMNS, EVENT_DTYPES, LEVEL_SET, TRADE

# LOBSTER message types
LOBSTER_SUBMIT = 1
LOBSTER_PARTIAL_CANCEL = 2
LOBSTER_DELETE = 3
LOBSTER_EXEC_VISIBLE = 4
LOBSTER_EXEC_HIDDEN = 5
LOBSTER_CROSS = 6
LOBSTER_HALT = 7

# Dummy prices LOBSTER writes for unoccupied levels (ask / bid respectively).
LOBSTER_ASK_SENTINEL = 9_999_999_999
LOBSTER_BID_SENTINEL = -9_999_999_999

_NS_PER_S = 1_000_000_000


def lobster_time_to_ns(time_str: str, midnight_ns: int) -> int:
    """Convert a LOBSTER 'seconds after midnight' decimal string to int ns since epoch.

    Units: input seconds (decimal string, up to 9 places); output nanoseconds.
    Parsed as text so that no float rounding can occur.
    """
    sec, _, frac = time_str.partition(".")
    if len(frac) > 9:
        raise ValueError(f"LOBSTER time {time_str!r} has more than ns precision")
    return midnight_ns + int(sec) * _NS_PER_S + int(frac.ljust(9, "0") or "0")


def _read_message_file(path: Path, midnight_ns: int) -> pd.DataFrame:
    df = pd.read_csv(
        path,
        header=None,
        names=["time", "type", "order_id", "size", "price", "direction"],
        dtype={
            "time": str,
            "type": np.int8,
            "order_id": np.int64,
            "size": np.int64,
            "price": np.int64,
            "direction": np.int8,
        },
    )
    df["ts_ns"] = np.fromiter(
        (lobster_time_to_ns(t, midnight_ns) for t in df["time"]), dtype=np.int64, count=len(df)
    )
    return df


def _read_orderbook_file(path: Path, levels: int) -> np.ndarray:
    arr = pd.read_csv(path, header=None, dtype=np.int64).to_numpy()
    if arr.shape[1] != 4 * levels:
        raise ValueError(f"orderbook file has {arr.shape[1]} columns, expected {4 * levels}")
    return arr


def _row_levels(row: np.ndarray, side: int, units_per_tick: int) -> dict[int, int]:
    """Occupied levels on one side of a LOBSTER orderbook row as {price_ticks: qty}."""
    offset = 0 if side == ASK else 2
    prices = row[offset::4]
    sizes = row[offset + 1 :: 4]
    sentinel = LOBSTER_ASK_SENTINEL if side == ASK else LOBSTER_BID_SENTINEL
    out: dict[int, int] = {}
    for p, s in zip(prices.tolist(), sizes.tolist(), strict=True):
        if p == sentinel or s <= 0:
            continue
        if p % units_per_tick:
            raise ValueError(f"book price {p} is not on the tick grid ({units_per_tick})")
        out[p // units_per_tick] = s
    return out


def load_lobster(
    message_path: str | Path,
    orderbook_path: str | Path,
    midnight_ns: int,
    levels: int = 10,
    units_per_tick: int = 100,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Build the canonical event stream from a LOBSTER message/orderbook pair.

    Args:
        message_path, orderbook_path: LOBSTER CSVs for one ticker-day.
        midnight_ns: local midnight of the trading day, ns since epoch.
        levels: number of book levels in the files.
        units_per_tick: LOBSTER price units (1e-4 USD) per tick; 100 = $0.01.

    Returns:
        (events, events_per_row) where ``events`` is the canonical DataFrame and
        ``events_per_row[i]`` is the number of events emitted for message row ``i``
        (used by replay validation to align with orderbook rows).

    Emission rules per message row ``i`` (orderbook row ``i`` is the state *after*
    the message):
      * type 4/5 (execution): a ``TRADE`` first. ``side`` is the book side that was
        hit (LOBSTER direction of the resting order); ``aggressor`` is the opposite.
      * then one ``LEVEL_SET`` per level whose quantity differs from row ``i-1``
        (row -1 is an empty book, so row 0 emits the full initial snapshot).
        A level that leaves the visible window is emitted with ``qty = 0``.
      * hidden executions (type 5) at prices off the tick grid (midpoint prints)
        are dropped; they cannot be represented in integer ticks.
      * halts (type 7) and crosses (type 6) are not handled; their presence raises.
    """
    msgs = _read_message_file(Path(message_path), midnight_ns)
    book = _read_orderbook_file(Path(orderbook_path), levels)
    if len(msgs) != len(book):
        raise ValueError(f"message rows ({len(msgs)}) != orderbook rows ({len(book)})")
    unsupported = msgs["type"].isin([LOBSTER_CROSS, LOBSTER_HALT])
    if unsupported.any():
        raise ValueError(f"{int(unsupported.sum())} cross/halt messages present; not supported")

    n = len(msgs)
    ts_all = msgs["ts_ns"].to_numpy()
    types = msgs["type"].to_numpy()
    sizes = msgs["size"].to_numpy()
    prices = msgs["price"].to_numpy()
    dirs = msgs["direction"].to_numpy()

    # Rows where each side's visible block changed vs. the previous row.
    ask_block = book[:, 0::4].copy()
    ask_block = np.stack([ask_block, book[:, 1::4]], axis=2)
    bid_block = np.stack([book[:, 2::4], book[:, 3::4]], axis=2)
    ask_changed = np.ones(n, dtype=bool)
    bid_changed = np.ones(n, dtype=bool)
    ask_changed[1:] = np.any(ask_block[1:] != ask_block[:-1], axis=(1, 2))
    bid_changed[1:] = np.any(bid_block[1:] != bid_block[:-1], axis=(1, 2))

    ts_l: list[int] = []
    kind_l: list[int] = []
    side_l: list[int] = []
    price_l: list[int] = []
    qty_l: list[int] = []
    agg_l: list[int] = []
    per_row = np.zeros(n, dtype=np.int64)

    prev: dict[int, dict[int, int]] = {BID: {}, ASK: {}}
    for i in range(n):
        ts = int(ts_all[i])
        count = 0
        t = int(types[i])
        if t in (LOBSTER_EXEC_VISIBLE, LOBSTER_EXEC_HIDDEN):
            p = int(prices[i])
            if p % units_per_tick == 0:
                resting = int(dirs[i])
                ts_l.append(ts)
                kind_l.append(TRADE)
                side_l.append(resting)
                price_l.append(p // units_per_tick)
                qty_l.append(int(sizes[i]))
                agg_l.append(-resting)
                count += 1
            elif t == LOBSTER_EXEC_VISIBLE:
                raise ValueError(f"visible execution at off-grid price {p} (row {i})")
        for side, changed in ((BID, bid_changed), (ASK, ask_changed)):
            if not changed[i]:
                continue
            cur = _row_levels(book[i], side, units_per_tick)
            old = prev[side]
            for price in sorted(set(old) | set(cur), reverse=(side == BID)):
                new_qty = cur.get(price, 0)
                if old.get(price, 0) != new_qty:
                    ts_l.append(ts)
                    kind_l.append(LEVEL_SET)
                    side_l.append(side)
                    price_l.append(price)
                    qty_l.append(new_qty)
                    agg_l.append(0)
                    count += 1
            prev[side] = cur
        per_row[i] = count

    events = pd.DataFrame(
        {
            "ts_ns": np.asarray(ts_l, dtype=np.int64),
            "seq": np.arange(len(ts_l), dtype=np.int64),
            "kind": np.asarray(kind_l, dtype=np.uint8),
            "side": np.asarray(side_l, dtype=np.int8),
            "price_ticks": np.asarray(price_l, dtype=np.int64),
            "qty": np.asarray(qty_l, dtype=np.int64),
            "aggressor": np.asarray(agg_l, dtype=np.int8),
        },
        columns=list(EVENT_COLUMNS),
    ).astype(EVENT_DTYPES)
    return events, per_row

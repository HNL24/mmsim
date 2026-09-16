"""§5.4 replay validation: rebuild the book from events and compare to LOBSTER rows.

Reports top-of-book mismatches (must be zero) and the mismatch rate for deeper levels.
Exit code 1 on any top-level mismatch.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

from mm.config import load_config, local_midnight_ns
from mm.events import BID, LEVEL_SET
from mm.loaders import (
    LOBSTER_ASK_SENTINEL,
    LOBSTER_BID_SENTINEL,
    _read_orderbook_file,
    load_lobster,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/base.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config, base=None)
    ds = cfg["dataset"]
    raw = Path(ds["raw_dir"])
    stem = f"{ds['ticker']}_{ds['date']}_{ds['lobster_start_ms']}_{ds['lobster_end_ms']}"
    levels = int(ds["levels"])
    upt = int(ds["price_unit_per_tick"])
    midnight_ns = local_midnight_ns(ds["date"], ds["utc_offset_hours"])

    events, per_row = load_lobster(
        raw / f"{stem}_message_{levels}.csv",
        raw / f"{stem}_orderbook_{levels}.csv",
        midnight_ns,
        levels=levels,
        units_per_tick=upt,
    )
    book_rows = _read_orderbook_file(raw / f"{stem}_orderbook_{levels}.csv", levels)

    kind = events["kind"].to_numpy()
    side = events["side"].to_numpy()
    price = events["price_ticks"].to_numpy()
    qty = events["qty"].to_numpy()

    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    top_mismatch = 0
    deep_mismatch = np.zeros(levels, dtype=np.int64)  # index 0 = level 1
    deep_total = np.zeros(levels, dtype=np.int64)
    first_bad: tuple[int, str] | None = None

    idx = 0
    for i, count in enumerate(per_row.tolist()):
        for j in range(idx, idx + count):
            if kind[j] != LEVEL_SET:
                continue
            levels_map = bids if side[j] == BID else asks
            if qty[j] == 0:
                levels_map.pop(int(price[j]), None)
            else:
                levels_map[int(price[j])] = int(qty[j])
        idx += count

        row = book_rows[i]
        ask_sorted = sorted(asks.items())
        bid_sorted = sorted(bids.items(), reverse=True)
        for lvl in range(levels):
            src_ask = (int(row[4 * lvl]), int(row[4 * lvl + 1]))
            src_bid = (int(row[4 * lvl + 2]), int(row[4 * lvl + 3]))
            ours_ask = ask_sorted[lvl] if lvl < len(ask_sorted) else None
            ours_bid = bid_sorted[lvl] if lvl < len(bid_sorted) else None
            exp_ask = (
                None if src_ask[0] == LOBSTER_ASK_SENTINEL else (src_ask[0] // upt, src_ask[1])
            )
            exp_bid = (
                None if src_bid[0] == LOBSTER_BID_SENTINEL else (src_bid[0] // upt, src_bid[1])
            )
            deep_total[lvl] += 1
            if ours_ask != exp_ask or ours_bid != exp_bid:
                deep_mismatch[lvl] += 1
                if lvl == 0:
                    top_mismatch += 1
                    if first_bad is None:
                        first_bad = (i, f"ask {ours_ask} vs {exp_ask}, bid {ours_bid} vs {exp_bid}")

    print(f"rows checked: {len(per_row):,}   events: {len(events):,}")
    print(f"top-of-book mismatches: {top_mismatch}")
    for lvl in range(1, levels):
        rate = deep_mismatch[lvl] / max(deep_total[lvl], 1)
        print(f"  level {lvl + 1}: mismatch rate {rate:.6f} ({deep_mismatch[lvl]})")
    if first_bad is not None:
        print(f"first top-level mismatch at row {first_bad[0]}: {first_bad[1]}")
    if top_mismatch:
        sys.exit(1)
    print("OK: zero top-of-book mismatches")


if __name__ == "__main__":
    main()

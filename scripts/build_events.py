"""Build data/processed/<dataset>/events.parquet from raw LOBSTER files (M1)."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from mm.config import load_config, local_midnight_ns
from mm.events import EVENT_SCHEMA, LEVEL_SET, TRADE, validate_events
from mm.loaders import load_lobster


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/base.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config, base=None)
    ds = cfg["dataset"]
    if ds["source"] != "lobster":
        raise SystemExit(f"unsupported source {ds['source']!r}")

    raw = Path(ds["raw_dir"])
    stem = f"{ds['ticker']}_{ds['date']}_{ds['lobster_start_ms']}_{ds['lobster_end_ms']}"
    msg_path = raw / f"{stem}_message_{ds['levels']}.csv"
    ob_path = raw / f"{stem}_orderbook_{ds['levels']}.csv"
    midnight_ns = local_midnight_ns(ds["date"], ds["utc_offset_hours"])

    t0 = time.perf_counter()
    events, _ = load_lobster(
        msg_path,
        ob_path,
        midnight_ns,
        levels=ds["levels"],
        units_per_tick=ds["price_unit_per_tick"],
    )
    validate_events(events)
    out = Path(ds["processed_path"])
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(events, schema=EVENT_SCHEMA, preserve_index=False), out)

    n_trade = int((events["kind"] == TRADE).sum())
    n_level = int((events["kind"] == LEVEL_SET).sum())
    print(f"wrote {out} ({len(events):,} events: {n_level:,} LEVEL_SET, {n_trade:,} TRADE)")
    print(f"ts range {events['ts_ns'].iloc[0]} .. {events['ts_ns'].iloc[-1]}")
    print(f"elapsed {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()

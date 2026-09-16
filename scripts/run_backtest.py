"""One config -> one run. Writes results/<name>/{events,fills}.parquet + meta.json."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd

from mm.config import load_config
from mm.engine import EngineConfig, run, stream_from_frame
from mm.strategy import from_config


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--window", default="evaluation", choices=["calibration", "evaluation"])
    ap.add_argument("--out", default=None, help="results dir (default results/<config stem>)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    name = Path(args.config).stem
    out = Path(args.out or f"results/{name}")
    ecfg = EngineConfig.from_config(cfg, window=args.window)
    quoter = from_config(cfg["strategy"])

    t0 = time.perf_counter()
    events = pd.read_parquet(cfg["dataset"]["processed_path"])
    stream = stream_from_frame(events)
    res = run(stream, quoter, ecfg, name=name)
    elapsed = time.perf_counter() - t0

    out.mkdir(parents=True, exist_ok=True)
    res.events.to_parquet(out / "events.parquet", index=False)
    res.fills.to_parquet(out / "fills.parquet", index=False)
    (out / "meta.json").write_text(json.dumps(res.meta, indent=2))

    tv = ecfg.tick_value_minor
    usd = lambda x2: x2 / 2 / 100  # doubled minor units (half-cents) -> USD  # noqa: E731
    ev, fl = res.events, res.fills
    minutes = (ecfg.end_ns - ecfg.start_ns) / 60e9
    print(
        f"run {name} [{args.window}] : {len(ev):,} events, {len(fl):,} fills "
        f"({len(fl) / minutes:.2f}/min), {elapsed:.1f}s"
    )
    print(f"  total PnL        {usd(ev['pnl_x2'].iloc[-1]):+.2f} USD")
    print(f"  spread capture   {usd(fl['sc_x2'].sum()):+.2f} USD")
    print(f"  inventory carry  {usd(ev['ic_x2'].iloc[-1]):+.2f} USD")
    print(f"  fees             {ev['fees_cum'].iloc[-1] / 100:.2f} USD")
    for h in ecfg.as_horizons_s:
        print(f"  adverse sel {h:>2}s  {usd(fl[f'as_{h}s_x2'].sum()):+.2f} USD")
    q = ev["q"]
    print(
        f"  inventory: final {res.meta['q_final']}, std {q.std():.1f}, max|q| {q.abs().max()}, "
        f"time at cap {res.meta['time_at_cap_ns'] / (ecfg.end_ns - ecfg.start_ns):.1%}, "
        f"post-only rejects {res.meta['n_rejected']}"
    )
    term = res.meta["q_final"] * res.meta["mid_final_x2"] * tv
    print(f"  terminal inventory mark {usd(term):+.2f} USD")
    print(f"  wrote {out}/")


if __name__ == "__main__":
    main()

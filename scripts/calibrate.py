"""Estimate A, k and the imbalance slope beta on the calibration window; write params + figures."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import yaml

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from mm import plotstyle  # noqa: E402
from mm.calibrate import calibrate, fit_imbalance  # noqa: E402
from mm.config import load_config  # noqa: E402
from mm.engine import stream_from_frame  # noqa: E402
from mm.events import LEVEL_SET  # noqa: E402
from mm.vol import realised_vol  # noqa: E402


def _mid_path(stream: list, start_ns: int, end_ns: int) -> tuple[list[int], list[int]]:
    from mm.book import OrderBook

    book, ts, mid = OrderBook(), [], []
    for t, kind, side, price, qty, _ in stream:
        if kind == LEVEL_SET:
            book.apply(side, price, qty)
        if t < start_ns:
            continue
        if t >= end_ns:
            break
        m = book.mid_ticks_x2()
        if m is not None:
            ts.append(t)
            mid.append(m)
    return ts, mid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/base.yaml")
    ap.add_argument("--max-delta", type=int, default=40)
    ap.add_argument("--min-count", type=int, default=30)
    ap.add_argument("--out", default="results/calibration")
    ap.add_argument("--imbalance-horizon-s", type=float, default=5.0)
    args = ap.parse_args()
    cfg = load_config(args.config, base=None)
    w = cfg["windows"]["calibration"]
    start, end = int(w["start_ns"]), int(w["end_ns"])

    stream = stream_from_frame(pd.read_parquet(cfg["dataset"]["processed_path"]))
    cal = calibrate(stream, start, end, args.max_delta, args.min_count)
    ts, mid = _mid_path(stream, start, end)
    sigma_cal = realised_vol(ts, mid, sample_s=1.0)
    imb = fit_imbalance(stream, start, end, horizon_s=args.imbalance_horizon_s, sample_s=1.0)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fit_deltas = cal.deltas[cal.fit_mask]
    params = {
        "A": round(cal.A, 6),
        "k": round(cal.k, 6),
        "r2": round(cal.r2, 4),
        "delta_fit_range_ticks": [int(fit_deltas.min()), int(fit_deltas.max())],
        "sigma_cal": round(sigma_cal, 6),
        "sigma_units": "ticks / sqrt(second)",
        "window": {"start_ns": start, "end_ns": end, "seconds": cal.window_s},
        "n_trades_bid_side": int(cal.counts_bid[0]),
        "n_trades_ask_side": int(cal.counts_ask[0]),
        "min_count": args.min_count,
        "beta_imbalance_ticks": round(imb.beta_ticks, 6),
        "beta_imbalance_intercept_ticks": round(imb.intercept_ticks, 6),
        "beta_imbalance_r2": round(imb.r2, 4),
        "beta_imbalance_horizon_s": imb.horizon_s,
        "beta_imbalance_n": imb.n,
    }
    (out / "params.yaml").write_text(yaml.safe_dump(params, sort_keys=False))
    pd.DataFrame(
        {
            "delta_ticks": cal.deltas,
            "count_bid": cal.counts_bid,
            "count_ask": cal.counts_ask,
            "lambda_per_s": cal.lam,
            "in_fit": cal.fit_mask,
        }
    ).to_csv(out / "intensity.csv", index=False)

    plotstyle.apply()
    fig, ax = plt.subplots(figsize=(7, 4.2))
    d = cal.deltas
    with np.errstate(divide="ignore"):
        ax.plot(
            d, np.log(cal.counts_bid / cal.window_s), "o", ms=5, label="bid side (sell aggressor)"
        )
        ax.plot(
            d, np.log(cal.counts_ask / cal.window_s), "s", ms=5, label="ask side (buy aggressor)"
        )
    xs = np.linspace(fit_deltas.min(), fit_deltas.max(), 50)
    ax.plot(
        xs,
        np.log(cal.A) - cal.k * xs,
        "-",
        color=plotstyle.SERIES[2],
        label=f"fit: ln A − kδ  (A={cal.A:.3g}/s, k={cal.k:.3g}/tick, R²={cal.r2:.2f})",
    )
    ax.set_xlabel("δ  (ticks from mid)")
    ax.set_ylabel("ln λ(δ)   [fills / s]")
    ax.set_title("Fill intensity, calibration window")
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(out / "fit.png")
    plt.close(fig)

    # imbalance -> forward mid move: binned means with the OLS line
    fig, ax = plt.subplots(figsize=(7, 4.2))
    edges = np.linspace(-1, 1, 21)
    centres = 0.5 * (edges[1:] + edges[:-1])
    idx = np.clip(np.digitize(imb.imbalance, edges) - 1, 0, len(centres) - 1)
    means = np.array(
        [
            imb.move_ticks[idx == b].mean() if (idx == b).any() else np.nan
            for b in range(len(centres))
        ]
    )
    counts = np.array([(idx == b).sum() for b in range(len(centres))])
    ax.scatter(
        centres,
        means,
        s=np.clip(counts / counts.max() * 120, 8, 120),
        zorder=3,
        label=f"binned mean (marker size ∝ n, N = {imb.n:,})",
    )
    xs = np.linspace(-1, 1, 50)
    ax.plot(
        xs,
        imb.intercept_ticks + imb.beta_ticks * xs,
        "-",
        color=plotstyle.SERIES[2],
        label=f"OLS: β = {imb.beta_ticks:.2f} ticks per unit imbalance, R² = {imb.r2:.3f}",
    )
    ax.axhline(0, color=plotstyle.INK_2, lw=0.8)
    ax.set_xlabel("touch imbalance  (bid size − ask size) / (bid size + ask size)")
    ax.set_ylabel(f"mid move over next {imb.horizon_s:g} s  (ticks)")
    ax.set_title("Forward mid move vs book imbalance, calibration window")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(out / "imbalance_fit.png")
    plt.close(fig)

    print(
        f"A = {cal.A:.4g} fills/s   k = {cal.k:.4g} /tick   R² = {cal.r2:.3f}   "
        f"fit range δ ∈ [{fit_deltas.min()}, {fit_deltas.max()}] ticks"
    )
    print(
        f"sigma_cal = {sigma_cal:.4g} ticks/sqrt(s)   "
        f"trades: {cal.counts_bid[0]} bid-side, {cal.counts_ask[0]} ask-side"
    )
    print(
        f"beta_imbalance = {imb.beta_ticks:.3g} ticks per unit imbalance over "
        f"{imb.horizon_s:g} s   R² = {imb.r2:.3f}   n = {imb.n}"
    )
    print(f"wrote {out}/params.yaml, intensity.csv, fit.png, imbalance_fit.png")


if __name__ == "__main__":
    main()

"""E1, E2, E4, E5. Writes results/<exp>/summary.csv and figures.

Order matters: E2 chooses gamma on the calibration window; E1/E4/E5 report it on
the evaluation window. Nothing here reads evaluation data to choose a parameter.
"""

from __future__ import annotations

import argparse
import copy
import math
import shutil
import warnings
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import yaml

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from mm import plotstyle  # noqa: E402
from mm.config import load_config  # noqa: E402
from mm.engine import EngineConfig, run, stream_from_frame  # noqa: E402
from mm.metrics import summarize  # noqa: E402
from mm.pnl import RunResult  # noqa: E402
from mm.strategy import ASQuoter, from_config  # noqa: E402

GAMMA_GRID = [1e-6, 3e-6, 1e-5, 3e-5, 1e-4, 3e-4, 1e-3]
LATENCIES_MS = [0, 50, 100, 250, 500]
CAP_BUDGET = 0.05  # E2 risk constraint: fraction of time at the inventory cap
HYPOTHETICAL_FEES_BPS = [1.0, 10.0]  # E5 what-ifs (equities venue fee is 0)


def run_cfg(cfg: dict, stream: list, window: str, name: str) -> tuple[RunResult, dict]:
    ecfg = EngineConfig.from_config(cfg, window=window)
    res = run(stream, from_config(cfg["strategy"]), ecfg, name=name)
    m = summarize(res)
    m["name"] = name
    return res, m


def _hours(ts_ns: np.ndarray, midnight_ns: int) -> np.ndarray:
    return (ts_ns - midnight_ns) / 3.6e12


def _thin(n: int, target: int = 3000) -> np.ndarray:
    step = max(1, n // target)
    idx = np.arange(0, n, step)
    return idx if idx[-1] == n - 1 else np.append(idx, n - 1)


def fig_paths(
    runs: dict[str, RunResult], midnight_ns: int, col: str, ylabel: str, title: str, path: Path
) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    for name, r in runs.items():
        ev = r.events
        idx = _thin(len(ev))
        y = ev[col].to_numpy()[idx]
        if col == "pnl_x2":
            y = y / 200.0
        ax.plot(_hours(ev["ts_ns"].to_numpy()[idx], midnight_ns), y, label=name, lw=1.6)
    ax.axhline(0, color=plotstyle.INK_2, lw=0.8)
    ax.set_xlabel("time of day (h, New York)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_inventory_panels(runs: dict[str, RunResult], midnight_ns: int, path: Path) -> None:
    fig, axes = plt.subplots(len(runs), 1, figsize=(7.5, 4.6), sharex=True, sharey=True)
    for ax, (name, r), color in zip(axes, runs.items(), plotstyle.SERIES, strict=False):
        ev = r.events
        idx = _thin(len(ev), 6000)
        ax.plot(
            _hours(ev["ts_ns"].to_numpy()[idx], midnight_ns),
            ev["q"].to_numpy()[idx],
            color=color,
            lw=0.9,
        )
        ax.axhline(0, color=plotstyle.INK_2, lw=0.8)
        q_max = r.meta["q_max"]
        for y in (q_max, -q_max):
            ax.axhline(y, color=plotstyle.INK_2, lw=0.8, ls=":")
        std = float(np.sqrt(np.average(ev["q"].to_numpy().astype(float) ** 2)))
        ax.set_title(
            f"{name}  (inventory RMS {std:.0f} shares; dotted = cap ±{q_max})",
            loc="left",
            fontsize=10,
        )
        ax.set_ylabel("shares")
    axes[-1].set_xlabel("time of day (h, New York)")
    fig.suptitle("E1: inventory paths, evaluation window", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_decomposition(metrics: dict[str, dict], path: Path) -> None:
    comps = [
        ("spread_capture", "spread capture"),
        ("inventory_carry", "inventory carry"),
        ("fees", "fees"),
        ("total_pnl", "total PnL"),
        ("adverse_selection_5s", "adverse sel. (5 s)"),
    ]
    names = list(metrics)
    x = np.arange(len(comps))
    width = 0.8 / len(names)
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    for i, n in enumerate(names):
        vals = [metrics[n][c] for c, _ in comps]
        bars = ax.bar(x + (i - (len(names) - 1) / 2) * width, vals, width * 0.92, label=n)
        for b, v in zip(bars, vals, strict=True):
            ax.annotate(
                f"{v:+,.0f}",
                (b.get_x() + b.get_width() / 2, v),
                xytext=(0, 4 if v >= 0 else -11),
                textcoords="offset points",
                ha="center",
                fontsize=8,
                color=plotstyle.INK_2,
            )
    ax.axhline(0, color=plotstyle.INK_2, lw=0.8)
    ax.set_xticks(x, [lab for _, lab in comps])
    ax.set_ylabel("USD")
    ax.set_title("E1: PnL decomposition, evaluation window")
    fig.text(
        0.01,
        0.01,
        "total PnL = spread capture + inventory carry + fees (exact). "
        "Adverse selection (5 s) is a sub-attribution shown for reference.",
        fontsize=8,
        color=plotstyle.INK_2,
    )
    ax.legend(loc="best")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path)
    plt.close(fig)


def e2_gamma_sweep(base: dict, stream: list, out: Path) -> float:
    rows = []
    for g in GAMMA_GRID:
        cfg = copy.deepcopy(base)
        cfg["strategy"]["gamma"] = g
        _, m = run_cfg(cfg, stream, "calibration", f"gamma={g:g}")
        m["gamma"] = g
        m["pnl_per_inventory_std"] = (
            m["total_pnl"] / m["inventory_std"] if m["inventory_std"] else math.nan
        )
        rows.append(m)
    df = pd.DataFrame(rows)
    feasible = df[df["frac_time_at_cap"] <= CAP_BUDGET]
    pick = feasible if len(feasible) else df
    chosen = float(pick.loc[pick["total_pnl"].idxmax(), "gamma"])
    df["chosen"] = df["gamma"] == chosen
    df.to_csv(out / "summary.csv", index=False)

    fig, axes = plt.subplots(2, 1, figsize=(7, 5.6), sharex=True)
    axes[0].plot(df["gamma"], df["total_pnl"], "o-", label="total PnL")
    axes[0].set_ylabel("PnL (USD), calibration window")
    axes[1].plot(
        df["gamma"], df["inventory_std"], "o-", color=plotstyle.SERIES[1], label="inventory std"
    )
    axes[1].plot(
        df["gamma"],
        df["frac_time_at_cap"] * 100,
        "s--",
        color=plotstyle.SERIES[2],
        label="% time at cap",
    )
    axes[1].set_ylabel("shares   /   % of time")
    axes[1].set_xlabel("γ  (1 / tick·share), log scale")
    for ax in axes:
        ax.set_xscale("log")
        ax.axvline(chosen, color=plotstyle.INK_2, lw=1, ls=":")
        ax.legend(loc="best")
    axes[0].set_title(f"E2: γ sweep on the calibration window (chosen γ = {chosen:g})")
    axes[0].text(
        0.01,
        0.04,
        f"chosen = best PnL among γ with ≤ {CAP_BUDGET:.0%} of time at the cap",
        transform=axes[0].transAxes,
        fontsize=8,
        color=plotstyle.INK_2,
    )
    fig.tight_layout()
    fig.savefig(out / "gamma_sweep.png")
    plt.close(fig)
    return chosen


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="configs/base.yaml")
    ap.add_argument("--as-config", default="configs/as_default.yaml")
    ap.add_argument("--naive-config", default="configs/naive.yaml")
    ap.add_argument("--results", default="results")
    args = ap.parse_args()

    as_cfg = load_config(args.as_config, base=args.base)
    naive_cfg = load_config(args.naive_config, base=args.base)
    stream = stream_from_frame(pd.read_parquet(as_cfg["dataset"]["processed_path"]))
    midnight_ns = int(as_cfg["dataset"]["session_open_ns"]) - 9 * 3600 * 10**9 - 30 * 60 * 10**9
    results = Path(args.results)
    plotstyle.apply()

    # ---- E2: choose gamma on the calibration window -------------------------------
    out = results / "E2"
    out.mkdir(parents=True, exist_ok=True)
    gamma = e2_gamma_sweep(as_cfg, stream, out)
    as_cfg["strategy"]["gamma"] = gamma
    print(f"E2: chosen gamma = {gamma:g}")

    # ---- E1: naive vs A-S, naive half-spread = A-S delta*/2 at q = 0 ---------------
    out = results / "E1"
    out.mkdir(parents=True, exist_ok=True)
    quoter = from_config(as_cfg["strategy"])
    assert isinstance(quoter, ASQuoter)
    with open(as_cfg["strategy"]["calibration_path"]) as f:
        sigma_cal = float(yaml.safe_load(f)["sigma_cal"])
    half = quoter.optimal_spread(sigma_cal, quoter.tau0_s) / 2
    naive_cfg["strategy"]["half_spread_ticks"] = max(1, round(half))
    res_as, m_as = run_cfg(as_cfg, stream, "evaluation", "Avellaneda–Stoikov")
    res_nv, m_nv = run_cfg(naive_cfg, stream, "evaluation", "Naive")
    e1 = pd.DataFrame([m_nv, m_as]).set_index("name")
    e1["gamma"] = [math.nan, gamma]
    e1["half_spread_ticks_naive"] = [naive_cfg["strategy"]["half_spread_ticks"], math.nan]
    e1.to_csv(out / "summary.csv")
    runs = {"Naive": res_nv, "Avellaneda–Stoikov": res_as}
    fig_paths(
        runs,
        midnight_ns,
        "pnl_x2",
        "cumulative PnL (USD)",
        "E1: cumulative PnL, evaluation window",
        out / "cum_pnl.png",
    )
    fig_inventory_panels(runs, midnight_ns, out / "inventory.png")
    fig_decomposition({"Naive": m_nv, "Avellaneda–Stoikov": m_as}, out / "decomposition.png")
    readme_dir = results / "readme"
    readme_dir.mkdir(exist_ok=True)
    for f in ("cum_pnl.png", "inventory.png", "decomposition.png"):
        shutil.copy(out / f, readme_dir / f)
    shutil.copy(results / "calibration" / "fit.png", readme_dir / "calibration_fit.png")
    shutil.copy(results / "E2" / "gamma_sweep.png", readme_dir / "gamma_sweep.png")
    print(
        f"E1: naive half-spread {naive_cfg['strategy']['half_spread_ticks']} ticks; "
        f"PnL naive {m_nv['total_pnl']:+.0f} vs A-S {m_as['total_pnl']:+.0f} USD; "
        f"inventory std {m_nv['inventory_std']:.0f} vs {m_as['inventory_std']:.0f}"
    )

    # ---- E4: fill-model sensitivity ------------------------------------------------
    out = results / "E4"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # latency 0 warning is expected here
        for model in ("off", "proportional"):
            for lat in LATENCIES_MS:
                cfg = copy.deepcopy(as_cfg)
                cfg["engine"]["latency_ms"] = lat
                cfg["engine"]["queue_cancel_model"] = model
                _, m = run_cfg(cfg, stream, "evaluation", f"lat={lat}ms,{model}")
                m["latency_ms"], m["queue_cancel_model"] = lat, model
                rows.append(m)
        for tf in ("full", "trade_qty"):  # through-fill rule at the reported latency
            cfg = copy.deepcopy(as_cfg)
            cfg["engine"]["through_fill_model"] = tf
            _, m = run_cfg(cfg, stream, "evaluation", f"lat=100ms,off,through={tf}")
            m["latency_ms"], m["queue_cancel_model"], m["through_fill_model"] = 100, "off", tf
            rows.append(m)
    e4 = pd.DataFrame(rows)
    e4["through_fill_model"] = e4["through_fill_model"].fillna("full")
    e4.to_csv(out / "summary.csv", index=False)
    fig, ax = plt.subplots(figsize=(7, 3.8))
    for model, mk in (("off", "o-"), ("proportional", "s--")):
        d = e4[(e4["queue_cancel_model"] == model) & (e4["through_fill_model"] == "full")]
        ax.plot(d["latency_ms"], d["total_pnl"], mk, label=f"queue cancel model: {model}")
    ax.axvline(100, color=plotstyle.INK_2, lw=1, ls=":")
    ax.set_xlabel("latency (ms), applied to posts and cancels")
    ax.set_ylabel("A–S total PnL (USD)")
    ax.set_title("E4: PnL vs latency and queue model (reported config: 100 ms, off)")
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(out / "pnl_vs_latency.png")
    plt.close(fig)
    shutil.copy(out / "pnl_vs_latency.png", readme_dir / "pnl_vs_latency.png")
    print("E4: done")

    # ---- E5: fees ------------------------------------------------------------------
    out = results / "E5"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    venue = float(as_cfg["engine"]["fee_bps"])
    for label, bps in [("venue", venue), ("zero", 0.0)] + [
        (f"hypothetical {b:g} bps", b) for b in HYPOTHETICAL_FEES_BPS
    ]:
        cfg = copy.deepcopy(as_cfg)
        cfg["engine"]["fee_bps"] = bps
        _, m = run_cfg(cfg, stream, "evaluation", label)
        m["fee_bps"] = bps
        rows.append(m)
    pd.DataFrame(rows).to_csv(out / "summary.csv", index=False)
    print("E5: done")


if __name__ == "__main__":
    main()

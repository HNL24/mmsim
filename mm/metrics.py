"""Summary statistics for a RunResult. Reads only ``events`` and ``fills``.

This is the reporting boundary: doubled minor units (``_x2``) are converted to USD
and ticks here and nowhere else. Floats are fine in this module.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from mm.pnl import RunResult

HORIZONS_S = (1, 5, 30)


def _usd(x2: float | np.ndarray, quote_minor_per_usd: int) -> float | np.ndarray:
    return x2 / 2.0 / quote_minor_per_usd


def _time_weights(ts_ns: np.ndarray) -> np.ndarray:
    """Duration (s) each event's state persists until the next event; last = 0."""
    w = np.zeros(len(ts_ns), dtype=float)
    if len(ts_ns) > 1:
        w[:-1] = np.diff(ts_ns) / 1e9
    return w


def time_weighted_mean_std(x: np.ndarray, w: np.ndarray) -> tuple[float, float]:
    if w.sum() <= 0:
        return float(np.mean(x)), float(np.std(x))
    m = float(np.average(x, weights=w))
    return m, float(np.sqrt(np.average((x - m) ** 2, weights=w)))


def max_drawdown(path: np.ndarray) -> float:
    """Largest peak-to-trough decline of a cumulative PnL path (same units as input)."""
    if len(path) == 0:
        return 0.0
    peak = np.maximum.accumulate(np.concatenate(([0.0], path)))
    return float(np.max(peak[1:] - path))


def per_minute_pnl(res: RunResult, quote_minor_per_usd: int = 100) -> pd.Series:
    """PnL change per wall-clock minute (USD), indexed by minute start (ns)."""
    ev = res.events
    if len(ev) == 0:
        return pd.Series(dtype=float)
    pnl = pd.Series(
        _usd(ev["pnl_x2"].to_numpy(), quote_minor_per_usd), index=ev["ts_ns"].to_numpy()
    )
    minute = (pnl.index // 60_000_000_000) * 60_000_000_000
    last = pnl.groupby(minute).last()
    start_ns = res.meta.get("start_ns", int(ev["ts_ns"].iloc[0]))
    prev = np.concatenate(([0.0], last.to_numpy()[:-1]))
    out = pd.Series(last.to_numpy() - prev, index=last.index)
    return out[out.index >= (start_ns // 60_000_000_000) * 60_000_000_000]


def realised_unrealised_x2(res: RunResult) -> tuple[float, float]:
    """Average-cost split of pre-fee PnL into realised (closed round-trips) and
    unrealised (open inventory marked at the final mid), doubled minor units.
    ``realised + unrealised == pnl_x2[-1] + 2*fees_cum[-1]`` up to float rounding.
    """
    tv = res.meta["tick_value_minor"]
    q = 0  # signed open position
    basis = 0.0  # doubled cost (minor units) of the |q| open units
    realised = 0.0
    for side, p, x in res.fills[["side", "price_ticks", "qty"]].itertuples(index=False):
        cost = 2 * p * tv
        if q == 0 or (q > 0) == (side > 0):  # open or add
            basis += x * cost
            q += side * x
            continue
        sgn = 1 if q > 0 else -1
        closed = min(x, abs(q))
        avg = basis / abs(q)
        realised += closed * (cost - avg) * sgn
        basis -= closed * avg
        q -= sgn * closed
        rest = x - closed
        if rest:  # flipped through zero
            basis = rest * cost
            q = side * rest
    if len(res.events):
        mid_final = int(res.events["mid_x2"].iloc[-1])
    else:
        mid_final = 0
    sgn = 1 if q > 0 else (-1 if q < 0 else 0)
    unrealised = q * mid_final * tv - sgn * basis
    return realised, unrealised


def decomposition_usd(res: RunResult, quote_minor_per_usd: int = 100) -> dict[str, float]:
    ev, fl = res.events, res.fills
    out = {
        "spread_capture": float(_usd(fl["sc_x2"].sum(), quote_minor_per_usd)) if len(fl) else 0.0,
        "inventory_carry": float(_usd(ev["ic_x2"].iloc[-1], quote_minor_per_usd))
        if len(ev)
        else 0.0,
        "fees": -float(ev["fees_cum"].iloc[-1]) / quote_minor_per_usd if len(ev) else 0.0,
    }
    for h in HORIZONS_S:
        col = f"as_{h}s_x2"
        out[f"adverse_selection_{h}s"] = (
            float(_usd(fl[col].sum(), quote_minor_per_usd)) if col in fl else 0.0
        )
    return out


def summarize(res: RunResult, quote_minor_per_usd: int = 100) -> dict[str, float]:
    """All §9 per-run metrics. Money in USD, spreads/adverse selection in ticks per share."""
    ev, fl, meta = res.events, res.fills, res.meta
    tv = meta["tick_value_minor"]
    n_ev, n_fill = len(ev), len(fl)
    window_s = (meta["end_ns"] - meta["start_ns"]) / 1e9
    m: dict[str, float] = {
        "n_events": n_ev,
        "n_fills": n_fill,
        "fills_per_min": n_fill / (window_s / 60),
    }
    if n_ev == 0:
        return m
    pnl_usd = _usd(ev["pnl_x2"].to_numpy(), quote_minor_per_usd)
    m["total_pnl"] = float(pnl_usd[-1])
    realised, unrealised = realised_unrealised_x2(res)
    m["realised_pnl"] = float(_usd(realised, quote_minor_per_usd))
    m["terminal_inventory_pnl"] = float(_usd(unrealised, quote_minor_per_usd))
    m["terminal_inventory"] = float(meta["q_final"])
    m["total_fees"] = float(ev["fees_cum"].iloc[-1]) / quote_minor_per_usd
    m["pnl_per_fill"] = m["total_pnl"] / n_fill if n_fill else float("nan")
    m.update(decomposition_usd(res, quote_minor_per_usd))

    w = _time_weights(ev["ts_ns"].to_numpy())
    both = (ev["bid_quote"] > 0) & (ev["ask_quote"] > 0)
    half = ((ev["ask_quote"] - ev["bid_quote"]) / 2.0).to_numpy()
    m["quoted_half_spread"] = (
        float(np.average(half[both], weights=w[both.to_numpy()])) if both.any() else float("nan")
    )
    m["frac_time_two_sided"] = (
        float(w[both.to_numpy()].sum() / w.sum()) if w.sum() else float("nan")
    )
    if n_fill:
        qty = fl["qty"].to_numpy()
        sc_ticks = fl["sc_x2"].to_numpy() / (2.0 * tv)
        for h in HORIZONS_S:
            as_ticks = fl[f"as_{h}s_x2"].to_numpy() / (2.0 * tv)
            m[f"adverse_selection_{h}s_ticks"] = float(as_ticks.sum() / qty.sum())
            m[f"realised_half_spread_{h}s"] = float((sc_ticks - as_ticks).sum() / qty.sum())
        m["spread_capture_ticks"] = float(sc_ticks.sum() / qty.sum())
    q = ev["q"].to_numpy().astype(float)
    m["inventory_mean"], m["inventory_std"] = time_weighted_mean_std(q, w)
    m["inventory_max_abs"] = float(np.abs(q).max())
    m["frac_time_at_cap"] = meta["time_at_cap_ns"] / (meta["end_ns"] - meta["start_ns"])
    pm = per_minute_pnl(res, quote_minor_per_usd)
    m["sharpe_per_min"] = (
        float(pm.mean() / pm.std()) if len(pm) > 1 and pm.std() > 0 else float("nan")
    )
    m["max_drawdown"] = max_drawdown(pnl_usd)
    m["n_rejected"] = float(meta.get("n_rejected", 0))
    return m

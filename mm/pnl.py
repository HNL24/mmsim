"""PnL accounting and decomposition. Pure functions, arrays in, arrays out.

Unit convention: because the mid is stored doubled, all monetary series that
involve the mid are kept **doubled** and carry an ``_x2`` suffix. Their unit is
2 × quote minor units (for AAPL: half-cents). ``cash`` and ``fee`` are plain
minor units (cents). The identity in :func:`check_identity` holds exactly in
integer arithmetic:

    pnl_x2 = 2*cash + q*mid_x2*tick_value_minor - 2*fees_cum
           = sum(sc_x2) + ic_x2 - 2*fees_cum
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


def fee_minor(price_ticks: int, qty: int, tick_value_minor: int, fee_bps: float) -> int:
    """Fee for one fill in quote minor units, rounded up (conservative).

    ``notional_minor = price_ticks * qty * tick_value_minor``; fee = notional × bps / 1e4.
    """
    if fee_bps == 0:
        return 0
    return math.ceil(price_ticks * qty * tick_value_minor * fee_bps / 10_000)


def spread_capture_x2(
    side: np.ndarray,
    price_ticks: np.ndarray,
    qty: np.ndarray,
    mid_x2: np.ndarray,
    tick_value_minor: int,
) -> np.ndarray:
    """Per-fill spread capture, doubled minor units.

    Buy (side +1): ``(mid - p) * x``; sell (side -1): ``(p - mid) * x``.
    """
    return side.astype(np.int64) * (mid_x2 - 2 * price_ticks) * qty * tick_value_minor


def inventory_carry_x2(
    q_before: np.ndarray, mid_x2: np.ndarray, tick_value_minor: int
) -> np.ndarray:
    """Cumulative inventory carry per event, doubled minor units.

    ``IC_T = sum_t q_{t-} (m_t - m_{t-})`` where ``q_before[t]`` is the inventory
    held before event ``t``'s fills. The first increment is zero.
    """
    d_mid = np.zeros_like(mid_x2)
    d_mid[1:] = np.diff(mid_x2)
    return np.cumsum(q_before * d_mid * tick_value_minor)


def mark_to_market_x2(
    cash: np.ndarray, q: np.ndarray, mid_x2: np.ndarray, fees_cum: np.ndarray, tick_value_minor: int
) -> np.ndarray:
    """``PnL_t = C_t + q_t m_t - Fees_t``, doubled minor units."""
    return 2 * cash + q * mid_x2 * tick_value_minor - 2 * fees_cum


def adverse_selection_x2(
    fill_ts: np.ndarray,
    fill_side: np.ndarray,
    fill_qty: np.ndarray,
    fill_mid_x2: np.ndarray,
    ts_ns: np.ndarray,
    mid_x2: np.ndarray,
    horizon_ns: int,
    tick_value_minor: int,
) -> np.ndarray:
    """Per-fill adverse selection at ``horizon_ns``, doubled minor units.

    Uses the last mid observed at or before ``fill_ts + horizon_ns`` (the final mid
    if the horizon runs past the end of the run). Buy: ``(m_f - m_{f+τ}) x``;
    sell: ``(m_{f+τ} - m_f) x``. Positive = the mid moved against us.
    """
    if len(fill_ts) == 0:
        return np.zeros(0, dtype=np.int64)
    idx = np.searchsorted(ts_ns, fill_ts + horizon_ns, side="right") - 1
    idx = np.clip(idx, 0, len(ts_ns) - 1)
    mid_after = mid_x2[idx]
    return fill_side.astype(np.int64) * (fill_mid_x2 - mid_after) * fill_qty * tick_value_minor


@dataclass(slots=True)
class RunResult:
    """Everything downstream reads from ``events`` and ``fills`` only.

    ``events`` columns (one row per event inside the run window):
      ts_ns, q, cash, mid_x2, fees_cum, bid_quote, ask_quote (0 = not quoting),
      ic_x2, pnl_x2.
    ``fills`` columns (one row per (partial) fill):
      ts_ns, event_idx, side, price_ticks, qty, mid_x2, fee, sc_x2, as_<h>s_x2.
    ``meta``: run configuration and scalar diagnostics (window, tick_value_minor,
    time_at_cap_ns, ...).
    """

    events: pd.DataFrame
    fills: pd.DataFrame
    meta: dict[str, Any] = field(default_factory=dict)


def check_identity(res: RunResult) -> bool:
    """§8.2 identity, exact integer equality on the final row."""
    if len(res.events) == 0:
        return True
    fees = int(res.events["fees_cum"].iloc[-1])
    lhs = int(res.events["pnl_x2"].iloc[-1])
    rhs = int(res.fills["sc_x2"].sum()) + int(res.events["ic_x2"].iloc[-1]) - 2 * fees
    return lhs == rhs


def finalize(
    ev: dict[str, np.ndarray],
    fl: dict[str, np.ndarray],
    tick_value_minor: int,
    horizons_s: tuple[int, ...],
    meta: dict[str, Any],
) -> RunResult:
    """Assemble a :class:`RunResult` from raw engine arrays, adding the decomposition."""
    events = pd.DataFrame({k: np.asarray(v, dtype=np.int64) for k, v in ev.items()})
    fills = pd.DataFrame({k: np.asarray(v, dtype=np.int64) for k, v in fl.items()})
    if len(events):
        q_before = np.concatenate(([0], events["q"].to_numpy()[:-1]))
        events["ic_x2"] = inventory_carry_x2(
            q_before, events["mid_x2"].to_numpy(), tick_value_minor
        )
        events["pnl_x2"] = mark_to_market_x2(
            events["cash"].to_numpy(),
            events["q"].to_numpy(),
            events["mid_x2"].to_numpy(),
            events["fees_cum"].to_numpy(),
            tick_value_minor,
        )
    else:
        events["ic_x2"] = np.zeros(0, dtype=np.int64)
        events["pnl_x2"] = np.zeros(0, dtype=np.int64)
    fills["sc_x2"] = spread_capture_x2(
        fills["side"].to_numpy(),
        fills["price_ticks"].to_numpy(),
        fills["qty"].to_numpy(),
        fills["mid_x2"].to_numpy(),
        tick_value_minor,
    )
    for h in horizons_s:
        fills[f"as_{h}s_x2"] = adverse_selection_x2(
            fills["ts_ns"].to_numpy(),
            fills["side"].to_numpy(),
            fills["qty"].to_numpy(),
            fills["mid_x2"].to_numpy(),
            events["ts_ns"].to_numpy(),
            events["mid_x2"].to_numpy(),
            h * 1_000_000_000,
            tick_value_minor,
        )
    return RunResult(events=events, fills=fills, meta=meta)

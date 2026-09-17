"""Estimate the A–S fill-intensity parameters A, k, and the imbalance slope beta.

lambda(delta) = A * exp(-k * delta): the rate (per second) at which a resting
order ``delta`` ticks from the mid would be filled. Estimated by counting trades
at or through ``mid ± delta`` for a grid of ``delta`` and fitting a line to
``ln lambda``.

beta: OLS slope of the forward mid move (ticks over ``horizon_s``) on the touch
imbalance, sampled on a regular grid. Used by ``ASQuoter(imbalance_beta_ticks=...)``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from mm.book import OrderBook
from mm.events import ASK, BID, LEVEL_SET

NS = 1_000_000_000


@dataclass(frozen=True, slots=True)
class Calibration:
    """Fitted intensity. ``A`` in fills/second at delta=0; ``k`` in 1/ticks.

    ``deltas`` is the grid (ticks); ``counts_bid``/``counts_ask`` are the number of
    trades in the window that would have filled a resting bid/ask at ``mid ∓ delta``;
    ``lam`` is the pooled rate per second; ``fit_mask`` marks the grid points used.
    """

    A: float
    k: float
    r2: float
    window_s: float
    deltas: np.ndarray
    counts_bid: np.ndarray
    counts_ask: np.ndarray
    lam: np.ndarray
    fit_mask: np.ndarray


def fill_distances(
    stream: Sequence[tuple[int, int, int, int, int, int]], start_ns: int, end_ns: int
) -> tuple[np.ndarray, np.ndarray]:
    """For every trade in ``[start_ns, end_ns)`` the max distance (ticks, doubled) from
    the pre-trade mid at which a resting order on the hit side would still have filled.

    Returns ``(bid_d2, ask_d2)``: doubled distances (so half-ticks stay integers)
    for trades that hit bids (sell aggressor) and asks (buy aggressor). A resting
    bid at ``mid - delta`` fills when ``2*delta <= mid_x2 - 2*price``.
    """
    book = OrderBook()
    bid_d2: list[int] = []
    ask_d2: list[int] = []
    for ts, kind, side, price, qty, _aggressor in stream:
        if kind == LEVEL_SET:
            book.apply(side, price, qty)
            continue
        if ts < start_ns:
            continue
        if ts >= end_ns:
            break
        mid_x2 = book.mid_ticks_x2()
        if mid_x2 is None:
            continue
        if side == BID:  # sell aggressor hit the bid side
            bid_d2.append(mid_x2 - 2 * price)
        elif side == ASK:
            ask_d2.append(2 * price - mid_x2)
    return np.asarray(bid_d2, dtype=np.int64), np.asarray(ask_d2, dtype=np.int64)


def fit_intensity(
    bid_d2: np.ndarray,
    ask_d2: np.ndarray,
    window_s: float,
    max_delta: int = 40,
    min_count: int = 30,
) -> Calibration:
    """Count fills per grid distance, pool sides, fit ``ln lam = ln A - k delta`` by OLS.

    Grid points with fewer than ``min_count`` pooled events are excluded from the fit.
    """
    deltas = np.arange(1, max_delta + 1, dtype=np.int64)
    counts_bid = np.array([(bid_d2 >= 2 * d).sum() for d in deltas], dtype=np.int64)
    counts_ask = np.array([(ask_d2 >= 2 * d).sum() for d in deltas], dtype=np.int64)
    pooled = (counts_bid + counts_ask) / 2.0
    lam = pooled / window_s
    fit_mask = pooled >= min_count
    if fit_mask.sum() < 3:
        raise ValueError("fewer than 3 grid points with enough events; widen the window")
    x = deltas[fit_mask].astype(float)
    y = np.log(lam[fit_mask])
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (intercept + slope * x)
    r2 = 1.0 - float(resid @ resid) / float(((y - y.mean()) ** 2).sum())
    return Calibration(
        A=float(np.exp(intercept)),
        k=float(-slope),
        r2=r2,
        window_s=window_s,
        deltas=deltas,
        counts_bid=counts_bid,
        counts_ask=counts_ask,
        lam=lam,
        fit_mask=fit_mask,
    )


def calibrate(
    stream: Sequence[tuple[int, int, int, int, int, int]],
    start_ns: int,
    end_ns: int,
    max_delta: int = 40,
    min_count: int = 30,
) -> Calibration:
    """End-to-end: distances on ``[start_ns, end_ns)`` then :func:`fit_intensity`."""
    bid_d2, ask_d2 = fill_distances(stream, start_ns, end_ns)
    return fit_intensity(bid_d2, ask_d2, (end_ns - start_ns) / NS, max_delta, min_count)


@dataclass(frozen=True, slots=True)
class ImbalanceFit:
    """OLS of the forward mid move on touch imbalance.

    ``beta_ticks``: expected mid move (ticks) over ``horizon_s`` per unit of
    imbalance; ``intercept_ticks`` in ticks; ``n`` samples used. ``imbalance`` and
    ``move_ticks`` are the sampled regressors/targets (for plotting).
    """

    beta_ticks: float
    intercept_ticks: float
    r2: float
    n: int
    horizon_s: float
    sample_s: float
    imbalance: np.ndarray
    move_ticks: np.ndarray


def sample_touch(
    stream: Sequence[tuple[int, int, int, int, int, int]],
    start_ns: int,
    end_ns: int,
    sample_s: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Sample the book every ``sample_s`` on ``[start_ns, end_ns)``.

    Returns ``(mid_x2, imbalance)`` as float arrays, one entry per grid point, with
    NaN where the book was one-sided. A grid point sees the book as of all events
    with ``ts <= grid_ts`` and nothing later.
    """
    step = int(round(sample_s * NS))
    book = OrderBook()
    mids: list[float] = []
    imbs: list[float] = []
    next_t = start_ns

    def record() -> None:
        bb, ba = book.best_bid(), book.best_ask()
        if bb is None or ba is None:
            mids.append(np.nan)
            imbs.append(np.nan)
            return
        mids.append(float(bb[0] + ba[0]))
        tot = bb[1] + ba[1]
        imbs.append((bb[1] - ba[1]) / tot if tot else 0.0)

    for ts, kind, side, price, qty, _ in stream:
        while next_t < ts and next_t < end_ns:
            record()
            next_t += step
        if ts >= end_ns:
            break
        if kind == LEVEL_SET:
            book.apply(side, price, qty)
    while next_t < end_ns:
        record()
        next_t += step
    return np.asarray(mids, dtype=float), np.asarray(imbs, dtype=float)


def fit_imbalance(
    stream: Sequence[tuple[int, int, int, int, int, int]],
    start_ns: int,
    end_ns: int,
    horizon_s: float = 5.0,
    sample_s: float = 1.0,
) -> ImbalanceFit:
    """Regress the mid move over ``horizon_s`` (ticks) on the touch imbalance.

    Every sample uses the book at its own grid time and the mid ``horizon_s`` later;
    this is estimation on the calibration window, not a trading-time input.
    """
    mid_x2, imb = sample_touch(stream, start_ns, end_ns, sample_s)
    h = int(round(horizon_s / sample_s))
    if h < 1 or len(mid_x2) <= h:
        raise ValueError("horizon must be >= sample_s and shorter than the window")
    y = (mid_x2[h:] - mid_x2[:-h]) / 2.0
    x = imb[:-h]
    ok = ~(np.isnan(x) | np.isnan(y))
    x, y = x[ok], y[ok]
    if len(x) < 30:
        raise ValueError("fewer than 30 imbalance samples; widen the window")
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (intercept + slope * x)
    ss = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float(resid @ resid) / ss if ss else 0.0
    return ImbalanceFit(
        beta_ticks=float(slope),
        intercept_ticks=float(intercept),
        r2=r2,
        n=int(len(x)),
        horizon_s=float(horizon_s),
        sample_s=float(sample_s),
        imbalance=x,
        move_ticks=y,
    )

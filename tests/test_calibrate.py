import numpy as np
import pytest
from conftest import snapshot, trade

from mm.calibrate import calibrate, fill_distances, fit_intensity
from mm.events import ASK, BID

S = 1_000_000_000


def _synthetic(A: float, k: float, T_s: float, seed: int = 0):
    """Trades whose fill distance d = floor(Exp(k)) so that P(d >= delta) = exp(-k delta)."""
    rng = np.random.default_rng(seed)
    ev = snapshot(0, {1000: 100, 990: 100}, {1020: 100, 1030: 100})  # mid = 1010 exactly
    trades = []
    for side in (BID, ASK):
        n = rng.poisson(A * T_s)
        ts = np.sort(rng.uniform(0, T_s, n))
        d = np.floor(rng.exponential(1.0 / k, n)).astype(int)
        for t, dd in zip(ts, d, strict=True):
            price = 1010 - dd if side == BID else 1010 + dd
            trades.append(trade(int(t * S) + 1, side, int(price), 10))
    trades.sort(key=lambda e: e[0])
    return ev + trades


def test_recovers_known_A_and_k():
    A, k = 2.0, 0.15
    cal = calibrate(_synthetic(A, k, T_s=3600), 0, 3600 * S, max_delta=40, min_count=30)
    assert cal.k == pytest.approx(k, rel=0.05)
    assert cal.A == pytest.approx(A, rel=0.10)
    assert cal.r2 > 0.98
    assert cal.deltas[cal.fit_mask].min() == 1


def test_fill_distances_use_pre_trade_mid_and_sides():
    ev = snapshot(0, {1000: 100}, {1020: 100})
    ev += [trade(1, BID, 1000, 5), trade(2, ASK, 1025, 5), trade(3, BID, 1012, 5)]
    bid_d2, ask_d2 = fill_distances(ev, 0, 10)
    assert bid_d2.tolist() == [20, -4]  # bid at 1000: 10 ticks below mid 1010 -> doubled 20
    assert ask_d2.tolist() == [30]


def test_window_bounds_respected():
    ev = snapshot(0, {1000: 100}, {1020: 100}) + [trade(5, BID, 1000, 1), trade(15, BID, 1000, 1)]
    bid_d2, _ = fill_distances(ev, 0, 10)
    assert len(bid_d2) == 1


def test_fit_requires_enough_points():
    with pytest.raises(ValueError):
        fit_intensity(np.array([2, 2, 2]), np.array([2]), window_s=10.0, min_count=30)

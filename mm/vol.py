"""Volatility estimators.

Every estimator returns sigma in **ticks per sqrt(second)** (arithmetic, not log).
Estimators are fed ``(ts_ns, mid_x2)`` by the strategy at each quote call and
only ever see the past.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Protocol


class SigmaEstimator(Protocol):
    def update(self, ts_ns: int, mid_x2: int) -> None:
        """Observe the current doubled mid (ticks) at ``ts_ns``."""
        ...

    def sigma(self) -> float:
        """Current estimate, ticks / sqrt(s)."""
        ...


class ConstantVol:
    """Fixed sigma (ticks / sqrt(s)); for unit tests and sanity checks."""

    def __init__(self, sigma: float) -> None:
        if sigma < 0:
            raise ValueError("sigma must be >= 0")
        self._sigma = float(sigma)

    def update(self, ts_ns: int, mid_x2: int) -> None:
        return None

    def sigma(self) -> float:
        return self._sigma


class RollingRV:
    """Trailing realised volatility of the mid.

    The mid is sampled at most once every ``sample_s`` seconds (suppresses
    microstructure noise), and over the trailing ``window_s`` seconds
    ``sigma^2 = sum(dm_j^2) / sum(dt_j)`` with ``dm`` in ticks and ``dt`` in seconds,
    i.e. the realised variance *rate*, which is robust to gaps in the sampling.
    Returns 0 until two samples exist.
    """

    def __init__(self, window_s: float, sample_s: float = 1.0) -> None:
        if window_s <= 0 or sample_s <= 0:
            raise ValueError("window_s and sample_s must be positive")
        self.window_ns = int(window_s * 1e9)
        self.sample_ns = int(sample_s * 1e9)
        self._samples: deque[tuple[int, int]] = deque()  # (ts_ns, mid_x2)
        self._sum_dm2 = 0.0  # ticks^2
        self._sum_dt = 0.0  # seconds

    def update(self, ts_ns: int, mid_x2: int) -> None:
        if self._samples and ts_ns < self._samples[-1][0] + self.sample_ns:
            return
        if self._samples:
            t_prev, m_prev = self._samples[-1]
            dm = (mid_x2 - m_prev) / 2.0
            self._sum_dm2 += dm * dm
            self._sum_dt += (ts_ns - t_prev) / 1e9
        self._samples.append((ts_ns, mid_x2))
        cutoff = ts_ns - self.window_ns
        while len(self._samples) > 1 and self._samples[0][0] < cutoff:
            t0, m0 = self._samples.popleft()
            t1, m1 = self._samples[0]
            dm = (m1 - m0) / 2.0
            self._sum_dm2 -= dm * dm
            self._sum_dt -= (t1 - t0) / 1e9

    def sigma(self) -> float:
        if self._sum_dt <= 0:
            return 0.0
        return math.sqrt(max(self._sum_dm2, 0.0) / self._sum_dt)


def realised_vol(
    ts_ns: list[int] | object, mid_x2: list[int] | object, sample_s: float = 1.0
) -> float:
    """Whole-sample realised vol (ticks / sqrt(s)) of a mid path, sampled every ``sample_s``.

    Offline counterpart of :class:`RollingRV` with an infinite window; used by
    the calibration script to produce ``sigma_cal`` for the A–S units check.
    """
    rv = RollingRV(window_s=1e12, sample_s=sample_s)
    for t, m in zip(ts_ns, mid_x2, strict=True):
        rv.update(int(t), int(m))
    return rv.sigma()

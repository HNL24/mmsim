"""Quoter interface and quoting strategies.

Strategies return integer tick prices. Float intermediate values (A–S) are
snapped to the grid with :func:`to_grid` (bid rounds down, ask rounds up) before
returning; the engine then clips quotes so they never cross the market.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

import yaml

from mm.book import BookView
from mm.vol import ConstantVol, RollingRV, SigmaEstimator


@dataclass(frozen=True, slots=True)
class State:
    """What the strategy may see besides the book.

    ``q`` inventory in base units; ``cash`` in quote minor units; ``ts_ns`` and
    ``t_end_ns`` in ns since epoch (``t_end_ns`` is the end of the run window,
    for finite-horizon strategies).
    """

    q: int
    cash: int
    ts_ns: int
    t_end_ns: int


class Quoter(Protocol):
    def quote(self, book: BookView, state: State) -> tuple[int, int]:
        """Return ``(bid_ticks, ask_ticks)``. Must satisfy ``bid < ask``."""
        ...


def to_grid(bid: float, ask: float) -> tuple[int, int]:
    """Snap float tick prices to the integer grid: bid down, ask up."""
    return math.floor(bid), math.ceil(ask)


def book_imbalance(book: BookView) -> float:
    """Touch imbalance ``(bid_qty - ask_qty) / (bid_qty + ask_qty)`` in ``[-1, 1]``.

    Dimensionless. Positive means more resting size on the bid, i.e. buying
    pressure. Returns 0 when either side is empty. Uses only the current book.
    """
    bb, ba = book.best_bid, book.best_ask
    if bb is None or ba is None:
        return 0.0
    tot = bb[1] + ba[1]
    return (bb[1] - ba[1]) / tot if tot else 0.0


class NaiveQuoter:
    """Symmetric quotes ``mid ± half_spread_ticks``; ignores inventory."""

    def __init__(self, half_spread_ticks: int) -> None:
        if half_spread_ticks < 1:
            raise ValueError("half_spread_ticks must be >= 1")
        self.half_spread_ticks = int(half_spread_ticks)

    def quote(self, book: BookView, state: State) -> tuple[int, int]:
        mid_x2 = book.mid_x2
        if mid_x2 is None:
            raise ValueError("cannot quote on a one-sided book")
        h = self.half_spread_ticks
        # mid = mid_x2 / 2; bid rounds down, ask rounds up.
        return to_grid((mid_x2 - 2 * h) / 2, (mid_x2 + 2 * h) / 2)


class ASQuoter:
    """Avellaneda–Stoikov (2008) quotes.

    Units: ``gamma`` in 1/(ticks·base_unit); ``k`` in 1/ticks; ``sigma`` (from the
    estimator) in ticks/sqrt(s); ``tau0_s`` seconds. Then ``q*gamma*sigma^2*tau``
    and ``(2/gamma)*ln(1 + gamma/k)`` are both in ticks.

    ``horizon="constant"`` uses ``T - t = tau0_s``; ``"finite"`` uses the time to
    ``state.t_end_ns``. ``sigma_ref`` (ticks/sqrt(s), from the calibration window)
    is only used for the construction-time units check.

    ``imbalance_beta_ticks`` (ticks per unit of touch imbalance, default 0) shifts
    the reservation price by ``beta * imbalance`` so quotes lean toward the side
    the book says the mid is about to move. ``beta`` is the OLS slope of the
    forward mid move on imbalance, fitted on the calibration window
    (:func:`mm.calibrate.fit_imbalance`). With ``beta = 0`` this is plain A–S.
    """

    def __init__(
        self,
        gamma: float,
        k: float,
        sigma_estimator: SigmaEstimator,
        tau0_s: float = 60.0,
        horizon: str = "constant",
        sigma_ref: float | None = None,
        log_sigma: bool = False,
        imbalance_beta_ticks: float = 0.0,
    ) -> None:
        if gamma <= 0 or k <= 0 or tau0_s <= 0:
            raise ValueError("gamma, k and tau0_s must be positive")
        self.imbalance_beta_ticks = float(imbalance_beta_ticks)
        if horizon not in ("constant", "finite"):
            raise ValueError("horizon must be 'constant' or 'finite'")
        self.gamma = float(gamma)
        self.k = float(k)
        self.vol = sigma_estimator
        self.tau0_s = float(tau0_s)
        self.horizon = horizon
        self.sigma_log: list[tuple[int, float]] = []
        self._log_sigma = log_sigma
        ref = self.vol.sigma() if sigma_ref is None else float(sigma_ref)
        d = self.optimal_spread(ref, self.tau0_s)
        if not 0.5 <= d <= 50.0:
            raise ValueError(
                f"A-S units check failed: delta*(q=0) = {d:.4g} ticks with gamma={gamma}, "
                f"k={k}, sigma_ref={ref}, tau={tau0_s}s; expected 0.5..50. "
                "Check the units of gamma, k, sigma and tau in the class docstring."
            )

    def optimal_spread(self, sigma: float, tau_s: float) -> float:
        """Total optimal spread delta* in ticks."""
        g = self.gamma
        return g * sigma * sigma * tau_s + (2.0 / g) * math.log1p(g / self.k)

    def reservation_price(self, mid: float, q: int, sigma: float, tau_s: float) -> float:
        """Reservation price in ticks: ``mid - q*gamma*sigma^2*tau``."""
        return mid - q * self.gamma * sigma * sigma * tau_s

    def quote(self, book: BookView, state: State) -> tuple[int, int]:
        mid_x2 = book.mid_x2
        if mid_x2 is None:
            raise ValueError("cannot quote on a one-sided book")
        self.vol.update(state.ts_ns, mid_x2)
        sigma = self.vol.sigma()
        if self._log_sigma:
            self.sigma_log.append((state.ts_ns, sigma))
        tau = (
            self.tau0_s
            if self.horizon == "constant"
            else max(state.t_end_ns - state.ts_ns, 0) / 1e9
        )
        r = self.reservation_price(mid_x2 / 2.0, state.q, sigma, tau)
        if self.imbalance_beta_ticks:
            r += self.imbalance_beta_ticks * book_imbalance(book)
        half = self.optimal_spread(sigma, tau) / 2.0
        return to_grid(r - half, r + half)


def sigma_from_config(cfg: dict) -> SigmaEstimator:
    name = cfg["name"]
    if name == "rolling_rv":
        return RollingRV(window_s=float(cfg["window_s"]), sample_s=float(cfg.get("sample_s", 1.0)))
    if name == "constant":
        return ConstantVol(float(cfg["sigma"]))
    raise NotImplementedError(f"sigma estimator {name!r} not available")


def from_config(strategy_cfg: dict) -> Quoter:
    """Build a quoter from the ``strategy`` section of a merged config."""
    name = strategy_cfg["name"]
    if name == "naive":
        return NaiveQuoter(int(strategy_cfg["half_spread_ticks"]))
    if name == "avellaneda_stoikov":
        params: dict = {}
        path = strategy_cfg.get("calibration_path")
        if path:
            with open(path) as f:
                params = yaml.safe_load(f) or {}
        k = float(strategy_cfg.get("k", params.get("k", 0.0)))
        sigma_ref = strategy_cfg.get("sigma_ref", params.get("sigma_cal"))
        return ASQuoter(
            gamma=float(strategy_cfg["gamma"]),
            k=k,
            sigma_estimator=sigma_from_config(strategy_cfg["sigma_estimator"]),
            tau0_s=float(strategy_cfg.get("tau0_s", 60.0)),
            horizon=str(strategy_cfg.get("horizon", "constant")),
            sigma_ref=None if sigma_ref is None else float(sigma_ref),
            log_sigma=bool(strategy_cfg.get("log_sigma", False)),
            imbalance_beta_ticks=float(strategy_cfg.get("imbalance_beta_ticks", 0.0)),
        )
    raise NotImplementedError(f"strategy {name!r} not available")

"""Quoter interface and quoting strategies.

Strategies return integer tick prices. Float intermediate values (A–S) are
snapped to the grid with :func:`to_grid` (bid rounds down, ask rounds up) before
returning; the engine then clips quotes so they never cross the market.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from mm.book import BookView


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


def from_config(strategy_cfg: dict) -> Quoter:
    """Build a quoter from the ``strategy`` section of a config (naive only until M3)."""
    name = strategy_cfg["name"]
    if name == "naive":
        return NaiveQuoter(int(strategy_cfg["half_spread_ticks"]))
    raise NotImplementedError(f"strategy {name!r} not available yet")

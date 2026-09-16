"""Limit order book reconstructed from LEVEL_SET events.

Prices are integer ticks, quantities integer base units. The mid is stored
doubled (``best_bid + best_ask``) so it stays an integer.
"""

from __future__ import annotations

from dataclasses import dataclass

from mm.events import ASK, BID


@dataclass(frozen=True, slots=True)
class BookView:
    """Read-only snapshot handed to strategies.

    ``bids`` is sorted best-first (descending price), ``asks`` best-first
    (ascending price). Each entry is ``(price_ticks, qty)``.
    """

    bids: tuple[tuple[int, int], ...]
    asks: tuple[tuple[int, int], ...]

    @property
    def best_bid(self) -> tuple[int, int] | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> tuple[int, int] | None:
        return self.asks[0] if self.asks else None

    @property
    def mid_x2(self) -> int | None:
        """Doubled mid in ticks (``best_bid + best_ask``), or None if one side is empty."""
        if not self.bids or not self.asks:
            return None
        return self.bids[0][0] + self.asks[0][0]

    def depth(self, side: int, price_ticks: int) -> int:
        """Resting qty (base units) at ``price_ticks`` on ``side``; 0 if absent."""
        for p, q in self.bids if side == BID else self.asks:
            if p == price_ticks:
                return q
        return 0


class OrderBook:
    """Mutable book: two dicts ``{price_ticks: qty}`` plus cached best prices."""

    __slots__ = ("asks", "bids", "_best_ask", "_best_bid")

    def __init__(self) -> None:
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}
        self._best_bid: int | None = None
        self._best_ask: int | None = None

    def apply(self, side: int, price_ticks: int, qty: int) -> int:
        """Apply a LEVEL_SET: set resting ``qty`` at ``price_ticks``; 0 removes the level.

        Returns the previous qty at that level (0 if it did not exist).
        """
        if qty < 0:
            raise ValueError("qty must be non-negative")
        if side == BID:
            old = self.bids.get(price_ticks, 0)
            if qty == 0:
                self.bids.pop(price_ticks, None)
                if price_ticks == self._best_bid:
                    self._best_bid = max(self.bids) if self.bids else None
            else:
                self.bids[price_ticks] = qty
                if self._best_bid is None or price_ticks > self._best_bid:
                    self._best_bid = price_ticks
        elif side == ASK:
            old = self.asks.get(price_ticks, 0)
            if qty == 0:
                self.asks.pop(price_ticks, None)
                if price_ticks == self._best_ask:
                    self._best_ask = min(self.asks) if self.asks else None
            else:
                self.asks[price_ticks] = qty
                if self._best_ask is None or price_ticks < self._best_ask:
                    self._best_ask = price_ticks
        else:
            raise ValueError(f"side must be +1 or -1, got {side}")
        return old

    def best_bid(self) -> tuple[int, int] | None:
        """``(price_ticks, qty)`` of the best bid, or None if no bids."""
        if self._best_bid is None:
            return None
        return self._best_bid, self.bids[self._best_bid]

    def best_ask(self) -> tuple[int, int] | None:
        """``(price_ticks, qty)`` of the best ask, or None if no asks."""
        if self._best_ask is None:
            return None
        return self._best_ask, self.asks[self._best_ask]

    def mid_ticks_x2(self) -> int | None:
        """``best_bid + best_ask`` in ticks (twice the mid), or None if a side is empty."""
        if self._best_bid is None or self._best_ask is None:
            return None
        return self._best_bid + self._best_ask

    def depth(self, side: int, price_ticks: int) -> int:
        """Resting qty (base units) at ``price_ticks`` on ``side``; 0 if absent."""
        return (self.bids if side == BID else self.asks).get(price_ticks, 0)

    def view(self) -> BookView:
        """Frozen snapshot of the whole visible book, best levels first."""
        return BookView(
            bids=tuple(sorted(self.bids.items(), reverse=True)),
            asks=tuple(sorted(self.asks.items())),
        )

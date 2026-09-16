"""Event loop, order manager, fill model, latency, fees and inventory cap.

Units: prices integer ticks, quantities integer base units, times int ns,
``cash``/fees quote minor units. The strategy only ever sees ``BookView`` and
``State`` for the current event.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from mm.book import OrderBook
from mm.events import ASK, BID, LEVEL_SET, TRADE
from mm.pnl import RunResult, fee_minor, finalize
from mm.strategy import Quoter, State

# (ts_ns, kind, side, price_ticks, qty, aggressor)
EventTuple = tuple[int, int, int, int, int, int]


def stream_from_frame(df: pd.DataFrame) -> list[EventTuple]:
    """Canonical event DataFrame -> list of plain int tuples for the loop."""
    cols = ("ts_ns", "kind", "side", "price_ticks", "qty", "aggressor")
    return list(zip(*(df[c].tolist() for c in cols), strict=True))


def _cancel_model(v: Any) -> str:
    """YAML parses a bare ``off`` as False; accept both spellings."""
    return "off" if v is False or v is None else str(v)


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Run parameters. ``start_ns``/``end_ns`` bound quoting; events before
    ``start_ns`` are replayed into the book only (warm-up)."""

    start_ns: int
    end_ns: int
    latency_ns: int
    requote_interval_ns: int
    order_qty: int
    q_max: int
    fee_bps: float
    tick_value_minor: int  # quote minor units per tick (1 for $0.01 tick, cents accounting)
    queue_cancel_model: str = "off"  # off | proportional
    as_horizons_s: tuple[int, ...] = (1, 5, 30)

    @classmethod
    def from_config(cls, cfg: dict[str, Any], window: str = "evaluation") -> EngineConfig:
        ds, en, w = cfg["dataset"], cfg["engine"], cfg["windows"][window]
        tv = ds["tick_value_usd"] / ds["quote_minor_unit_usd"]
        if abs(tv - round(tv)) > 1e-9:
            raise ValueError("tick_value_usd must be an integer multiple of quote_minor_unit_usd")
        return cls(
            start_ns=int(w["start_ns"]),
            end_ns=int(w["end_ns"]),
            latency_ns=int(en["latency_ms"]) * 1_000_000,
            requote_interval_ns=int(en["requote_interval_ms"]) * 1_000_000,
            order_qty=int(en["order_qty"]),
            q_max=int(en["q_max"]),
            fee_bps=float(en["fee_bps"]),
            tick_value_minor=int(round(tv)),
            queue_cancel_model=_cancel_model(en.get("queue_cancel_model", "off")),
            as_horizons_s=tuple(
                int(h) for h in cfg.get("pnl", {}).get("adverse_selection_horizons_s", (1, 5, 30))
            ),
        )


@dataclass(slots=True)
class Order:
    side: int
    price: int
    qty_remaining: int
    ts_posted: int
    ts_active: int
    queue_ahead: int = -1  # -1 until activated
    ts_cancel: int | None = None  # cancel effective time; the order stays live until then

    @property
    def active(self) -> bool:
        return self.queue_ahead >= 0


class OrderManager:
    """Our resting orders and the §6.3 queue model. At most one non-cancelling order per side."""

    def __init__(self, latency_ns: int, order_qty: int, queue_cancel_model: str) -> None:
        if queue_cancel_model not in ("off", "proportional"):
            raise ValueError(f"unknown queue_cancel_model {queue_cancel_model!r}")
        self.latency_ns = latency_ns
        self.order_qty = order_qty
        self.proportional = queue_cancel_model == "proportional"
        self.orders: list[Order] = []
        self.n_rejected = 0  # post-only rejections: order would have crossed at activation

    def current(self, side: int) -> Order | None:
        """The live, not-being-cancelled order on ``side`` (if any)."""
        for o in self.orders:
            if o.side == side and o.ts_cancel is None:
                return o
        return None

    def replace(self, bid: int | None, ask: int | None, ts: int) -> None:
        """Cancel/replace so that each side rests at the given price (None = no order).

        An order whose price is unchanged is left alone and keeps its queue position.
        Cancels and posts both take effect after ``latency_ns``. At most one post may
        be in flight per side: while the current order is still awaiting activation,
        a requote on that side is deferred to the next requote trigger.
        """
        for side, price in ((BID, bid), (ASK, ask)):
            cur = self.current(side)
            if cur is not None and (cur.price == price or not cur.active):
                continue
            if cur is not None:
                cur.ts_cancel = ts + self.latency_ns
            if price is not None:
                self.orders.append(Order(side, price, self.order_qty, ts, ts + self.latency_ns))

    def process(self, ts: int, book: OrderBook) -> None:
        """Expire cancels and activate posts whose latency has elapsed at ``ts``.

        Post-only semantics: an order that would cross the opposite side when it
        arrives (the market moved through its price during the latency window) is
        rejected rather than treated as a resting order the market then trades through.
        """
        keep: list[Order] = []
        for o in self.orders:
            if o.ts_cancel is not None and o.ts_cancel <= ts:
                continue
            if not o.active and o.ts_active <= ts:
                opp = book.best_ask() if o.side == BID else book.best_bid()
                if opp is not None and o.side * (o.price - opp[0]) >= 0:
                    self.n_rejected += 1
                    continue
                best = book.best_bid() if o.side == BID else book.best_ask()
                improves = best is not None and o.side * (o.price - best[0]) > 0
                o.queue_ahead = 0 if improves else book.depth(o.side, o.price)
            keep.append(o)
        self.orders = keep

    def on_trade(self, price: int, qty: int, aggressor: int) -> list[tuple[int, int, int]]:
        """Apply a TRADE to our active orders; returns fills as ``(side, price, qty)``."""
        fills: list[tuple[int, int, int]] = []
        remaining_trade = qty
        for o in self.orders:
            if not o.active or o.side != -aggressor or o.qty_remaining == 0:
                continue
            if o.side * (o.price - price) > 0:  # market traded through our level
                x = o.qty_remaining
            elif price == o.price:
                o.queue_ahead -= remaining_trade
                if o.queue_ahead >= 0:
                    continue
                x = min(-o.queue_ahead, o.qty_remaining)
                o.queue_ahead = 0
                remaining_trade -= x
            else:
                continue
            o.qty_remaining -= x
            fills.append((o.side, o.price, x))
        self.orders = [o for o in self.orders if o.qty_remaining > 0]
        return fills

    def on_level_set(self, side: int, price: int, old_qty: int, new_qty: int) -> None:
        """Optional proportional queue-cancel model; no-op when ``off``."""
        if not self.proportional or new_qty >= old_qty or old_qty <= 0:
            return
        for o in self.orders:
            if o.active and o.side == side and o.price == price and o.queue_ahead > 0:
                o.queue_ahead = (o.queue_ahead * new_qty) // old_qty


def clip_quotes(bid: int, ask: int, book: OrderBook) -> tuple[int, int]:
    """§7.1 post-processing: never cross the market (may quote inside the spread)."""
    if bid >= ask:
        raise ValueError(f"strategy returned bid {bid} >= ask {ask}")
    bb, ba = book.best_bid(), book.best_ask()
    if ba is not None:
        bid = min(bid, ba[0] - 1)
    if bb is not None:
        ask = max(ask, bb[0] + 1)
    return bid, ask


def run(
    stream: Sequence[EventTuple], quoter: Quoter, cfg: EngineConfig, name: str = ""
) -> RunResult:
    """Replay ``stream`` through the book, quote with ``quoter`` inside the window, account PnL.

    ``stream`` must be time-ordered. Events before ``cfg.start_ns`` only update the
    book. Determinism: no randomness is used anywhere in the loop.
    """
    if cfg.latency_ns == 0:
        warnings.warn("latency_ms = 0: fills will be unrealistically favourable", stacklevel=2)
    book = OrderBook()
    om = OrderManager(cfg.latency_ns, cfg.order_qty, cfg.queue_cancel_model)
    tv = cfg.tick_value_minor
    n = len(stream)

    q = 0
    cash = 0
    fees_cum = 0
    last_mid: int | None = None
    last_requote_ts = -1
    last_requote_mid: int | None = None
    last_requote_q = 0
    time_at_cap = 0
    n_out = 0
    prev_ts = 0

    ev = {
        k: np.zeros(n, dtype=np.int64)
        for k in ("ts_ns", "q", "cash", "mid_x2", "fees_cum", "bid_quote", "ask_quote")
    }
    fl: dict[str, list[int]] = {
        k: [] for k in ("ts_ns", "event_idx", "side", "price_ticks", "qty", "mid_x2", "fee")
    }

    for i in range(n):
        ts, kind, side, price, qty, aggressor = stream[i]
        if kind == LEVEL_SET:
            old = book.apply(side, price, qty)
            if ts >= cfg.start_ns:
                om.on_level_set(side, price, old, qty)
        if ts < cfg.start_ns:
            continue
        if ts >= cfg.end_ns:
            break

        mid = book.mid_ticks_x2()
        if mid is None:
            mid = last_mid
        if mid is None:
            continue  # one-sided book before any two-sided state: cannot mark or quote
        if last_mid is None:
            last_mid = mid
            prev_ts = ts
        if abs(q) >= cfg.q_max:
            time_at_cap += ts - prev_ts
        prev_ts = ts
        last_mid = mid

        om.process(ts, book)
        if kind == TRADE:
            for f_side, f_price, f_qty in om.on_trade(price, qty, aggressor):
                fee = fee_minor(f_price, f_qty, tv, cfg.fee_bps)
                cash -= f_side * f_price * f_qty * tv
                q += f_side * f_qty
                fees_cum += fee
                for k, v in (
                    ("ts_ns", ts),
                    ("event_idx", n_out),
                    ("side", f_side),
                    ("price_ticks", f_price),
                    ("qty", f_qty),
                    ("mid_x2", mid),
                    ("fee", fee),
                ):
                    fl[k].append(v)

        cur_bid, cur_ask = om.current(BID), om.current(ASK)
        ev["ts_ns"][n_out] = ts
        ev["q"][n_out] = q
        ev["cash"][n_out] = cash
        ev["mid_x2"][n_out] = mid
        ev["fees_cum"][n_out] = fees_cum
        ev["bid_quote"][n_out] = cur_bid.price if cur_bid else 0
        ev["ask_quote"][n_out] = cur_ask.price if cur_ask else 0
        n_out += 1

        if (
            mid != last_requote_mid
            or q != last_requote_q
            or ts - last_requote_ts >= cfg.requote_interval_ns
        ):
            bid, ask = quoter.quote(book.view(), State(q, cash, ts, cfg.end_ns))
            bid, ask = clip_quotes(bid, ask, book)
            om.replace(bid if q < cfg.q_max else None, ask if q > -cfg.q_max else None, ts)
            last_requote_ts, last_requote_mid, last_requote_q = ts, mid, q

    ev = {k: v[:n_out] for k, v in ev.items()}
    meta = {
        "name": name,
        "start_ns": cfg.start_ns,
        "end_ns": cfg.end_ns,
        "latency_ns": cfg.latency_ns,
        "order_qty": cfg.order_qty,
        "q_max": cfg.q_max,
        "fee_bps": cfg.fee_bps,
        "tick_value_minor": tv,
        "queue_cancel_model": cfg.queue_cancel_model,
        "time_at_cap_ns": time_at_cap,
        "q_final": q,
        "mid_final_x2": last_mid,
        "n_events": n_out,
        "n_rejected": om.n_rejected,
    }
    return finalize(
        ev, {k: np.asarray(v, dtype=np.int64) for k, v in fl.items()}, tv, cfg.as_horizons_s, meta
    )

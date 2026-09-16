"""Shared helpers: hand-built and synthetic event streams for engine tests."""

from __future__ import annotations

import numpy as np

from mm.engine import EngineConfig, EventTuple
from mm.events import ASK, BID, LEVEL_SET, TRADE

MS = 1_000_000
S = 1_000_000_000


def make_cfg(**kw) -> EngineConfig:
    base = dict(
        start_ns=0,
        end_ns=10**18,
        latency_ns=100 * MS,
        requote_interval_ns=500 * MS,
        order_qty=100,
        q_max=500,
        fee_bps=0.0,
        tick_value_minor=1,
        queue_cancel_model="off",
    )
    base.update(kw)
    return EngineConfig(**base)


def level(ts: int, side: int, price: int, qty: int) -> EventTuple:
    return (ts, LEVEL_SET, side, price, qty, 0)


def trade(ts: int, hit_side: int, price: int, qty: int) -> EventTuple:
    """A trade that hits ``hit_side`` of the book (aggressor is the opposite side)."""
    return (ts, TRADE, hit_side, price, qty, -hit_side)


def snapshot(ts: int, bids: dict[int, int], asks: dict[int, int]) -> list[EventTuple]:
    return [level(ts, BID, p, q) for p, q in bids.items()] + [
        level(ts, ASK, p, q) for p, q in asks.items()
    ]


def synthetic_stream(seed: int, n: int, p_trade: float = 0.4, depth: int = 5) -> list[EventTuple]:
    """Random but always-valid (uncrossed) book + trade stream, ts strictly increasing."""
    rng = np.random.default_rng(seed)
    bids = {1000 - i: int(rng.integers(100, 500)) for i in range(depth)}
    asks = {1002 + i: int(rng.integers(100, 500)) for i in range(depth)}
    ts = 0
    out = snapshot(ts, bids, asks)
    for _ in range(n):
        ts += int(rng.integers(1, 200)) * MS
        u = rng.random()
        if u < p_trade:
            side = BID if rng.random() < 0.5 else ASK
            lv = bids if side == BID else asks
            best = max(lv) if side == BID else min(lv)
            qty = int(rng.integers(10, 400))
            out.append(trade(ts, side, best, qty))
            new = max(0, lv[best] - qty)
            out.append(level(ts, side, best, new))
            if new == 0:
                del lv[best]
                if not lv:  # never let a side empty out
                    p = best - 1 if side == BID else best + 1
                    lv[p] = int(rng.integers(100, 500))
                    out.append(level(ts, side, p, lv[p]))
        elif u < 0.7:
            side = BID if rng.random() < 0.5 else ASK
            lv = bids if side == BID else asks
            p = int(rng.choice(list(lv)))
            lv[p] = int(rng.integers(50, 600))
            out.append(level(ts, side, p, lv[p]))
        else:
            bb, ba = max(bids), min(asks)
            if ba - bb > 1:
                side = BID if rng.random() < 0.5 else ASK
                p = bb + 1 if side == BID else ba - 1
                lv = bids if side == BID else asks
                lv[p] = int(rng.integers(50, 300))
                out.append(level(ts, side, p, lv[p]))
            else:
                side = BID if rng.random() < 0.5 else ASK
                lv = bids if side == BID else asks
                p = (bb - depth) if side == BID else (ba + depth)
                lv[p] = int(rng.integers(50, 300))
                out.append(level(ts, side, p, lv[p]))
    return out

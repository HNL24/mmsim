import warnings

import pytest
from conftest import MS, level, make_cfg, snapshot, trade

from mm.book import OrderBook
from mm.engine import OrderManager, clip_quotes, run
from mm.events import ASK, BID
from mm.strategy import NaiveQuoter


def _book(bid_qty=500, ask_qty=500):
    b = OrderBook()
    b.apply(BID, 100, bid_qty)
    b.apply(BID, 99, 300)
    b.apply(ASK, 105, ask_qty)
    b.apply(ASK, 106, 300)
    return b


def _om(**kw):
    return OrderManager(
        latency_ns=100 * MS, order_qty=100, queue_cancel_model=kw.get("model", "off")
    )


def test_trade_through_fills_fully():
    b, om = _book(), _om()
    om.replace(100, 105, ts=0)
    om.process(100 * MS, b)
    assert om.current(BID).queue_ahead == 500
    fills = om.on_trade(price=99, qty=10, aggressor=ASK)  # sell printed below our bid
    assert fills == [(BID, 100, 100)]
    assert om.current(BID) is None


def test_trade_at_level_decrements_queue_then_fills():
    b, om = _book(), _om()
    om.replace(100, 105, ts=0)
    om.process(100 * MS, b)
    assert om.on_trade(100, 300, ASK) == []
    assert om.current(BID).queue_ahead == 200
    assert om.on_trade(100, 250, ASK) == [(BID, 100, 50)]  # 50 through the queue
    o = om.current(BID)
    assert o.queue_ahead == 0 and o.qty_remaining == 50
    assert om.on_trade(100, 30, ASK) == [(BID, 100, 30)]
    assert om.current(BID).qty_remaining == 20


def test_trade_on_wrong_side_does_nothing():
    b, om = _book(), _om()
    om.replace(100, 105, ts=0)
    om.process(100 * MS, b)
    assert om.on_trade(100, 1000, BID) == []  # buyer aggressor cannot hit our bid
    assert om.on_trade(105, 1000, ASK) == []  # seller aggressor cannot lift our ask
    assert om.current(BID).queue_ahead == 500


def test_order_inactive_before_ts_active():
    b, om = _book(), _om()
    om.replace(100, 105, ts=0)
    om.process(50 * MS, b)
    assert not om.current(BID).active
    assert om.on_trade(90, 1000, ASK) == []
    om.process(100 * MS, b)
    assert om.on_trade(90, 1000, ASK) == [(BID, 100, 100)]


def test_order_that_would_cross_at_activation_is_rejected():
    b, om = _book(), _om()
    om.replace(100, 105, ts=0)
    b.apply(ASK, 105, 0)
    b.apply(ASK, 100, 50)  # market falls: best ask now at our bid price
    om.process(100 * MS, b)
    assert om.current(BID) is None and om.n_rejected == 1
    assert om.current(ASK).active  # ask at 105 does not cross the bid at 100
    assert om.on_trade(99, 10, ASK) == []  # nothing rests, so nothing fills


def test_improving_order_has_zero_queue():
    b, om = _book(), _om()
    om.replace(101, 104, ts=0)  # inside the 100/105 spread
    om.process(100 * MS, b)
    assert om.current(BID).queue_ahead == 0
    assert om.current(ASK).queue_ahead == 0


def test_unchanged_requote_keeps_queue_position():
    b, om = _book(), _om()
    om.replace(100, 105, ts=0)
    om.process(100 * MS, b)
    om.on_trade(100, 300, ASK)
    o = om.current(BID)
    om.replace(100, 105, ts=200 * MS)
    assert om.current(BID) is o and o.queue_ahead == 200
    assert len(om.orders) == 2


def test_changed_requote_resets_queue_and_cancels_with_latency():
    b, om = _book(), _om()
    om.replace(100, 105, ts=0)
    om.process(100 * MS, b)
    om.on_trade(100, 300, ASK)
    old = om.current(BID)
    om.replace(99, 105, ts=200 * MS)
    assert old.ts_cancel == 300 * MS
    new = om.current(BID)
    assert new is not old and new.price == 99 and not new.active
    om.process(250 * MS, b)  # old still live and fillable during cancel latency
    assert om.on_trade(98, 1, ASK) == [(BID, 100, 100)]
    om.process(300 * MS, b)
    assert om.current(BID).queue_ahead == 300 and len(om.orders) == 2


def test_requote_deferred_while_post_in_flight():
    b, om = _book(), _om()
    om.replace(100, 105, ts=0)
    om.replace(99, 105, ts=50 * MS)  # bid still in flight: deferred
    assert [o.price for o in om.orders] == [100, 105]
    om.process(100 * MS, b)
    om.replace(99, 105, ts=120 * MS)  # now acknowledged: replaced
    assert [(o.price, o.ts_cancel) for o in om.orders] == [(100, 220 * MS), (105, None), (99, None)]


def test_cancelled_order_removed_at_cancel_time():
    b, om = _book(), _om()
    om.replace(100, 105, ts=0)
    om.process(100 * MS, b)
    om.replace(None, 105, ts=200 * MS)
    om.process(300 * MS, b)
    assert [o.side for o in om.orders] == [ASK]


def test_proportional_queue_cancel_model():
    b, om = _book(), _om(model="proportional")
    om.replace(100, 105, ts=0)
    om.process(100 * MS, b)
    om.on_level_set(BID, 100, old_qty=500, new_qty=250)
    assert om.current(BID).queue_ahead == 250
    om.on_level_set(BID, 100, old_qty=250, new_qty=400)  # increase: no effect
    assert om.current(BID).queue_ahead == 250
    om2 = _om()
    om2.replace(100, 105, ts=0)
    om2.process(100 * MS, b)
    om2.on_level_set(BID, 100, 500, 250)
    assert om2.current(BID).queue_ahead == 500  # off: cancellations assumed behind us


def test_clip_quotes_never_crosses():
    b = _book()
    assert clip_quotes(90, 120, b) == (90, 120)
    assert clip_quotes(107, 110, b) == (104, 110)
    assert clip_quotes(95, 97, b) == (95, 101)
    with pytest.raises(ValueError):
        clip_quotes(100, 100, b)


def _capped_stream():
    """Sellers keep hitting the bid at 100; our bid at 100 is filled repeatedly."""
    ev = snapshot(0, {100: 10, 99: 1000}, {102: 1000, 103: 1000})  # mid 101 -> bid 100, ask 102
    ts = 0
    for _ in range(12):
        ts += 600 * MS
        ev.append(trade(ts, BID, 100, 500))
        ev.append(level(ts, BID, 100, 10))
    return ev


def test_inventory_cap_suppresses_bid_only():
    cfg = make_cfg(q_max=250, order_qty=100)
    res = run(_capped_stream(), NaiveQuoter(1), cfg)
    q = res.events["q"]
    assert q.max() >= 250
    at_cap = res.events[q >= 250]
    assert (at_cap["bid_quote"] == 0).iloc[-1]
    assert (at_cap["ask_quote"] > 0).all()
    assert res.meta["time_at_cap_ns"] > 0
    assert res.fills["side"].eq(BID).all()


def test_fees_charged_on_every_fill():
    cfg = make_cfg(fee_bps=10.0)
    res = run(_capped_stream(), NaiveQuoter(1), cfg)
    assert len(res.fills) > 0
    assert (res.fills["fee"] > 0).all()
    assert res.events["fees_cum"].iloc[-1] == res.fills["fee"].sum()


def test_zero_latency_warns():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        run(_capped_stream(), NaiveQuoter(1), make_cfg(latency_ns=0))
    assert any("latency" in str(x.message) for x in w)


def test_warmup_events_before_window_only_build_book():
    ev = snapshot(0, {100: 10}, {101: 10}) + [
        level(5 * MS, BID, 100, 20),
        level(10 * MS, ASK, 101, 5),
    ]
    res = run(ev, NaiveQuoter(1), make_cfg(start_ns=10 * MS))
    assert len(res.events) == 1 and res.events["ts_ns"].iloc[0] == 10 * MS

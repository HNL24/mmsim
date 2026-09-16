from hypothesis import given, settings
from hypothesis import strategies as st

from mm.book import OrderBook
from mm.events import ASK, BID


def test_apply_set_and_delete():
    b = OrderBook()
    assert b.apply(BID, 100, 50) == 0
    assert b.apply(BID, 100, 70) == 50
    assert b.depth(BID, 100) == 70
    assert b.apply(BID, 100, 0) == 70
    assert b.depth(BID, 100) == 0
    assert b.best_bid() is None


def test_best_maintenance_and_mid():
    b = OrderBook()
    b.apply(BID, 100, 10)
    b.apply(BID, 99, 20)
    b.apply(ASK, 103, 5)
    b.apply(ASK, 101, 7)
    assert b.best_bid() == (100, 10)
    assert b.best_ask() == (101, 7)
    assert b.mid_ticks_x2() == 201
    b.apply(BID, 100, 0)
    assert b.best_bid() == (99, 20)
    b.apply(ASK, 101, 0)
    assert b.best_ask() == (103, 5)
    assert b.mid_ticks_x2() == 202
    b.apply(ASK, 102, 1)
    assert b.best_ask() == (102, 1)


def test_one_sided_book_has_no_mid():
    b = OrderBook()
    b.apply(BID, 100, 10)
    assert b.mid_ticks_x2() is None
    assert b.view().mid_x2 is None


def test_view_is_sorted_best_first_and_frozen():
    b = OrderBook()
    for p, q in ((98, 1), (100, 2), (99, 3)):
        b.apply(BID, p, q)
    for p, q in ((105, 1), (103, 2), (104, 3)):
        b.apply(ASK, p, q)
    v = b.view()
    assert v.bids == ((100, 2), (99, 3), (98, 1))
    assert v.asks == ((103, 2), (104, 3), (105, 1))
    assert v.best_bid == (100, 2) and v.best_ask == (103, 2)
    assert v.mid_x2 == 203
    assert v.depth(ASK, 104) == 3 and v.depth(BID, 104) == 0
    b.apply(BID, 100, 0)
    assert v.best_bid == (100, 2)  # snapshot unaffected by later mutation


def test_bad_side_or_qty_rejected():
    b = OrderBook()
    import pytest

    with pytest.raises(ValueError):
        b.apply(0, 1, 1)
    with pytest.raises(ValueError):
        b.apply(BID, 1, -1)


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.tuples(st.sampled_from([BID, ASK]), st.integers(1, 30), st.integers(0, 50)),
        min_size=1,
        max_size=60,
    )
)
def test_cached_bests_match_dicts_and_never_cross(ops):
    b = OrderBook()
    for side, p, q in ops:
        price = p if side == BID else 100 + p  # bids in [1,30], asks in [101,130]
        b.apply(side, price, q)
        bb, ba = b.best_bid(), b.best_ask()
        assert bb == ((max(b.bids), b.bids[max(b.bids)]) if b.bids else None)
        assert ba == ((min(b.asks), b.asks[min(b.asks)]) if b.asks else None)
        if bb and ba:
            assert bb[0] < ba[0]
            assert b.mid_ticks_x2() == bb[0] + ba[0]

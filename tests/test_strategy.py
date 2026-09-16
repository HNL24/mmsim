import pytest

from mm.book import BookView
from mm.strategy import NaiveQuoter, State, to_grid

STATE = State(q=0, cash=0, ts_ns=0, t_end_ns=1)


def test_naive_symmetric_even_mid():
    v = BookView(bids=((100, 1),), asks=((104, 1),))  # mid 102
    assert NaiveQuoter(2).quote(v, STATE) == (100, 104)


def test_naive_symmetric_half_tick_mid_rounds_outward():
    v = BookView(bids=((100, 1),), asks=((103, 1),))  # mid 101.5
    assert NaiveQuoter(1).quote(v, STATE) == (100, 103)


def test_naive_ignores_inventory():
    v = BookView(bids=((100, 1),), asks=((104, 1),))
    assert NaiveQuoter(2).quote(v, State(500, 0, 0, 1)) == NaiveQuoter(2).quote(
        v, State(-500, 0, 0, 1)
    )


def test_naive_rejects_bad_params_and_one_sided_book():
    with pytest.raises(ValueError):
        NaiveQuoter(0)
    with pytest.raises(ValueError):
        NaiveQuoter(1).quote(BookView(bids=(), asks=((1, 1),)), STATE)


def test_to_grid():
    assert to_grid(99.9, 100.1) == (99, 101)
    assert to_grid(100.0, 101.0) == (100, 101)

import numpy as np
from conftest import make_cfg, synthetic_stream
from hypothesis import given, settings
from hypothesis import strategies as st

from mm.engine import run
from mm.pnl import check_identity, inventory_carry_x2, mark_to_market_x2, spread_capture_x2
from mm.strategy import NaiveQuoter


def _assert_identity_every_event(res):
    ev, fl = res.events, res.fills
    sc_cum = np.zeros(len(ev), dtype=np.int64)
    if len(fl):
        np.add.at(sc_cum, fl["event_idx"].to_numpy(), fl["sc_x2"].to_numpy())
        sc_cum = np.cumsum(sc_cum)
    rhs = sc_cum + ev["ic_x2"].to_numpy() - 2 * ev["fees_cum"].to_numpy()
    assert np.array_equal(ev["pnl_x2"].to_numpy(), rhs)


def test_identity_on_synthetic_stream_with_fees():
    res = run(synthetic_stream(seed=1, n=2000), NaiveQuoter(1), make_cfg(fee_bps=2.5))
    assert len(res.fills) > 20
    assert res.events["fees_cum"].iloc[-1] > 0
    assert check_identity(res)
    _assert_identity_every_event(res)


@settings(max_examples=40, deadline=None)
@given(
    seed=st.integers(0, 10**6),
    n=st.integers(20, 300),
    h=st.integers(1, 3),
    fee=st.sampled_from([0.0, 1.0, 7.5]),
    q_max=st.integers(100, 600),
)
def test_identity_random_streams(seed, n, h, fee, q_max):
    res = run(synthetic_stream(seed, n), NaiveQuoter(h), make_cfg(fee_bps=fee, q_max=q_max))
    assert check_identity(res)
    _assert_identity_every_event(res)


@settings(max_examples=100, deadline=None)
@given(
    mids=st.lists(st.integers(150, 250), min_size=2, max_size=40),
    fills=st.lists(
        st.tuples(
            st.integers(0, 39),
            st.sampled_from([1, -1]),
            st.integers(70, 130),
            st.integers(1, 50),
            st.integers(0, 9),
        ),
        max_size=30,
    ),
    tv=st.sampled_from([1, 5]),
)
def test_pure_decomposition_identity(mids, fills, tv):
    """Random mid path + random fills: MTM == SC + IC - fees, exactly."""
    n = len(mids)
    mid = np.asarray(mids, dtype=np.int64)
    fills = [(i % n, s, p, x, f) for i, s, p, x, f in fills]
    fills.sort(key=lambda t: t[0])
    q = np.zeros(n, dtype=np.int64)
    cash = np.zeros(n, dtype=np.int64)
    fees = np.zeros(n, dtype=np.int64)
    for i, s, p, x, f in fills:
        q[i:] += s * x
        cash[i:] -= s * p * x * tv
        fees[i:] += f
    q_before = np.concatenate(([0], q[:-1]))
    ic = inventory_carry_x2(q_before, mid, tv)
    fi = np.asarray([t[0] for t in fills], dtype=np.int64)
    sc = spread_capture_x2(
        np.asarray([t[1] for t in fills], dtype=np.int64),
        np.asarray([t[2] for t in fills], dtype=np.int64),
        np.asarray([t[3] for t in fills], dtype=np.int64),
        mid[fi] if len(fills) else mid[:0],
        tv,
    )
    pnl = mark_to_market_x2(cash, q, mid, fees, tv)
    assert pnl[-1] == sc.sum() + ic[-1] - 2 * fees[-1]

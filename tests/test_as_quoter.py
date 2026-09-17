import math

import pytest

from mm.book import BookView
from mm.strategy import ASQuoter, State
from mm.vol import ConstantVol

BOOK = BookView(bids=((990, 100),), asks=((1010, 100),))  # mid 1000
PARAMS = dict(gamma=0.001, k=0.1, tau0_s=60.0)


def _q(sigma=2.0, **kw):
    p = {**PARAMS, **kw}
    return ASQuoter(
        p["gamma"],
        p["k"],
        ConstantVol(sigma),
        tau0_s=p["tau0_s"],
        horizon=p.get("horizon", "constant"),
        imbalance_beta_ticks=p.get("imbalance_beta_ticks", 0.0),
    )


def _state(q, ts=0, t_end=10**12):
    return State(q=q, cash=0, ts_ns=ts, t_end_ns=t_end)


def test_symmetric_about_mid_at_zero_inventory():
    qt = _q()
    bid, ask = qt.quote(BOOK, _state(0))
    half = qt.optimal_spread(2.0, 60.0) / 2
    assert bid == math.floor(1000 - half) and ask == math.ceil(1000 + half)
    assert bid < ask


def test_spread_formula():
    qt = _q()
    g, k, s, t = 0.001, 0.1, 2.0, 60.0
    assert qt.optimal_spread(s, t) == pytest.approx(g * s * s * t + 2 / g * math.log(1 + g / k))


def test_positive_inventory_lowers_both_quotes():
    qt = _q(sigma=3.0)
    b0, a0 = qt.quote(BOOK, _state(0))
    b1, a1 = qt.quote(BOOK, _state(300))
    b2, a2 = qt.quote(BOOK, _state(-300))
    assert b1 < b0 and a1 < a0
    assert b2 > b0 and a2 > a0
    assert (a1 - b1) == (a0 - b0) or abs((a1 - b1) - (a0 - b0)) <= 1  # skew, not widening


def test_larger_gamma_widens_spread_when_risk_term_dominates():
    # d(delta*)/d(gamma) = sigma^2 tau - (decreasing term); holds once sigma^2 tau is material.
    assert _q(sigma=3.0, gamma=0.002).optimal_spread(3.0, 60) > _q(
        sigma=3.0, gamma=0.001
    ).optimal_spread(3.0, 60)


def test_finite_horizon_skew_fades_at_end():
    qt = _q(sigma=3.0, horizon="finite")
    t_end = 1000 * 10**9
    b_early, _ = qt.quote(BOOK, _state(300, ts=0, t_end=t_end))
    b_late, a_late = qt.quote(BOOK, _state(300, ts=t_end, t_end=t_end))
    assert b_early < b_late
    assert b_late < 1000 < a_late  # only the (2/gamma) ln(1 + gamma/k) term is left


def test_units_assertion_fires_on_mis_scaled_sigma():
    with pytest.raises(ValueError, match="units"):
        ASQuoter(0.001, 0.1, ConstantVol(2.0 * math.sqrt(1000)), tau0_s=60.0)  # sigma per sqrt(ms)
    with pytest.raises(ValueError, match="units"):
        ASQuoter(0.001, 0.1, ConstantVol(2.0), tau0_s=60_000.0)  # tau in ms
    with pytest.raises(ValueError, match="units"):
        ASQuoter(50.0, 0.1, ConstantVol(0.0), tau0_s=60.0)  # gamma huge: spread < 0.5 tick


def test_sigma_ref_overrides_estimator_for_units_check():
    from mm.vol import RollingRV

    ASQuoter(0.001, 0.1, RollingRV(300), sigma_ref=2.0)  # RollingRV reports 0 before warm-up
    with pytest.raises(ValueError):
        ASQuoter(0.001, 0.1, RollingRV(300), sigma_ref=200.0)


def test_bad_params_rejected():
    with pytest.raises(ValueError):
        ASQuoter(0.0, 0.1, ConstantVol(1.0))
    with pytest.raises(ValueError):
        ASQuoter(0.001, 0.1, ConstantVol(1.0), horizon="weird")


def test_imbalance_beta_shifts_quotes_toward_pressure():
    from mm.strategy import book_imbalance

    heavy_bid = BookView(bids=((990, 300),), asks=((1010, 100),))  # imbalance +0.5
    heavy_ask = BookView(bids=((990, 100),), asks=((1010, 300),))  # imbalance -0.5
    assert book_imbalance(heavy_bid) == 0.5 and book_imbalance(heavy_ask) == -0.5
    assert book_imbalance(BookView(bids=(), asks=((1010, 5),))) == 0.0
    plain = _q(sigma=3.0)
    b0, a0 = plain.quote(heavy_bid, _state(0))
    assert _q(sigma=3.0, imbalance_beta_ticks=0.0).quote(heavy_bid, _state(0)) == (b0, a0)
    skew = ASQuoter(0.001, 0.1, ConstantVol(3.0), tau0_s=60.0, imbalance_beta_ticks=4.0)
    b_up, a_up = skew.quote(heavy_bid, _state(0))
    b_dn, a_dn = skew.quote(heavy_ask, _state(0))
    assert b_up == b0 + 2 and a_up == a0 + 2  # +4 ticks * 0.5
    assert b_dn == b0 - 2 and a_dn == a0 - 2

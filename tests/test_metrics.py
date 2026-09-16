import math

import numpy as np
import pandas as pd
import pytest
from conftest import make_cfg, synthetic_stream

from mm.engine import run
from mm.metrics import max_drawdown, per_minute_pnl, realised_unrealised_x2, summarize
from mm.pnl import RunResult
from mm.strategy import NaiveQuoter


@pytest.fixture(scope="module")
def res():
    return run(synthetic_stream(seed=2, n=4000), NaiveQuoter(1), make_cfg(fee_bps=1.0, q_max=300))


def test_max_drawdown():
    assert max_drawdown(np.array([1.0, 3.0, 2.0, 5.0, 1.0, 4.0])) == 4.0
    assert max_drawdown(np.array([-1.0, -3.0])) == 3.0  # from the zero start
    assert max_drawdown(np.array([])) == 0.0


def test_realised_plus_unrealised_equals_pre_fee_pnl(res):
    r, u = realised_unrealised_x2(res)
    total = int(res.events["pnl_x2"].iloc[-1]) + 2 * int(res.events["fees_cum"].iloc[-1])
    assert r + u == pytest.approx(total, abs=1e-6 * max(1, abs(total)))


def test_realised_unrealised_hand_case():
    # buy 100 @ 10, sell 60 @ 12, sell 60 @ 9 (flips to -20 short), final mid 8
    fills = pd.DataFrame({"side": [1, -1, -1], "price_ticks": [10, 12, 9], "qty": [100, 60, 60]})
    events = pd.DataFrame({"mid_x2": [16]})
    r, u = realised_unrealised_x2(RunResult(events, fills, {"tick_value_minor": 1}))
    # realised: 60*(12-10) + 40*(9-10) = 120 - 40 = 80 tick-shares -> doubled 160
    # unrealised: short 20 @ 9, mid 8 -> +20 -> doubled 40
    assert r == pytest.approx(160) and u == pytest.approx(40)


def test_per_minute_pnl_sums_to_total(res):
    pm = per_minute_pnl(res)
    assert pm.sum() == pytest.approx(res.events["pnl_x2"].iloc[-1] / 200)


def test_summarize_keys_and_consistency(res):
    m = summarize(res)
    for k in (
        "total_pnl",
        "terminal_inventory_pnl",
        "pnl_per_fill",
        "n_fills",
        "fills_per_min",
        "quoted_half_spread",
        "realised_half_spread_5s",
        "adverse_selection_5s_ticks",
        "inventory_mean",
        "inventory_std",
        "inventory_max_abs",
        "frac_time_at_cap",
        "sharpe_per_min",
        "max_drawdown",
        "total_fees",
        "spread_capture",
        "inventory_carry",
    ):
        assert k in m and not (isinstance(m[k], float) and math.isnan(m[k])), k
    assert m["total_pnl"] == pytest.approx(m["spread_capture"] + m["inventory_carry"] + m["fees"])
    assert m["total_pnl"] == pytest.approx(
        m["realised_pnl"] + m["terminal_inventory_pnl"] - m["total_fees"]
    )
    assert m["total_fees"] > 0 and m["fees"] == -m["total_fees"]
    assert 0 <= m["frac_time_at_cap"] <= 1
    assert m["quoted_half_spread"] >= 1
    assert m["inventory_max_abs"] >= abs(m["terminal_inventory"])

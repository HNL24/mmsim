import pandas as pd
import pytest
from conftest import make_cfg, synthetic_stream

from mm.engine import run
from mm.events import LEVEL_SET
from mm.strategy import ASQuoter, NaiveQuoter
from mm.vol import ConstantVol, RollingRV


class GuardedStream:
    """Sequence that raises on any access beyond the next sequential index."""

    def __init__(self, events):
        self._ev = events
        self.cursor = -1

    def __len__(self):
        return len(self._ev)

    def __getitem__(self, i):
        if i > self.cursor + 1:
            raise LookupError(f"lookahead: index {i} requested at cursor {self.cursor}")
        self.cursor = max(self.cursor, i)
        return self._ev[i]


def _quoters():
    return [
        NaiveQuoter(1),
        NaiveQuoter(3),
        ASQuoter(0.01, 0.5, ConstantVol(1.0), tau0_s=30.0),
        ASQuoter(0.01, 0.5, RollingRV(window_s=5, sample_s=0.5), tau0_s=30.0, sigma_ref=1.0),
    ]


QUOTERS = _quoters()


@pytest.mark.parametrize("quoter", QUOTERS)
def test_engine_never_reads_ahead(quoter):
    g = GuardedStream(synthetic_stream(seed=7, n=1500))
    res = run(g, quoter, make_cfg())
    assert g.cursor == len(g) - 1
    if getattr(quoter, "half_spread_ticks", 0) == 1:
        assert len(res.fills) > 0


def _perturb(events, from_idx):
    """Shift every event after ``from_idx`` by +3 ticks and change quantities; keep time order."""
    out = list(events[: from_idx + 1])
    for ts, kind, side, price, qty, agg in events[from_idx + 1 :]:
        new_qty = qty + 7 if kind == LEVEL_SET and qty > 0 else qty
        out.append((ts, kind, side, price + 3, new_qty, agg))
    return out


@pytest.mark.parametrize("idx", range(len(QUOTERS)))
def test_past_is_bit_identical_under_future_perturbation(idx):
    quoter, quoter_b = _quoters()[idx], _quoters()[idx]
    base = synthetic_stream(seed=11, n=1500)
    i_star = len(base) // 2
    while base[i_star][0] == base[i_star + 1][0]:  # perturb from a timestamp boundary
        i_star += 1
    t_star = base[i_star][0]
    res_a = run(base, quoter, make_cfg())
    res_b = run(_perturb(base, i_star), quoter_b, make_cfg())
    past_a = res_a.events[res_a.events["ts_ns"] <= t_star]
    past_b = res_b.events[res_b.events["ts_ns"] <= t_star]
    assert len(past_a) > 100
    assert not res_a.events.equals(res_b.events)  # the future really was perturbed
    pd.testing.assert_frame_equal(past_a, past_b)
    # Adverse-selection columns look forward by construction (a sub-attribution), so exclude them.
    cols = [c for c in res_a.fills.columns if not c.startswith("as_")]
    fa = res_a.fills[res_a.fills["ts_ns"] <= t_star][cols].reset_index(drop=True)
    fb = res_b.fills[res_b.fills["ts_ns"] <= t_star][cols].reset_index(drop=True)
    if getattr(quoter, "half_spread_ticks", 0) == 1:
        assert len(fa) > 0
    pd.testing.assert_frame_equal(fa, fb)

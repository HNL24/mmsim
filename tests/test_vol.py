import math

import pytest

from mm.vol import ConstantVol, RollingRV, realised_vol

S = 1_000_000_000


def test_constant_vol():
    v = ConstantVol(1.5)
    v.update(0, 200)
    assert v.sigma() == 1.5
    with pytest.raises(ValueError):
        ConstantVol(-1)


def test_rolling_rv_recovers_constant_increments():
    rv = RollingRV(window_s=10, sample_s=1)
    for i in range(20):
        rv.update(i * S, 200 + 4 * i)  # mid rises 2 ticks per second
    assert rv.sigma() == pytest.approx(2.0)


def test_rolling_rv_ignores_sub_sample_updates():
    rv = RollingRV(window_s=10, sample_s=1)
    rv.update(0, 200)
    rv.update(S // 2, 10_000)  # too soon: not sampled
    rv.update(S, 202)
    assert rv.sigma() == pytest.approx(1.0)


def test_rolling_rv_window_drops_old_samples():
    rv = RollingRV(window_s=3, sample_s=1)
    for i, m in enumerate([200, 220, 220, 220, 220, 220]):  # one big jump then flat
        rv.update(i * S, m)
    assert rv.sigma() == pytest.approx(0.0)


def test_rolling_rv_irregular_sampling_is_a_rate():
    rv = RollingRV(window_s=100, sample_s=1)
    rv.update(0, 200)
    rv.update(4 * S, 208)  # +4 ticks over 4 s -> 16/4 = 4 ticks^2/s
    assert rv.sigma() == pytest.approx(2.0)


def test_rolling_rv_warmup_is_zero():
    rv = RollingRV(window_s=10)
    assert rv.sigma() == 0.0
    rv.update(0, 200)
    assert rv.sigma() == 0.0


def test_realised_vol_offline_matches_rolling():
    ts = [i * S for i in range(50)]
    mid = [200 + (6 if i % 2 else 0) for i in range(50)]  # alternates 3 ticks up/down
    assert realised_vol(ts, mid) == pytest.approx(math.sqrt(9.0))

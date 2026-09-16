import pandas as pd
from conftest import make_cfg, synthetic_stream

from mm.engine import run
from mm.strategy import NaiveQuoter


def test_same_config_same_data_same_result():
    stream = synthetic_stream(seed=3, n=2000)
    a = run(stream, NaiveQuoter(1), make_cfg(fee_bps=1.0), name="a")
    b = run(stream, NaiveQuoter(1), make_cfg(fee_bps=1.0), name="a")
    pd.testing.assert_frame_equal(a.events, b.events)
    pd.testing.assert_frame_equal(a.fills, b.fills)
    assert a.meta == b.meta

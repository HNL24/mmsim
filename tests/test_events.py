import pandas as pd
import pytest

from mm.events import (
    ASK,
    BID,
    EVENT_COLUMNS,
    EVENT_SCHEMA,
    LEVEL_SET,
    TRADE,
    Event,
    events_to_frame,
    validate_events,
)


def test_schema_matches_columns():
    assert tuple(EVENT_SCHEMA.names) == EVENT_COLUMNS


def test_valid_stream_round_trips():
    evs = [
        Event(ts_ns=10, seq=0, kind=TRADE, side=BID, price_ticks=100, qty=5, aggressor=ASK),
        Event(ts_ns=10, seq=1, kind=LEVEL_SET, side=BID, price_ticks=100, qty=0),
        Event(ts_ns=11, seq=2, kind=LEVEL_SET, side=ASK, price_ticks=101, qty=20),
    ]
    df = events_to_frame(evs)
    validate_events(df)
    assert list(df.columns) == list(EVENT_COLUMNS)
    assert str(df["kind"].dtype) == "uint8"


def test_empty_frame_is_valid():
    validate_events(events_to_frame([]))


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(kind=5, side=BID, price_ticks=1, qty=1),
        dict(kind=LEVEL_SET, side=0, price_ticks=1, qty=1),
        dict(kind=LEVEL_SET, side=BID, price_ticks=1, qty=-1),
        dict(kind=TRADE, side=BID, price_ticks=1, qty=1),  # missing aggressor
        dict(kind=TRADE, side=BID, price_ticks=1, qty=0, aggressor=ASK),
        dict(kind=LEVEL_SET, side=BID, price_ticks=1, qty=1, aggressor=BID),
    ],
)
def test_invalid_event_rejected(kwargs):
    with pytest.raises(ValueError):
        Event(ts_ns=0, seq=0, **kwargs)


def test_decreasing_ts_rejected():
    df = events_to_frame(
        [
            Event(ts_ns=2, seq=0, kind=LEVEL_SET, side=BID, price_ticks=1, qty=1),
            Event(ts_ns=1, seq=1, kind=LEVEL_SET, side=BID, price_ticks=1, qty=1),
        ]
    )
    with pytest.raises(ValueError, match="ts_ns"):
        validate_events(df)


def test_non_increasing_seq_rejected():
    df = events_to_frame(
        [
            Event(ts_ns=1, seq=0, kind=LEVEL_SET, side=BID, price_ticks=1, qty=1),
            Event(ts_ns=1, seq=0, kind=LEVEL_SET, side=BID, price_ticks=1, qty=1),
        ]
    )
    with pytest.raises(ValueError, match="seq"):
        validate_events(df)


def test_wrong_dtype_rejected():
    df = events_to_frame([Event(ts_ns=1, seq=0, kind=LEVEL_SET, side=BID, price_ticks=1, qty=1)])
    df["price_ticks"] = df["price_ticks"].astype("float64")
    with pytest.raises(ValueError, match="dtype"):
        validate_events(df)


def test_missing_column_rejected():
    with pytest.raises(ValueError, match="missing"):
        validate_events(pd.DataFrame({"ts_ns": [1]}))

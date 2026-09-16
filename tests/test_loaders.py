from pathlib import Path

import numpy as np
import pytest

from mm.events import ASK, BID, LEVEL_SET, TRADE, validate_events
from mm.loaders import load_lobster, lobster_time_to_ns

MIDNIGHT = 1_340_251_200_000_000_000  # 2012-06-21 00:00 EDT
S = 9_999_999_999  # ask sentinel
B = -9_999_999_999  # bid sentinel


def _write(tmp_path: Path, messages: list[str], books: list[str]) -> tuple[Path, Path]:
    m = tmp_path / "m.csv"
    o = tmp_path / "o.csv"
    m.write_text("\n".join(messages) + "\n")
    o.write_text("\n".join(books) + "\n")
    return m, o


def _book(ask1, bid1, ask2=(S, 0), bid2=(B, 0)):
    return ",".join(str(x) for x in (*ask1, *bid1, *ask2, *bid2))


@pytest.mark.parametrize(
    "s, expected",
    [
        ("34200", MIDNIGHT + 34200 * 10**9),
        ("34200.5", MIDNIGHT + 34200 * 10**9 + 500_000_000),
        ("34200.004241176", MIDNIGHT + 34200 * 10**9 + 4_241_176),
        ("34200.123456789", MIDNIGHT + 34200 * 10**9 + 123_456_789),
    ],
)
def test_time_to_ns_exact(s, expected):
    assert lobster_time_to_ns(s, MIDNIGHT) == expected


def test_time_to_ns_rejects_sub_ns():
    with pytest.raises(ValueError):
        lobster_time_to_ns("1.0000000001", 0)


def test_lobster_fixture_emits_canonical_stream(tmp_path):
    # Row 0: submit buy 18 @ 585.33 -> initial snapshot ask 585.94x200, bid 585.33x18
    # Row 1: submit sell 50 @ 585.90 (new best ask, inside)
    # Row 2: visible execution of resting sell 30 @ 585.90 (buyer aggressor); ask qty -> 20
    # Row 3: hidden execution at midpoint (off grid) -> dropped, book unchanged
    # Row 4: delete bid 18 @ 585.33 -> bid level removed, deeper bid scrolls in
    messages = [
        "34200.004241176,1,1,18,5853300,1",
        "34200.10,1,2,50,5859000,-1",
        "34200.20,4,2,30,5859000,-1",
        "34200.25,5,9,10,5859050,-1",
        "34200.30,3,1,18,5853300,1",
    ]
    books = [
        _book((5859400, 200), (5853300, 18)),
        _book((5859000, 50), (5853300, 18), (5859400, 200)),
        _book((5859000, 20), (5853300, 18), (5859400, 200)),
        _book((5859000, 20), (5853300, 18), (5859400, 200)),
        _book((5859000, 20), (5853000, 150), (5859400, 200)),
    ]
    m, o = _write(tmp_path, messages, books)
    ev, per_row = load_lobster(m, o, MIDNIGHT, levels=2, units_per_tick=100)
    validate_events(ev)

    assert per_row.tolist() == [2, 1, 2, 0, 2]
    rows = ev.to_dict("records")
    # row 0 snapshot
    assert rows[0]["ts_ns"] == MIDNIGHT + 34200 * 10**9 + 4_241_176
    assert {(r["side"], r["price_ticks"], r["qty"]) for r in rows[:2]} == {
        (BID, 58533, 18),
        (ASK, 58594, 200),
    }
    # row 1: new ask level
    assert rows[2]["kind"] == LEVEL_SET and (
        rows[2]["side"],
        rows[2]["price_ticks"],
        rows[2]["qty"],
    ) == (ASK, 58590, 50)
    # row 2: TRADE first (side = resting ask, aggressor = buyer), then LEVEL_SET
    assert rows[3]["kind"] == TRADE
    assert (rows[3]["side"], rows[3]["aggressor"], rows[3]["price_ticks"], rows[3]["qty"]) == (
        ASK,
        BID,
        58590,
        30,
    )
    assert rows[4]["kind"] == LEVEL_SET and (rows[4]["price_ticks"], rows[4]["qty"]) == (58590, 20)
    assert rows[3]["ts_ns"] == rows[4]["ts_ns"] == MIDNIGHT + 34200 * 10**9 + 200_000_000
    # row 4: old bid removed (qty 0), new bid appears
    assert (rows[5]["side"], rows[5]["price_ticks"], rows[5]["qty"]) == (BID, 58533, 0)
    assert (rows[6]["side"], rows[6]["price_ticks"], rows[6]["qty"]) == (BID, 58530, 150)
    assert ev["seq"].tolist() == list(range(7))
    assert np.all(np.diff(ev["ts_ns"].to_numpy()) >= 0)


def test_hidden_on_grid_execution_is_a_trade(tmp_path):
    messages = ["34200.0,1,1,10,1000000,1", "34201.0,5,7,5,1000000,1"]
    books = [_book((1000100, 5), (1000000, 10)), _book((1000100, 5), (1000000, 10))]
    m, o = _write(tmp_path, messages, books)
    ev, per_row = load_lobster(m, o, MIDNIGHT, levels=2)
    assert per_row.tolist() == [2, 1]
    tr = ev.iloc[-1]
    assert tr["kind"] == TRADE and tr["side"] == BID and tr["aggressor"] == ASK and tr["qty"] == 5


def test_row_count_mismatch_raises(tmp_path):
    m, o = _write(
        tmp_path,
        ["34200.0,1,1,10,1000000,1", "34201.0,1,2,10,1000100,-1"],
        [_book((1000100, 5), (1000000, 10))],
    )
    with pytest.raises(ValueError, match="rows"):
        load_lobster(m, o, MIDNIGHT, levels=2)


def test_halt_message_raises(tmp_path):
    m, o = _write(tmp_path, ["34200.0,7,0,0,-1,-1"], [_book((1000100, 5), (1000000, 10))])
    with pytest.raises(ValueError, match="halt"):
        load_lobster(m, o, MIDNIGHT, levels=2)

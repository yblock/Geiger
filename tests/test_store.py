import pytest

from geiger.calibration import Calibration
from geiger.store import DEVICE, LIVE, Store

MINUTE = 1_792_000_020  # a minute boundary
T0 = MINUTE + 20
FIVE = 1_792_000_200  # a five-minute boundary

LINEAR = Calibration(((1538, 10.0), (15380, 100.0), (30760, 200.0)))


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.db")


def minutes(store):
    return store._db().execute("SELECT ts, count, seconds FROM minutes ORDER BY ts").fetchall()


def test_rollup_follows_inserts_merges_and_deletes(store):
    for i in range(50):
        store.add_live(T0 + i, 1)
    assert minutes(store) == [(MINUTE, 40, 40), (MINUTE + 60, 10, 10)]

    store.add_live(T0, 4)  # same second again: merged, exposure unchanged
    assert minutes(store)[0] == (MINUTE, 44, 40)

    with store._db() as db:
        db.execute("DELETE FROM counts WHERE ts >= ?", (MINUTE + 60,))
    assert minutes(store)[1] == (MINUTE + 60, 0, 0)


def test_live_never_merges_into_device_rows(store):
    store.import_intervals([(T0, 1, 5)])
    store.add_live(T0, 2)
    assert store._db().execute("SELECT count, source FROM counts").fetchall() == [(5, DEVICE)]


def test_import_fills_only_gaps(store):
    for i in range(10):
        store.add_live(T0 + 100 + i, 1)
    rows = [(T0 + i, 1, 1) for i in range(200)]
    imported, skipped = store.import_intervals(rows)
    # the live stretch plus a 2 s margin on each side is left alone
    assert skipped == 10 + 2 * 2
    assert imported == 200 - skipped
    sources = dict(store._db().execute("SELECT ts, source FROM counts").fetchall())
    assert sources[T0 + 97] == DEVICE and sources[T0 + 112] == DEVICE
    assert sources[T0 + 100] == LIVE
    assert T0 + 98 not in sources and T0 + 111 not in sources
    # a second sync of the same history changes nothing
    assert store.import_intervals(rows) == (0, 200)


def test_import_respects_longer_intervals(store):
    store.import_intervals([(T0, 60, 20)])
    imported, skipped = store.import_intervals([(T0 + 30, 1, 1), (T0 + 90, 1, 1)])
    assert (imported, skipped) == (1, 1)


def test_rolling_series_is_the_trailing_minute(store):
    for i in range(120):
        store.add_live(T0 + i, 1 if i % 4 == 0 else 0)  # 15 CPM
    series = store.rate_series(T0 + 60, T0 + 120, 10)
    assert [p[1] for p in series] == [15.0] * len(series)
    assert all(p[0] % 10 == 0 for p in series)
    # windows with under 30 s of data get no point rather than a noisy one
    assert store.rate_series(T0 - 60, T0 + 60, 5)[0][0] == T0 + 30


def test_binned_series_averages_and_bands(store):
    rows = [(FIVE + m * 60 + s, 1, 1 if s < (10 if m == 0 else 30) else 0)
            for m in range(5) for s in range(60)]
    store.import_intervals(rows)
    (t, cpm, low, high), = store.rate_series(FIVE, FIVE + 300, 300)
    assert t == FIVE + 150
    assert cpm == pytest.approx((10 + 4 * 30) / 5)
    assert (low, high) == (10, 30)


def test_summary(store):
    store.import_intervals([(MINUTE + s, 1, 1) for s in range(120)])  # 60 CPM for 2 min
    summary = store.summary(MINUTE, MINUTE + 120, LINEAR)
    assert summary["counts"] == 120
    assert summary["mean_cpm"] == pytest.approx(60)
    assert summary["coverage"] == pytest.approx(1)
    assert summary["dose_usv"] == pytest.approx(LINEAR.usv_h(60) * 2 / 60)

import time

import pytest

from geiger import demo
from geiger.store import Store


def test_seeded_history_looks_like_a_month_of_background(tmp_path):
    store = Store(tmp_path / "demo.db")
    demo.seed(store)
    first, last = store.bounds()
    now = time.time()
    assert now - first == pytest.approx(demo.DAYS * 86400, abs=120)
    assert now - last < 5
    week = store.summary(int(now) - 7 * 86400, int(now), store.calibration())
    assert 14 < week["mean_cpm"] < 19  # base rate plus a little from rain
    assert week["peak_cpm"] > 25  # the rain bumps show
    assert store.get_meta("device")["serial"] == demo.INFO.serial
    # the last two hours are per second, so short ranges and the tape have data
    assert all(seconds == 1 for _, seconds, _ in store.recent(int(now) - 3600))


def test_seeding_is_repeatable(tmp_path):
    a, b = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    now = int(time.time())
    demo.seed(a, days=2, now=now)
    demo.seed(b, days=2, now=now)
    assert a.minutes_between(0, 2**40) == b.minutes_between(0, 2**40)


def test_counter_paces_like_a_heartbeat():
    counter = demo.DemoCounter()
    counter.start_heartbeat()
    start = time.time()
    readings = [counter.read_cps() for _ in range(2)]
    assert all(isinstance(r, int) and r >= 0 for r in readings)
    assert 1.5 < time.time() - start < 2.5

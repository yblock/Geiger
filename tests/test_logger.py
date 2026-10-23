import time

import serial

from geiger.device import DeviceInfo
from geiger.logger import Hub, Logger, local_utc_offset
from geiger.store import Store

NOW = 1_792_000_000.4


def make_logger(tmp_path):
    return Logger(Store(tmp_path / "test.db"), Hub())


def slots(logger):
    return [ts for ts, *_ in logger.store.recent(0)]


def test_jittery_heartbeats_take_consecutive_seconds(tmp_path):
    logger = make_logger(tmp_path)
    # arrivals near a second boundary: wall-clock flooring alone would collide and skip
    for t in (0.0, 0.62, 1.58, 2.61, 3.59):
        logger._record(NOW + t, 1)
    first = int(NOW) - 1
    assert slots(logger) == [first + i for i in range(5)]


def test_burst_after_a_stall_lands_on_its_own_seconds(tmp_path):
    logger = make_logger(tmp_path)
    logger._record(NOW, 1)
    logger._record(NOW + 1, 2)
    # readings 3-5 were held up and arrive together; the queue says how far behind they are
    logger._record(NOW + 4.0, 3, pending=2)
    logger._record(NOW + 4.05, 4, pending=1)
    logger._record(NOW + 4.1, 5, pending=0)
    first = int(NOW) - 1
    assert logger.store.recent(0) == [(first + i, 1, i + 1) for i in range(5)]


def test_running_ahead_of_the_clock_folds_into_the_latest_second(tmp_path):
    logger = make_logger(tmp_path)
    for i in range(4):
        logger._record(NOW, 1)  # four readings within one wall-clock second
    first = int(NOW) - 1
    assert logger.store.recent(0) == [(first, 1, 1), (first + 1, 1, 1), (first + 2, 1, 2)]


def test_long_gap_reanchors_and_requests_backfill(tmp_path):
    logger = make_logger(tmp_path)
    logger._record(NOW, 1)
    logger._record(NOW + 1, 1)
    assert logger._backfill_due is None
    logger._record(NOW + 300, 1)  # e.g. the Mac slept
    assert slots(logger)[-1] == int(NOW + 300) - 1
    assert logger._backfill_due is not None


class UnpluggedMidStream:
    """A counter whose cable is pulled after two readings."""

    info = DeviceInfo("GMC-320S", "GMC-320SRe 1.17", "abc", "/dev/cu.test", 115200)
    flash_size = 2**20

    def __init__(self, port=None):
        self.readings = 0
        self.closed = False

    def config(self):
        return {f"CalibrationCPM_{i}": c for i, c in enumerate((1538, 15380, 30760))} | {
            f"Calibration_uSv_{i}": u for i, u in enumerate((10.0, 100.0, 200.0))
        }

    def voltage(self):
        return 4.1

    def clock_offset(self):
        # a counter showing this Mac's local time exactly, so no clock fix is wanted
        return time.time() + local_utc_offset(), -local_utc_offset()

    def read_history(self, progress=None):
        return b""

    def start_heartbeat(self):
        pass

    def stop_heartbeat(self):
        pass

    def pending_readings(self):
        return 0

    def read_cps(self):
        self.readings += 1
        if self.readings > 2:
            raise serial.SerialException("device disconnected")
        time.sleep(0.01)
        return 1

    def close(self):
        self.closed = True


def test_pulling_the_cable_is_an_ordinary_disconnect(tmp_path, monkeypatch, caplog):
    made = []

    def connect(port):
        made.append(UnpluggedMidStream(port))
        return made[-1]

    monkeypatch.setattr("geiger.logger.Counter", connect)
    logger = make_logger(tmp_path)
    logger.start()
    deadline = time.time() + 5
    while logger.status()["state"] != "offline" and time.time() < deadline:
        time.sleep(0.02)
    logger.stop()
    logger.join(timeout=5)
    assert logger.status()["error"] == "counter disconnected"
    assert made[0].closed
    assert len(logger.store.recent(0)) == 2  # readings before the unplug were kept
    assert not [r for r in caplog.records if r.levelname == "ERROR"]  # no traceback logged


def test_backfill_waits_out_the_cooldown(tmp_path, monkeypatch):
    logger = make_logger(tmp_path)
    logger._last_backfill = 1000.0
    monkeypatch.setattr("time.time", lambda: 1100.0)
    logger._want_backfill()
    assert logger._backfill_due == 1000.0 + 600
    logger._check_timers()
    assert not logger._sync_requested.is_set()
    monkeypatch.setattr("time.time", lambda: 1601.0)
    logger._check_timers()
    assert logger._sync_requested.is_set()


def test_rolling_cpm_estimates_until_the_window_fills(tmp_path):
    logger = make_logger(tmp_path)
    for i in range(90):
        logger._record(NOW + i, 1 if i % 2 else 0)
        reading = logger.hub.wait(0, 0)[-1][2]
        assert reading["window"] == min(i + 1, 60)
    assert reading["cpm"] == 30

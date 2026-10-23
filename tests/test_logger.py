import time

import pytest
import serial

from geiger import demo
from geiger.device import DeviceInfo, Flash
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

    def settings_raw(self):
        return demo.SETTINGS

    def voltage(self):
        return 4.1

    def clock_offset(self):
        # a counter showing this Mac's local time exactly, so no clock fix is wanted
        return time.time() + local_utc_offset(), -local_utc_offset()

    def read_history(self, progress=None):
        return Flash(b"", [], False, 0, self.flash_size)

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


def test_pulling_the_cable_is_an_ordinary_disconnect(tmp_path, caplog):
    made = []

    def connect(port):
        made.append(UnpluggedMidStream(port))
        return made[-1]

    logger = make_logger(tmp_path)
    logger.connect = connect
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


class CutShort(demo.DemoCounter):
    """Erases its settings, writes the first `survive` bytes, then the cable comes out."""

    def __init__(self, survive):
        super().__init__()
        self.survive = survive
        self.cut = False

    def write_settings(self, new):
        if not self.cut:
            self.cut = True
            self._settings = bytes(new[: self.survive]) + b"\xff" * (256 - self.survive)
            raise OSError("device disconnected")
        super().write_settings(new)


@pytest.mark.parametrize("survive", [0, 40, 66])  # 66: past every byte incomplete() checks
def test_an_interrupted_settings_write_is_undone_on_the_next_connect(tmp_path, survive):
    from geiger.logger import change_settings, describe

    store = Store(tmp_path / "test.db")
    counter = CutShort(survive)
    with pytest.raises(OSError):
        change_settings(counter, store, {"save_mode": 1, "led": 0})
    assert store.get_meta("settings_write")["done"] is False
    describe(counter, store)  # what the logger does when the counter reconnects
    assert counter.settings_raw() == demo.SETTINGS
    assert store.get_meta("settings_write")["done"] is True
    assert store.get_meta("config")["SaveDataType"] == 2


def test_a_write_that_landed_before_the_cut_is_kept(tmp_path):
    from geiger.logger import change_settings, describe

    store = Store(tmp_path / "test.db")
    counter = CutShort(256)  # every byte made it; only the confirmation was lost
    with pytest.raises(OSError):
        change_settings(counter, store, {"save_mode": 1})
    writes = []
    counter.write_settings = lambda new: (writes.append(new), demo.DemoCounter.write_settings(counter, new))
    describe(counter, store)
    assert counter.settings_raw()[32] == 1
    assert writes and writes[0][32] == 1  # written once more, so the counter applies it


def test_a_write_that_never_reached_the_counter_is_just_closed(tmp_path):
    from geiger.logger import change_settings, describe

    store = Store(tmp_path / "test.db")
    counter = CutShort(0)
    counter.write_settings = lambda new: (_ for _ in ()).throw(OSError("unplugged before the erase"))
    with pytest.raises(OSError):
        change_settings(counter, store, {"led": 0})
    writes = []
    counter.write_settings = writes.append
    describe(counter, store)
    assert writes == []  # nothing rewritten: the counter still had its old settings
    assert store.get_meta("settings_write")["done"] is True


def test_a_valid_calibration_with_a_0xff_byte_isnt_mistaken_for_erased(tmp_path):
    from geiger.logger import change_settings, describe

    store = Store(tmp_path / "test.db")
    counter = demo.DemoCounter()
    change_settings(counter, store, {"cal_cpm_0": 1023})  # stored as 03 FF
    describe(counter, store)
    assert store.get_meta("config")["CalibrationCPM_0"] == 1023
    assert store.get_meta("settings_problem") is None


def test_an_erased_block_is_not_cached_or_changed(tmp_path):
    from geiger.device import DeviceError
    from geiger.logger import change_settings, describe

    store = Store(tmp_path / "test.db")
    counter = demo.DemoCounter()
    describe(counter, store)
    counter._settings = b"\xff" * 256  # erased by something else, with no write on record
    describe(counter, store)
    assert store.get_meta("config")["SaveDataType"] == 2  # the last good copy stays
    assert "erased" in store.get_meta("settings_problem")
    with pytest.raises(DeviceError, match="erased"):
        change_settings(counter, store, {"led": 1})


def test_settings_jobs_run_between_readings(tmp_path):
    logger = make_logger(tmp_path)
    logger.connect = demo.DemoCounter
    logger.backfill = logger.set_clock = False
    logger.start()
    try:
        deadline = time.time() + 10
        while logger.status()["state"] != "live" and time.time() < deadline:
            time.sleep(0.05)
        raw = logger.submit("write", {"speaker": 1, "alarm_usv": 0.8})
        assert raw[2] == 1
        view = logger.settings_view()
        assert {f["key"]: f["value"] for f in view["fields"]}["alarm_usv"] == pytest.approx(0.8)
        with pytest.raises(ValueError):
            logger.submit("write", {"save_mode": 42})
        for junk in (None, [1], {"a": 1}, float("inf")):  # what malformed JSON can produce
            with pytest.raises(ValueError):
                logger.submit("write", {"alarm_cpm": junk})
        assert logger.status()["state"] == "live"  # bad input never costs the connection
    finally:
        logger.stop()
        logger.join(timeout=5)


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

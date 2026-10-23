"""Counter behavior against a scripted serial line (no hardware)."""

import pytest

from geiger import demo, device
from geiger.device import Counter, DeviceError

ACK = b"\xaa"


class Line:
    """Answers the commands a GMC-320S answers, from an in-memory counter."""

    def __init__(self, settings: bytes, flash: bytes = b"", drop_ack_at: int | None = None,
                 keeps_settings: bool = True):
        self.settings = bytearray(settings)
        self.flash = flash.ljust(2**20, b"\xff")
        self.sent: list[bytes] = []
        self.drop_ack_at = drop_ack_at
        self.keeps_settings = keeps_settings
        self._con = type("Port", (), {"in_waiting": 0})()

    def reset_buffers(self):
        pass

    def write(self, cmd, log=True):
        self.sent.append(cmd)

    def get_exact(self, cmd, size=None, expected=b""):
        self.sent.append(cmd)
        if cmd == b"<GETCFG>>":
            return bytes(self.settings)
        if cmd == b"<ECFG>>":
            self.settings = bytearray(b"\xff" * 256)
            return ACK
        if cmd.startswith(b"<WCFG"):
            address, value = cmd[5], cmd[6]
            if address == self.drop_ack_at:
                self.drop_ack_at = None  # only once
                return b""
            if self.keeps_settings:
                self.settings[address] = value
            return ACK
        if cmd == b"<CFGUPDATE>>":
            self.settings[121:127] = bytes([26, 10, 23, 11, 0, 0])  # the counter stamps itself
            return ACK
        if cmd.startswith(b"<SPIR"):
            position = int.from_bytes(cmd[5:8], "big")
            length = int.from_bytes(cmd[8:10], "big")
            return self.flash[position : position + length]
        raise AssertionError(f"unexpected command {cmd!r}")


def counter_on(line: Line) -> Counter:
    c = Counter.__new__(Counter)
    c._conn = line
    c._dev = type("Dev", (), {"_flash_memory_size_bytes": 2**20})()
    c._last_command = 0.0
    c._heartbeat_on = False
    c._page_extra = 0
    return c


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(device, "COMMAND_GAP", 0)
    monkeypatch.setattr(device, "SETTINGS_SETTLE", 0)
    monkeypatch.setattr(device, "STRAY_BYTE_WAIT", 0)


def test_write_settings_erases_writes_set_bytes_and_applies():
    line = Line(b"\xff" * 256)
    counter_on(line).write_settings(demo.SETTINGS)
    writes = [c for c in line.sent if c.startswith(b"<WCFG")]
    assert line.sent[0] == b"<ECFG>>"
    assert len(writes) == sum(1 for b in demo.SETTINGS if b != 0xFF)  # erased bytes already read 0xFF
    assert b"<CFGUPDATE>>" in line.sent
    assert bytes(line.settings[:121]) == demo.SETTINGS[:121]


def test_a_dropped_reply_is_retried_from_the_erase():
    line = Line(demo.SETTINGS, drop_ack_at=32)
    counter_on(line).write_settings(demo.SETTINGS)
    assert line.sent.count(b"<ECFG>>") == 2


def test_a_counter_that_doesnt_keep_the_settings_is_an_error():
    line = Line(demo.SETTINGS, keeps_settings=False)
    with pytest.raises(DeviceError, match="didn't keep"):
        counter_on(line).write_settings(demo.SETTINGS)


def test_history_before_the_ring_wraps_stops_at_the_first_blank_page():
    line = Line(demo.SETTINGS, flash=b"\x55" * 3000)
    flash = counter_on(line).read_history()
    assert not flash.wrapped
    assert flash.used == 3000
    assert len(flash.image) == 4096  # two pages; the third came back blank
    assert sum(1 for c in line.sent if c.startswith(b"<SPIR")) == 4  # last page, 2 data, 1 blank


def test_history_after_the_ring_wraps_reads_every_page_in_stretches():
    newest = b"\x01" * 4096
    gap = b"\xff" * 4096  # erased just ahead of where it writes
    oldest = b"\x02" * (2**20 - 8192)
    line = Line(demo.SETTINGS, flash=newest + gap + oldest)
    flash = counter_on(line).read_history()
    assert flash.wrapped
    assert flash.used == flash.capacity == 2**20
    # recording ran from the end of memory into its start: one stretch, oldest first
    assert flash.segments == [oldest + newest]


class StubbornHeartbeat(Line):
    """Ignores the first `ignore` HEARTBEAT0 commands and keeps streaming."""

    def __init__(self, ignore):
        super().__init__(demo.SETTINGS)
        self.ignore = ignore
        self.streaming = True
        line = self

        class Port:
            @property
            def in_waiting(self):
                return 2 if line.streaming else 0

        self._con = Port()

    def write(self, cmd, log=True):
        super().write(cmd)
        if cmd == device.HEARTBEAT_OFF:
            if self.ignore:
                self.ignore -= 1
            else:
                self.streaming = False


def test_stopping_the_heartbeat_resends_until_the_line_goes_quiet(monkeypatch):
    monkeypatch.setattr(device, "HEARTBEAT_QUIET", 0.05)
    line = StubbornHeartbeat(ignore=2)
    c = counter_on(line)
    c._heartbeat_on = True
    c.stop_heartbeat()
    assert line.sent.count(device.HEARTBEAT_OFF) == 3
    assert not c._heartbeat_on


def test_a_heartbeat_that_never_stops_is_an_error(monkeypatch):
    monkeypatch.setattr(device, "HEARTBEAT_QUIET", 0.01)
    c = counter_on(StubbornHeartbeat(ignore=99))
    c._heartbeat_on = True
    with pytest.raises(DeviceError, match="didn't stop"):
        c.stop_heartbeat()

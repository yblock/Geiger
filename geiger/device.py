"""Serial access to a GMC-300/320-series counter through pygmc.

Quirks found on a GMC-320S (firmware "Re 1.17") that shape this module:

* A command sent within a few milliseconds of GETCPM or HEARTBEAT0 is silently
  dropped, so every request waits COMMAND_GAP after the previous one.
* Settings are rewritten as GQ's protocol describes: erase the block (ECFG), write
  each byte (WCFG with a one-byte address), then apply (CFGUPDATE). About a second
  later the counter rewrites a few bytes of its own (see ``settings.DEVICE_MANAGED``).
  Rewriting settings doesn't touch the history memory or interrupt logging.
* It sometimes ignores HEARTBEAT0 and keeps streaming, so stopping the heartbeat
  waits for a second of silence and resends until it gets one. (Seen as a 0x80
  heartbeat byte arriving in place of a settings-write reply.)
* Heartbeat words carry status flags in their top two bits (seen as 0x80 and 0xC0,
  changing mid-stream); pygmc keeps the low 14 bits, and so does this module.
* A history page read (SPIR) returns one byte more than asked for. pygmc clears the
  buffer straight after each page, and when that byte lands a moment later it
  shifts the next page by one, which can split a timestamp marker into bogus
  counts. Pages here wait for the stray byte before moving on.
* While the heartbeat is on, the counter writes every second, which garbles any
  other reply and makes pygmc's discovery skip the port. Connecting therefore
  sends HEARTBEAT0 first, in case a previous run died with it on.

The open port is flock()ed, the same lock pyserial's ``exclusive=True`` takes, so a
second geiger process fails fast instead of interleaving commands with the logger.
"""

from __future__ import annotations

import datetime
import fcntl
import logging
import re
import struct
import time
from dataclasses import asdict, dataclass

import pygmc
import serial
from pygmc.connection.utils import get_gmc_usb_devices
from pygmc.devices.device_rfc1201 import DeviceRFC1201

from . import settings
from .history import PAGE, device_epoch, split_segments

log = logging.getLogger(__name__)

COMMAND_GAP = 0.3  # seconds between commands
STRAY_BYTE_WAIT = 0.05  # seconds to wait for the extra byte after a history page
SETTINGS_SETTLE = 1.5  # seconds the counter takes to stamp its own bytes after a settings update
HEARTBEAT_QUIET = 1.2  # seconds of silence (over one heartbeat period) that prove it stopped
HEARTBEAT_STOP_TRIES = 4
HEARTBEAT_OFF = b"<HEARTBEAT0>>"


class DeviceError(Exception):
    """The counter did not answer as expected."""


@dataclass(frozen=True)
class Flash:
    """What a history read found in the counter's memory."""

    image: bytes  # the pages as read, in memory order
    segments: list[bytes]  # runs of data between blank pages, each parsed on its own
    wrapped: bool  # the ring buffer is full and overwriting its oldest records
    used: int  # bytes holding data (the whole memory once wrapped)
    capacity: int


@dataclass(frozen=True)
class DeviceInfo:
    model: str
    version: str
    serial: str
    port: str
    baudrate: int

    def to_dict(self) -> dict:
        return asdict(self)


class Counter:
    """One open connection to a GMC counter."""

    def __init__(self, port: str | None = None, timeout: float = 3):
        dev = _connect(port, timeout)
        self._dev = dev
        self._conn = dev.connection
        self._last_command = time.monotonic()
        self._heartbeat_on = False
        self._page_extra = 0

        try:
            details = self._conn.get_connection_details()
            version = self._call(dev.get_version).strip()
            # "GMC-320SRe 1.17" -> "GMC-320S", the split pygmc's discovery uses
            model = re.match(r"(.*?) ?Re ?", version)
            self.info = DeviceInfo(
                model=model.group(1) if model else version,
                version=version,
                serial=self._call(dev.get_serial),
                port=details["port"],
                baudrate=details["baudrate"],
            )
        except BaseException:
            self.close()  # release the port lock, or every retry would find it busy
            raise

    def close(self) -> None:
        try:
            if self._heartbeat_on:
                self.stop_heartbeat()
        except Exception:  # the port may already be gone
            pass
        try:
            self._conn.close_connection()
        except Exception:
            pass

    def _pace(self) -> None:
        wait = self._last_command + COMMAND_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _ready(self) -> None:
        if self._heartbeat_on:
            raise DeviceError("stop the heartbeat before sending commands")
        self._pace()
        self._conn.reset_buffers()

    def _call(self, fn, *args):
        self._ready()
        try:
            return fn(*args)
        except (struct.error, ValueError, IndexError) as e:
            # pygmc unpacks whatever arrived before the timeout; a short reply lands here
            raise DeviceError(f"no valid reply to {fn.__name__}") from e
        finally:
            self._last_command = time.monotonic()

    # commands

    # settings

    def settings_raw(self) -> bytes:
        """The 256-byte settings block, as stored."""
        self._ready()
        raw = self._conn.get_exact(b"<GETCFG>>", size=settings.SIZE)
        self._last_command = time.monotonic()
        if len(raw) != settings.SIZE:
            raise DeviceError("settings came back short")
        return raw

    def write_settings(self, new: bytes) -> None:
        """Replace the settings block, then read it back to confirm.

        The protocol only allows erasing the whole block and rewriting it byte by
        byte (unset bytes read 0xFF after the erase, so those are skipped). One
        retry covers a dropped reply; the caller keeps a backup for anything worse.
        """
        if len(new) != settings.SIZE:
            raise ValueError("settings block must be 256 bytes")
        for attempt in (1, 2):
            self._ready()
            try:
                self._expect_ack(b"<ECFG>>", "erase settings")
                for address, value in enumerate(new):
                    if value != 0xFF:
                        self._expect_ack(b"<WCFG" + bytes([address, value]) + b">>", f"write byte {address}")
                self._expect_ack(b"<CFGUPDATE>>", "apply settings")
            except DeviceError:
                if attempt == 2:
                    raise
                continue
            finally:
                self._last_command = time.monotonic()
            time.sleep(SETTINGS_SETTLE)  # the counter rewrites its own bytes, then is quiet
            if settings.same(self.settings_raw(), new):
                return
        raise DeviceError("the counter didn't keep the new settings")

    def _expect_ack(self, cmd: bytes, what: str) -> None:
        reply = self._conn.get_exact(cmd, size=1)
        if reply != b"\xaa":
            raise DeviceError(f"no confirmation to {what} (got {reply!r})")

    def cpm(self) -> int:
        return self._call(self._dev.get_cpm)

    def voltage(self) -> float:
        return self._call(self._dev.get_voltage)

    def clock(self) -> datetime.datetime:
        """The counter's clock, naive local time."""
        return self._call(self._dev.get_datetime)

    def clock_offset(self) -> tuple[float, float]:
        """(counter time now, seconds to add to counter time to get real time).

        Counter time is the zone-less count from ``history.device_epoch``.
        """
        # the counter truncates to whole seconds, so it reads 0.5 s early on average
        counter_now = device_epoch(self.clock()) + 0.5
        return counter_now, time.time() - counter_now

    def set_clock(self, when: datetime.datetime | None = None) -> None:
        self._call(self._dev.set_datetime, when)

    # live counts

    def start_heartbeat(self) -> None:
        self._pace()
        self._conn.reset_buffers()
        self._conn.write(b"<HEARTBEAT1>>")
        self._heartbeat_on = True

    def pending_readings(self) -> int:
        """Heartbeat words already waiting: how far behind the stream is being read."""
        return self._conn._con.in_waiting // 2

    def read_cps(self) -> int | None:
        """Block until the next 1 s count arrives; None if the read timed out."""
        raw = self._conn.read_until(size=2)
        if not raw:
            return None
        if len(raw) == 1:  # every later word would be split across two readings
            raise DeviceError("heartbeat stream lost a byte")
        return struct.unpack(">H", raw)[0] & 0x3FFF

    def stop_heartbeat(self) -> None:
        """Stop the heartbeat, and make sure it really stopped.

        The counter now and then ignores HEARTBEAT0 and keeps streaming, and a
        stray heartbeat word in the middle of a settings write or history read
        would be taken for a reply. So after each attempt, wait out one heartbeat
        period: if nothing arrives, it has stopped.
        """
        port = self._conn._con
        for _ in range(HEARTBEAT_STOP_TRIES):
            self._conn.write(HEARTBEAT_OFF)
            time.sleep(COMMAND_GAP)  # let any word already in flight land, then drop it
            self._conn.reset_buffers()
            deadline = time.monotonic() + HEARTBEAT_QUIET
            while not port.in_waiting and time.monotonic() < deadline:
                time.sleep(0.02)
            if not port.in_waiting:
                self._heartbeat_on = False
                self._last_command = time.monotonic()
                return
        raise DeviceError("the counter didn't stop its heartbeat")

    # history

    @property
    def flash_size(self) -> int:
        return self._dev._flash_memory_size_bytes

    def read_history(self, progress=None) -> Flash:
        """Read the history memory page by page.

        The memory is a ring buffer: once full, the counter overwrites its oldest
        records, erasing a little ahead of where it writes. Until it first wraps,
        everything after the data is blank, so reading stops at the first blank
        page. Once it has wrapped, the last page holds data, and every page is
        read: the newest stretch, the erased gap, then the older stretch.
        Progress is reported as (bytes_read, flash_size).
        """
        if self._heartbeat_on:
            raise DeviceError("stop the heartbeat before reading history")
        page = PAGE
        blank = lambda chunk: chunk.count(0xFF) == page
        self._ready()
        image = bytearray()
        try:
            last = self._read_page(self.flash_size - page, page)
            wrapped = not blank(last)
            for position in range(0, self.flash_size, page):
                chunk = last if position == self.flash_size - page else self._read_page(position, page)
                if blank(chunk) and not wrapped:
                    break
                image += chunk
                if progress:
                    progress(len(image), self.flash_size)
        finally:
            self._last_command = time.monotonic()
        image = bytes(image)
        used = self.flash_size if wrapped else len(image.rstrip(b"\xff"))
        return Flash(image, split_segments(image, page), wrapped, used, self.flash_size)

    def _read_page(self, position: int, size: int) -> bytes:
        cmd = b"<SPIR" + struct.pack(">I", position)[1:] + struct.pack(">H", size) + b">>"
        # once a counter has shown the extra byte, read it as part of every page
        chunk = self._conn.get_exact(cmd, size=size + self._page_extra)
        if len(chunk) < size:
            raise DeviceError(f"history page at {position} came back short")
        if not self._page_extra:
            port = self._conn._con
            deadline = time.monotonic() + STRAY_BYTE_WAIT
            while not port.in_waiting and time.monotonic() < deadline:
                time.sleep(0.001)
            self._page_extra = 1 if port.in_waiting else 0
        self._conn.reset_buffers()
        return chunk[:size]


def _connect(port: str | None, timeout: float) -> DeviceRFC1201:
    """Find and open a counter, on `port` or on any port with the GQ USB chip."""
    candidates = [port] if port else sorted({d.device for d in get_gmc_usb_devices()})
    if not candidates:
        raise DeviceError("no GQ counter found; is it plugged in?")
    busy = []
    for candidate in candidates:
        if not _quiet_heartbeat(candidate):
            busy.append(candidate)
            continue
        try:
            dev = pygmc.connect(port=candidate, timeout=timeout)
        except ConnectionError:
            continue
        try:
            fcntl.flock(dev.connection._con.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            dev.connection.close_connection()
            busy.append(candidate)
            continue
        if not isinstance(dev, DeviceRFC1201):
            dev.connection.close_connection()
            raise DeviceError(f"{type(dev).__name__} is not a GMC-300/320-series counter")
        return dev
    if busy:
        raise DeviceError(f"{', '.join(busy)} is in use by another program (another geiger?)")
    raise DeviceError(f"no GMC counter answered on {', '.join(candidates)}")


def _quiet_heartbeat(port: str, baudrate: int = 115200) -> bool:
    """Turn off a heartbeat left running so discovery can see the counter.

    Returns False when another process holds the port.
    """
    try:
        with serial.Serial(port, baudrate, timeout=1, exclusive=True) as s:
            s.write(HEARTBEAT_OFF)
            s.flush()
            time.sleep(COMMAND_GAP)
            s.reset_input_buffer()
    except serial.SerialException as e:
        if "exclusively lock" in str(e):
            return False
        log.debug("could not quiet %s: %s", port, e)
    return True

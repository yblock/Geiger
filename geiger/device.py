"""Serial access to a GMC-300/320-series counter through pygmc.

Quirks found on a GMC-320S (firmware "Re 1.17") that shape this module:

* A command sent within a few milliseconds of GETCPM or HEARTBEAT0 is silently
  dropped, so every request waits COMMAND_GAP after the previous one.
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

from .history import device_epoch

log = logging.getLogger(__name__)

COMMAND_GAP = 0.3  # seconds between commands
STRAY_BYTE_WAIT = 0.05  # seconds to wait for the extra byte after a history page
HEARTBEAT_OFF = b"<HEARTBEAT0>>"


class DeviceError(Exception):
    """The counter did not answer as expected."""


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

    def _call(self, fn, *args):
        if self._heartbeat_on:
            raise DeviceError("stop the heartbeat before sending commands")
        self._pace()
        self._conn.reset_buffers()
        try:
            return fn(*args)
        except (struct.error, ValueError, IndexError) as e:
            # pygmc unpacks whatever arrived before the timeout; a short reply lands here
            raise DeviceError(f"no valid reply to {fn.__name__}") from e
        finally:
            self._last_command = time.monotonic()

    # commands

    def config(self) -> dict:
        return dict(self._call(self._dev.get_config))

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
        self._conn.write(HEARTBEAT_OFF)
        self._heartbeat_on = False
        time.sleep(COMMAND_GAP)  # let any word already in flight land, then drop it
        self._conn.reset_buffers()
        self._last_command = time.monotonic()

    # history

    @property
    def flash_size(self) -> int:
        return self._dev._flash_memory_size_bytes

    def read_history(self, progress=None) -> bytes:
        """Read the flash history page by page, stopping at the first blank page.

        The loop of pygmc's ``get_raw_history`` with the stray-byte fix above, a
        progress callback (bytes_read, flash_size), and a check for short pages.
        """
        if self._heartbeat_on:
            raise DeviceError("stop the heartbeat before reading history")
        page = self._dev._flash_memory_page_size_bytes
        data = bytearray()
        self._pace()
        self._conn.reset_buffers()
        try:
            for position in range(0, self.flash_size, page):
                chunk = self._read_page(position, page)
                if chunk.count(0xFF) == page:
                    break
                data += chunk
                if progress:
                    progress(len(data), self.flash_size)
        finally:
            self._last_command = time.monotonic()
        return bytes(data)

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

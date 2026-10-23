"""A simulated counter and made-up history, for trying the dashboard without hardware.

The background model is a plausible indoor reading: about 16 CPM with a small daily
radon cycle (highest around dawn), bumps where rain washed radon decay products out of
the air, and a day the counter was switched off. Counts are Poisson-distributed, like
real ones. History is seeded from a fixed random seed, so screenshots are repeatable.
"""

from __future__ import annotations

import datetime
import math
import random
import time

from . import settings
from .device import DeviceInfo, Flash
from .logger import local_utc_offset, remember_settings
from .store import Store

BASE_CPM = 15.8
DAYS = 30

INFO = DeviceInfo(
    model="GMC-320S",
    version="GMC-320SRe 1.17",
    serial="a5f0c1e2d3b4c7",
    port="/dev/cu.usbserial-110",
    baudrate=115200,
)

def _settings_block() -> bytes:
    blank = bytearray(b"\xff" * settings.SIZE)
    blank[0] = 0  # power on
    blank[57] = 254  # 115200 baud
    return settings.encode(bytes(blank), {
        "speaker": 0, "led": 1, "alarm": 1, "alarm_type": 1, "alarm_cpm": 100, "alarm_usv": 0.5,
        "save_mode": 2, "threshold_type": 0, "threshold_cpm": 100, "threshold_usv": 0.5,
        "power_saving": 0, "motion_detect": 1, "swivel": 2, "reverse_display": 0, "battery": 0,
        "contrast": 21, "backlight_level": 4, "backlight_timer": 1, "large_font_unit": 2,
        "cal_cpm_0": 1538, "cal_usv_0": 10.0, "cal_cpm_1": 15380, "cal_usv_1": 100.0,
        "cal_cpm_2": 30760, "cal_usv_2": 200.0,
    })


SETTINGS = _settings_block()

# rain events: (days ago, local hour it starts, extra fraction at peak, hours it lasts)
RAIN = [(27, 3, 0.55, 2), (21, 16, 0.35, 1), (11, 22, 0.8, 3), (6, 5, 0.4, 1.5), (2, 14, 0.65, 2)]
# hours ago, for one inside the last day
RECENT_RAIN = (9, 0.45, 1)
# (days ago, local hour) the counter was off, and for how many hours
SWITCHED_OFF = (16, 10, 22)


def _local(days_ago: int, hour: float) -> float:
    midnight = datetime.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - datetime.timedelta(days=days_ago) + datetime.timedelta(hours=hour)).timestamp()


def _bumps(now: float) -> list[tuple[float, float, float]]:
    """(start, peak fraction, hours) for each rain event, in epoch seconds."""
    events = [(_local(d, h), a, p) for d, h, a, p in RAIN]
    hours_ago, a, p = RECENT_RAIN
    events.append((now - hours_ago * 3600, a, p))
    return events


def rate_cpm(ts: float, bumps) -> float:
    hour = (ts + local_utc_offset()) / 3600 % 24
    cpm = BASE_CPM * (1 + 0.06 * math.cos(2 * math.pi * (hour - 6) / 24))
    for start, peak, hours in bumps:
        dt = (ts - start) / 60  # minutes since the rain began
        if dt < 0:
            continue
        rise, plateau = 45, hours * 60
        if dt < rise:
            extra = peak * dt / rise
        elif dt < rise + plateau:
            extra = peak
        else:
            extra = peak * math.exp(-(dt - rise - plateau) / 80)
        cpm += BASE_CPM * extra
    return cpm


def poisson(rng: random.Random, lam: float) -> int:
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit = math.exp(-lam)
    k, p = 0, rng.random()
    while p > limit:
        k += 1
        p *= rng.random()
    return k


def seed(store: Store, days: int = DAYS, seed: int = 320, now: int | None = None) -> None:
    """Fill an empty store with `days` of history ending `now` (default: the current time)."""
    rng = random.Random(seed)
    now = int(time.time()) if now is None else now
    bumps = _bumps(now)
    off_start = _local(SWITCHED_OFF[0], SWITCHED_OFF[1])
    off_end = off_start + SWITCHED_OFF[2] * 3600
    fine_from = now - 7200  # the last two hours per second, like a live session

    start = (now - days * 86400) // 60 * 60
    rows = []
    for ts in range(start, fine_from, 60):
        if off_start <= ts < off_end:
            continue
        rows.append((ts, 60, poisson(rng, rate_cpm(ts, bumps))))
    for ts in range(fine_from, now):
        rows.append((ts, 1, poisson(rng, rate_cpm(ts, bumps) / 60)))
    store.import_intervals(rows)

    store.set_meta("device", INFO.to_dict())
    remember_settings(store, SETTINGS)
    last_visit = now - 5 * 86400
    used = round(len(rows) * 1.07)  # about a byte a record, plus timestamps
    store.set_meta(
        "last_sync",
        {
            "at": now - 20,
            "duration": 3.1,
            "bytes": 0,  # no memory image to download in the demo
            "used": used,
            "capacity": 2**20,
            "wrapped": False,
            "records": len(rows),
            "imported": sum(1 for r in rows if r[0] >= last_visit),
            "skipped": sum(1 for r in rows if r[0] < last_visit),
            "first": rows[0][0],
            "last": now,
            "error": None,
        },
    )


class DemoCounter:
    """Stands in for ``device.Counter``: same methods, readings from the model above."""

    info = INFO
    flash_size = 2**20

    def __init__(self, port: str | None = None):
        self._rng = random.Random()
        self._bumps = _bumps(time.time())
        self._next = time.time() + 1
        self._settings = SETTINGS

    def settings_raw(self) -> bytes:
        return self._settings

    def write_settings(self, new: bytes) -> None:
        time.sleep(1)  # about as long as the real thing
        self._settings = bytes(new)

    def voltage(self) -> float:
        return 4.1

    def cpm(self) -> int:
        return poisson(self._rng, rate_cpm(time.time(), self._bumps))

    def clock_offset(self) -> tuple[float, float]:
        # a counter showing this Mac's local time exactly
        return time.time() + local_utc_offset(), -local_utc_offset()

    def set_clock(self, when=None) -> None:
        pass

    def start_heartbeat(self) -> None:
        self._next = time.time() + 1

    def read_cps(self) -> int:
        time.sleep(max(0.0, self._next - time.time()))
        self._next += 1
        return poisson(self._rng, rate_cpm(time.time(), self._bumps) / 60)

    def pending_readings(self) -> int:
        return 0

    def stop_heartbeat(self) -> None:
        pass

    def read_history(self, progress=None) -> Flash:
        return Flash(b"", [], False, 0, self.flash_size)

    def close(self) -> None:
        pass

"""The logging worker: owns the serial port, records every second, backfills gaps."""

from __future__ import annotations

import datetime
import logging
import threading
import time
from collections import deque

from .calibration import Calibration
from .device import Counter, DeviceError
from .history import history_intervals
from .store import Store

log = logging.getLogger(__name__)

# Backfill from the counter's flash when this much time is missing (seconds).
BACKFILL_AFTER_GAP = 90
# Never start automatic backfills closer together than this (seconds).
BACKFILL_COOLDOWN = 600
# Consecutive heartbeat timeouts before the counter is treated as gone.
MAX_MISSED_READS = 3
# Counter clock error worth correcting (seconds). Readings of it carry ±0.5 s of noise.
CLOCK_TOLERANCE = 3.0

CONFIG_KEYS = (
    "Alarm",
    "AlarmValue_uSv",
    "Speaker",
    "SaveDataType",
    "CalibrationCPM_0",
    "Calibration_uSv_0",
    "CalibrationCPM_1",
    "Calibration_uSv_1",
    "CalibrationCPM_2",
    "Calibration_uSv_2",
)


def describe(counter: Counter, store: Store) -> tuple[Calibration | None, float | None, float]:
    """Read and remember the counter's identity, calibration, and clock.

    Returns (calibration, battery volts or None, seconds the counter's clock is
    behind this computer's local time; negative when it runs ahead).
    """
    config = counter.config()
    calibration = Calibration.from_config(config)
    if not calibration.segments:
        log.warning("counter reports no usable calibration; showing CPM only")
        calibration = None
    store.set_meta("device", counter.info.to_dict())
    store.set_meta("config", {k: config.get(k) for k in CONFIG_KEYS})
    store.set_meta("calibration", calibration.to_dict() if calibration else None)
    try:
        voltage = counter.voltage()
    except DeviceError:
        voltage = None
    drift = clock_drift(counter, store)
    log.info(
        "connected to %s (%s, serial %s) on %s; counter clock %+.1f s vs local time",
        counter.info.model,
        counter.info.version,
        counter.info.serial,
        counter.info.port,
        -drift,
    )
    return calibration, voltage, drift


def local_utc_offset() -> float:
    return datetime.datetime.now().astimezone().utcoffset().total_seconds()


def clock_drift(counter: Counter, store: Store) -> float:
    """Seconds the counter's clock is behind local time (negative: ahead); remembered."""
    counter_now, offset = counter.clock_offset()
    store.note_clock(counter_now, offset)
    return offset + local_utc_offset()


def correct_clock(counter: Counter, store: Store, force: bool = False) -> tuple[float, float] | None:
    """Set the counter's clock to local time if it is off by more than CLOCK_TOLERANCE.

    Run a history sync first: once the clock moves back, old and new records can
    share counter times. Returns (drift before, drift after), or None if unchanged.
    """
    before = clock_drift(counter, store)
    if not force and abs(before) <= CLOCK_TOLERANCE:
        return None
    time.sleep(1 - time.time() % 1)  # the counter keeps whole seconds; set it on the tick
    counter.set_clock(datetime.datetime.now())
    after = clock_drift(counter, store)
    log.info("set the counter's clock: was %+.1f s from local time, now %+.1f s", -before, -after)
    return before, after


def sync_history(counter: Counter, store: Store, progress=None) -> dict:
    """Import the counter's flash history into gaps in the database; record the result."""
    started = time.time()
    try:
        offsets = store.note_clock(*counter.clock_offset())
        raw = counter.read_history(progress)
        finished = time.time()
        # leave the last couple of seconds to the live stream that resumes next
        rows, dropped = history_intervals(raw, offsets, until=finished - 2)
        imported, skipped = store.import_intervals(rows)
        result = {
            "at": finished,
            "duration": round(finished - started, 1),
            "bytes": len(raw),
            "records": len(rows) + dropped,
            "imported": imported,
            "skipped": skipped + dropped,
            "first": rows[0][0] if rows else None,
            "last": rows[-1][0] + rows[-1][1] if rows else None,
            "error": None,
        }
        log.info(
            "history sync: %d records read, %d imported in %.1f s",
            result["records"],
            imported,
            result["duration"],
        )
    except DeviceError as e:
        log.warning("history sync failed: %s", e)
        result = {"at": time.time(), "error": str(e)}
    store.set_meta("last_sync", result)
    return result


class Hub:
    """Fan-out of logger events to any number of waiting readers (the SSE stream)."""

    def __init__(self, keep: int = 300):
        self._cond = threading.Condition()
        self._events: deque[tuple[int, str, dict]] = deque(maxlen=keep)
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    def publish(self, kind: str, data: dict) -> None:
        with self._cond:
            self._seq += 1
            self._events.append((self._seq, kind, data))
            self._cond.notify_all()

    def wait(self, after: int, timeout: float) -> list[tuple[int, str, dict]]:
        with self._cond:
            self._cond.wait_for(lambda: self._seq > after, timeout)
            return [e for e in self._events if e[0] > after]


class Logger(threading.Thread):
    def __init__(
        self,
        store: Store,
        hub: Hub,
        port: str | None = None,
        backfill: bool = True,
        set_clock: bool = True,
    ):
        super().__init__(name="geiger-logger", daemon=True)
        self.store = store
        self.hub = hub
        self.port = port
        self.backfill = backfill
        self.set_clock = set_clock
        self.calibration: Calibration | None = store.calibration()

        self._stop_event = threading.Event()
        self._sync_requested = threading.Event()
        self._lock = threading.Lock()
        self._state = "starting"
        self._state_since = time.time()
        self._error: str | None = None
        self._voltage: float | None = None
        self._clock_offset: float | None = None
        self._sync_progress: dict | None = None
        self._reading: dict | None = None
        self._last_backfill = 0.0
        self._backfill_due: float | None = None
        self._utc_offset = local_utc_offset()
        self._utc_offset_checked = time.time()
        self._clock_fix_requested = False

        self._last_ts: int | None = None
        self._window: deque[list[int]] = deque()  # [ts, count] for the last 60 s

    # control (called from other threads)

    def request_sync(self) -> None:
        self._sync_requested.set()

    def stop(self) -> None:
        self._stop_event.set()

    def status(self) -> dict:
        with self._lock:
            calibration = self.calibration
            return {
                "state": self._state,
                "since": self._state_since,
                "error": self._error,
                "device": self.store.get_meta("device"),
                "config": self.store.get_meta("config"),
                "calibration": {
                    "points": [list(p) for p in calibration.points],
                    "usv_per_cpm": calibration.segments[0][1],
                }
                if calibration and calibration.segments
                else None,
                "voltage": self._voltage,
                "clock_offset": self._clock_offset,
                "sets_clock": self.set_clock,
                "sync": self._sync_progress,
                "sync_pending": self._sync_requested.is_set(),
                "last_sync": self.store.get_meta("last_sync"),
                "reading": self._reading,
                "bounds": self.store.bounds(),
            }

    # worker

    def run(self) -> None:
        retry = 2.0
        while not self._stop_event.is_set():
            counter = None
            try:
                self._set_state("connecting")
                counter = Counter(self.port)
                self._on_connect(counter)
                retry = 2.0
                self._stream(counter)
            except DeviceError as e:
                if str(e) != self._error:  # don't repeat the same complaint every retry
                    log.warning("counter unavailable: %s", e)
                self._set_state("offline", str(e))
            except Exception as e:  # serial errors on unplug, and anything unforeseen
                message = f"{type(e).__name__}: {e}"
                if message != self._error:
                    log.exception("logger error")
                self._set_state("offline", message)
            finally:
                if counter:
                    counter.close()
            self._stop_event.wait(retry)
            retry = min(retry * 2, 30.0)
        self._set_state("stopped")

    def _on_connect(self, counter: Counter) -> None:
        calibration, voltage, drift = describe(counter, self.store)
        with self._lock:
            self.calibration = calibration
            self._voltage = voltage
            self._clock_offset = round(drift, 1)

        bounds = self.store.bounds()
        if bounds is None or time.time() - bounds[1] > BACKFILL_AFTER_GAP:
            self._want_backfill()
        if self.set_clock and abs(drift) > CLOCK_TOLERANCE:
            self._clock_fix_requested = True

    def _fix_clock(self, counter: Counter) -> None:
        change = correct_clock(counter, self.store)
        if change:
            with self._lock:
                self._clock_offset = round(change[1], 1)

    def _want_backfill(self) -> None:
        """Ask for a history sync now, or once the cooldown since the last one ends.

        The cooldown keeps a counter that connects but keeps dropping out from
        having its whole flash re-read on every reconnect.
        """
        if not self.backfill:
            return
        self._backfill_due = max(self._last_backfill + BACKFILL_COOLDOWN, time.time())

    def _stream(self, counter: Counter) -> None:
        while not self._stop_event.is_set():
            self._check_timers()
            if self._clock_fix_requested:
                self._clock_fix_requested = False
                if self.backfill:
                    self._sync_requested.set()  # imports old-clock history, then sets the clock
                else:
                    self._fix_clock(counter)
            if self._sync_requested.is_set():
                self._sync(counter)
            self._resume_window()
            counter.start_heartbeat()
            self._set_state("live")
            self._read_heartbeat(counter)
            counter.stop_heartbeat()

    def _read_heartbeat(self, counter: Counter) -> None:
        """Record readings until stopped or a sync is wanted."""
        missed = 0
        while not self._stop_event.is_set():
            self._check_timers()
            if self._sync_requested.is_set() or self._clock_fix_requested:
                return
            cps = counter.read_cps()
            if cps is None:
                missed += 1
                if missed >= MAX_MISSED_READS:
                    raise DeviceError("heartbeat stopped (counter off or unplugged?)")
                continue
            missed = 0
            self._record(time.time(), cps, counter.pending_readings())

    def _check_timers(self) -> None:
        now = time.time()
        if self._backfill_due is not None and now >= self._backfill_due:
            self._backfill_due = None
            self._sync_requested.set()
        if now - self._utc_offset_checked > 60:
            self._utc_offset_checked = now
            offset = local_utc_offset()
            if offset != self._utc_offset:  # DST began or ended, or the Mac changed zones
                log.info("local time zone offset changed; resetting the counter's clock")
                self._utc_offset = offset
                if self.set_clock:
                    self._clock_fix_requested = True

    def _resume_window(self) -> None:
        """Seed the rolling CPM from what is already stored, and re-anchor time slots."""
        self._last_ts = None
        self._window.clear()
        for ts, seconds, count in self.store.recent(int(time.time()) - 61):
            if seconds == 1:
                self._window.append([ts, count])

    def _record(self, now: float, cps: int, pending: int = 0) -> None:
        # A heartbeat reports the second that just ended; `pending` readings queued
        # behind it are newer still. Readings land ~1 s apart with some jitter, so
        # consecutive readings take consecutive slots, and the wall clock only takes
        # over when the stream has fallen behind it (a gap) or run more than 2 s
        # ahead of it (folded into the latest second, so slots never collide).
        clock = int(now) - 1 - pending
        last = self._last_ts
        if last is None or clock > last + 2:
            ts = clock
            if last is not None and clock - last > BACKFILL_AFTER_GAP:
                log.info("missed %d s of readings (sleep?); scheduling backfill", clock - last)
                self._want_backfill()
        elif clock < last - 1:
            ts = last
        else:
            ts = last + 1
        self._last_ts = ts
        if not self.store.add_live(ts, cps):
            log.debug("dropped live reading at %d: an imported record covers it", ts)

        if self._window and self._window[-1][0] == ts:
            self._window[-1][1] += cps
        else:
            self._window.append([ts, cps])
        while self._window and self._window[0][0] <= ts - 60:
            self._window.popleft()
        seconds = len(self._window)
        cpm = 60 * sum(c for _, c in self._window) / seconds
        reading = {
            "ts": ts,
            "cps": cps,
            "cpm": round(cpm, 1),
            "window": seconds,  # under 60 while the rolling window fills
            "usv_h": round(self.calibration.usv_h(cpm), 4) if self.calibration else None,
        }
        with self._lock:
            self._reading = reading
        self.hub.publish("reading", reading)

    def _sync(self, counter: Counter) -> None:
        """Import the counter's flash history into any gaps in the database."""
        self._sync_requested.clear()
        self._backfill_due = None  # any sync covers a pending backfill
        self._last_backfill = time.time()
        self._set_state("syncing")
        last_publish = 0.0

        def progress(done: int, total: int) -> None:
            nonlocal last_publish
            with self._lock:
                self._sync_progress = {"bytes": done, "total": total}
            if time.time() - last_publish > 0.5:
                last_publish = time.time()
                self._publish_status()

        with self._lock:
            self._sync_progress = {"bytes": 0, "total": counter.flash_size}
        try:
            result = sync_history(counter, self.store, progress)
        finally:
            with self._lock:
                self._sync_progress = None
        # history under the old clock is safely stored, so this is the moment to fix it
        if self.set_clock and not result["error"]:
            self._clock_fix_requested = False
            self._fix_clock(counter)
        self._publish_status()

    def _set_state(self, state: str, error: str | None = None) -> None:
        with self._lock:
            if state != self._state:
                self._state_since = time.time()
            self._state = state
            if error is not None or state in ("live", "syncing"):
                self._error = error
        self._publish_status()

    def _publish_status(self) -> None:
        self.hub.publish("status", self.status())

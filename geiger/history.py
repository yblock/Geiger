"""Turn the counter's flash history into stored count intervals."""

from __future__ import annotations

import bisect
import datetime

import pygmc

# pygmc mode name -> seconds each record covers. The threshold modes only write
# while readings exceed a limit, so their stepped timestamps can't be trusted.
MODE_SECONDS = {"every second": 1, "every minute": 60, "every hour": 3600}


def device_epoch(when: datetime.datetime) -> float:
    """Counter clock reading as a plain seconds count.

    The counter has no time zone or DST, so its naive times are read as if they
    were UTC. Adding a measured offset (see ``ClockOffsets``) gives real time.
    """
    return when.replace(tzinfo=datetime.timezone.utc).timestamp()


class ClockOffsets:
    """Offsets (real time minus counter time) measured over the counter's life.

    Each entry applies from the counter time it was measured at until the next,
    so history recorded before someone reset the clock (say, for DST) keeps the
    offset that was true when it was recorded. Older records use the first entry.
    """

    def __init__(self, entries: list[list[float]] | None = None):
        self.entries = sorted(entries or [])

    def at(self, counter_time: float) -> float:
        if not self.entries:
            return 0.0
        i = bisect.bisect_right(self.entries, [counter_time, float("inf")])
        return self.entries[max(i - 1, 0)][1]

    def note(self, counter_time: float, offset: float, tolerance: float = 2.0) -> bool:
        """Record a measurement if the clock moved more than `tolerance` seconds.

        After the clock is set back, counter times it already showed come round
        again. Those now belong to the new clock; the old clock's latest offset
        is kept for everything before.
        """
        if self.entries and abs(self.latest - offset) <= tolerance:
            return False
        kept = [e for e in self.entries if e[0] < counter_time]
        reused = [e for e in self.entries if e[0] >= counter_time]
        if reused:
            kept.append([counter_time - 1, reused[-1][1]])
        self.entries = [*kept, [counter_time, offset]]
        return True

    @property
    def latest(self) -> float:
        """The most recent measurement: always the entry with the latest counter time."""
        return self.entries[-1][1]


PAGE = 2048  # bytes per history page read from the counter


def split_segments(image: bytes, page: int = PAGE) -> list[bytes]:
    """Runs of data between blank (all 0xFF) pages of a memory image, in time order.

    In a wrapped ring buffer the counter erases a little ahead of where it writes,
    so the newest stretch (from the start of memory) and the oldest (up to its end)
    are separated by blank pages. The recording runs on from the end of memory
    straight into its start, so those two are joined, oldest first: that way the
    newest stretch inherits the timestamp that precedes it instead of losing its
    first records, and the record split across the wrap is whole again.
    """
    segments, current = [], bytearray()
    for start in range(0, len(image), page):
        chunk = image[start : start + page]
        if chunk.count(0xFF) == len(chunk):
            if current:
                segments.append(bytes(current))
            current = bytearray()
        else:
            current += chunk
    if current:
        segments.append(bytes(current))
    first_page, last_page = image[:page], image[-page:]
    wraps = len(segments) > 1 and first_page.count(0xFF) != page and last_page.count(0xFF) != page
    if wraps:
        segments = [*segments[1:-1], segments[-1] + segments[0]]
    return segments


def history_intervals(
    segments: bytes | list[bytes], offsets: ClockOffsets, until: float | None = None
) -> tuple[list[tuple[int, int, int]], int]:
    """Parse flash history into (ts, seconds, count) rows.

    `segments` are runs of memory between blank pages (see ``device.Flash``).
    Each is parsed on its own, so a stretch that starts mid-record never borrows
    the timestamp of the stretch before it.

    pygmc stamps each record with the end of its interval in counter time.
    Records ending after `until` are dropped. Hourly averages are spread over
    the hour's minutes so per-minute rollups and partial ranges stay correct.
    Returns (rows, records dropped).
    """
    if isinstance(segments, bytes):
        segments = [segments]
    records = [r for seg in segments for r in pygmc.HistoryParser(data=seg).get_data()]
    rows = []
    dropped = 0
    for when, value, _unit, mode, _ref, _note in records:
        seconds = MODE_SECONDS.get(mode)
        counter_end = device_epoch(when)
        end = counter_end + offsets.at(counter_end)
        if seconds is None or (until is not None and end > until):
            dropped += 1
            continue
        start = round(end) - seconds
        if seconds == 3600:
            # the record is the hour's average CPM; whole counts, remainder spread evenly
            per_minute, extra = divmod(value * 60, 60)
            rows.extend((start + 60 * m, 60, per_minute + (m < extra)) for m in range(60))
        else:
            rows.append((start, seconds, value))
    return rows, dropped

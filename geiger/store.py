"""SQLite storage for counts, a per-minute rollup, and device metadata.

Every reading is stored as raw counts over an interval rather than as a rate, so
per-second heartbeat data, per-minute device history, and hourly device history
all aggregate the same way: rate = 60 * sum(count) / sum(seconds).
"""

from __future__ import annotations

import bisect
import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from .calibration import Calibration
from .history import ClockOffsets

LIVE = 0  # counted by the logger from the 1 s heartbeat
DEVICE = 1  # imported from the counter's flash history

# A rolling-window point needs at least this many seconds of data to be shown.
MIN_WINDOW_COVERAGE = 30
# A minute needs this many observed seconds to count toward min/peak.
FULL_MINUTE = 45

SCHEMA = """
CREATE TABLE IF NOT EXISTS counts (
    ts      INTEGER PRIMARY KEY,  -- interval start, unix seconds (UTC)
    seconds INTEGER NOT NULL,     -- interval length: 1 per CPS reading, 60 or 3600 for history
    count   INTEGER NOT NULL,
    source  INTEGER NOT NULL      -- LIVE or DEVICE
);

-- Kept in step with counts by the triggers below, so long ranges never scan per-second rows.
CREATE TABLE IF NOT EXISTS minutes (
    ts      INTEGER PRIMARY KEY,  -- minute start
    count   INTEGER NOT NULL,
    seconds INTEGER NOT NULL      -- seconds of exposure recorded in this minute
);

CREATE TRIGGER IF NOT EXISTS counts_insert AFTER INSERT ON counts BEGIN
    INSERT INTO minutes (ts, count, seconds) VALUES (NEW.ts - NEW.ts % 60, NEW.count, NEW.seconds)
    ON CONFLICT (ts) DO UPDATE SET count = count + excluded.count, seconds = seconds + excluded.seconds;
END;

CREATE TRIGGER IF NOT EXISTS counts_update AFTER UPDATE OF count ON counts BEGIN
    UPDATE minutes SET count = count + NEW.count - OLD.count WHERE ts = NEW.ts - NEW.ts % 60;
END;

CREATE TRIGGER IF NOT EXISTS counts_delete AFTER DELETE ON counts BEGIN
    UPDATE minutes SET count = count - OLD.count, seconds = seconds - OLD.seconds
    WHERE ts = OLD.ts - OLD.ts % 60;
END;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL           -- JSON
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # sqlite3 connections are per-thread; the logger writes while HTTP threads read
        self._local = threading.local()
        self._db().executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=10)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            self._local.db = db
        return db

    def release(self) -> None:
        """Close this thread's connection (HTTP request threads are short-lived)."""
        db = getattr(self._local, "db", None)
        if db is not None:
            db.close()
            self._local.db = None

    # writes

    def add_live(self, ts: int, cps: int) -> bool:
        """Store one heartbeat reading for the second starting at ts.

        A second that already holds a live reading absorbs the new count (the
        logger folds readings together when it has run ahead of the clock).
        Imported device rows are never touched; False means the reading was
        dropped because one already covers that second.
        """
        with self._db() as db:
            cur = db.execute(
                "INSERT INTO counts (ts, seconds, count, source) VALUES (?, 1, ?, ?) "
                "ON CONFLICT (ts) DO UPDATE SET count = count + excluded.count WHERE source = ?",
                (ts, cps, LIVE, LIVE),
            )
            return cur.rowcount > 0

    def import_intervals(
        self, rows: Iterable[tuple[int, int, int]], margin: int = 2
    ) -> tuple[int, int]:
        """Insert (ts, seconds, count) device rows that fall in gaps of what is stored.

        Rows within `margin` seconds of existing data are skipped, so device history
        never double-counts a stretch the logger already recorded live.
        Returns (imported, skipped).
        """
        rows = sorted(rows)
        if not rows:
            return 0, 0
        covered = self.coverage(rows[0][0], max(ts + s for ts, s, _ in rows))
        starts = [a for a, _ in covered]
        keep = []
        for ts, seconds, count in rows:
            i = bisect.bisect_left(starts, ts + seconds + margin)
            if i and covered[i - 1][1] > ts - margin:
                continue
            keep.append((ts, seconds, count, DEVICE))
        with self._db() as db:
            cur = db.executemany(
                "INSERT OR IGNORE INTO counts (ts, seconds, count, source) VALUES (?, ?, ?, ?)",
                keep,
            )
            imported = max(cur.rowcount, 0)
        return imported, len(rows) - imported

    def set_meta(self, key: str, value) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    # reads

    def get_meta(self, key: str, default=None):
        row = self._db().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def calibration(self) -> Calibration | None:
        data = self.get_meta("calibration")
        calibration = Calibration.from_dict(data) if data else None
        return calibration if calibration and calibration.segments else None

    def clock_offsets(self) -> ClockOffsets:
        return ClockOffsets(self.get_meta("clock_offsets"))

    def note_clock(self, counter_time: float, offset: float) -> ClockOffsets:
        """Remember a counter clock measurement when the clock has moved."""
        offsets = self.clock_offsets()
        if offsets.note(counter_time, offset):
            self.set_meta("clock_offsets", offsets.entries)
        return offsets

    def bounds(self) -> tuple[int, int] | None:
        """(start of the oldest interval, end of the newest), or None when empty."""
        db = self._db()
        first = db.execute("SELECT MIN(ts) FROM counts").fetchone()[0]
        if first is None:
            return None
        last = db.execute(
            "SELECT ts + seconds FROM counts ORDER BY ts DESC LIMIT 1"
        ).fetchone()[0]
        return first, last

    def extent(self, start: int, end: int) -> tuple[int, int] | None:
        """(first start, last end) of the intervals starting in [start, end), or None."""
        db = self._db()
        first = db.execute(
            "SELECT ts FROM counts WHERE ts >= ? AND ts < ? ORDER BY ts LIMIT 1", (start, end)
        ).fetchone()
        if first is None:
            return None
        last = db.execute(
            "SELECT ts + seconds FROM counts WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 1",
            (start, end),
        ).fetchone()
        return first[0], min(last[0], end)

    def first_session(self, gap: int, longest: int) -> dict | None:
        """The earliest recording session, if it lasted at most `longest` seconds.

        A session is a run of intervals with no break longer than `gap`. Returns
        {start, end, next, sources, records, counts}, `next` being where the following
        session starts, or None if the first session is longer or is the only one.
        Stops reading as soon as the answer is known, so it's cheap on big databases.
        """
        rows = self._db().execute("SELECT ts, ts + seconds, source FROM counts ORDER BY ts")
        first = rows.fetchone()
        if first is None:
            return None
        start, end, sources = first[0], first[1], {first[2]}
        for ts, stop, source in rows:
            if ts > end + gap:
                records, counts = self._db().execute(
                    "SELECT COUNT(*), SUM(count) FROM counts WHERE ts >= ? AND ts < ?", (start, end)
                ).fetchone()
                return {"start": start, "end": end, "next": ts, "sources": sorted(sources),
                        "records": records, "counts": counts}
            end = max(end, stop)
            sources.add(source)
            if end - start > longest:
                return None
        return None

    def delete_device_records(self, start: int, end: int) -> int:
        """Delete records copied from the counter's memory in [start, end)."""
        with self._db() as db:
            return db.execute(
                "DELETE FROM counts WHERE ts >= ? AND ts < ? AND source = ?", (start, end, DEVICE)
            ).rowcount

    def recent(self, since: int) -> list[tuple[int, int, int]]:
        """(ts, seconds, count) rows starting at or after `since`."""
        return self._db().execute(
            "SELECT ts, seconds, count FROM counts WHERE ts >= ? ORDER BY ts", (since,)
        ).fetchall()

    def coverage(self, start: int, end: int, slack: int = 2) -> list[tuple[int, int]]:
        """Merged [start, end) spans of stored data that touch [start, end)."""
        rows = self._db().execute(
            "SELECT ts, ts + seconds FROM counts WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start - 3600, end),
        )
        spans: list[list[int]] = []
        for a, b in rows:
            if b <= start:
                continue
            if spans and a <= spans[-1][1] + slack:
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        return [(a, b) for a, b in spans]

    def rate_series(self, start: int, end: int, step: int, tz_offset: int = 0) -> list[list]:
        """CPM over [start, end) as [t, cpm, min_cpm, max_cpm] rows; gaps are omitted.

        Steps under a minute give the rolling 60 s CPM the counter itself displays,
        sampled every `step` seconds. Longer steps average whole minutes into bins
        aligned to local time (`tz_offset` is seconds east of UTC) and report the
        quietest and busiest full minute in each bin.
        """
        if step < 60:
            return self._rolling_series(start, end, step)
        return self._binned_series(start, end, step, tz_offset)

    def _rolling_series(self, start: int, end: int, step: int) -> list[list]:
        base = start - 60
        n = end - base
        counts = [0.0] * n
        covered = [0.0] * n
        rows = self._db().execute(
            "SELECT ts, seconds, count FROM counts WHERE ts >= ? AND ts < ? AND ts + seconds > ?",
            (base - 3600, end, base),
        )
        for ts, seconds, count in rows:
            per_second = count / seconds
            for i in range(max(ts - base, 0), min(ts + seconds - base, n)):
                counts[i] += per_second
                covered[i] += 1
        sum_counts = [0.0]
        sum_covered = [0.0]
        for c, s in zip(counts, covered):
            sum_counts.append(sum_counts[-1] + c)
            sum_covered.append(sum_covered[-1] + s)

        series = []
        first = -(-start // step) * step  # first multiple of step at or after start
        for t in range(first, end + 1, step):
            hi = t - base
            lo = hi - 60
            seconds = sum_covered[hi] - sum_covered[lo]
            if seconds >= MIN_WINDOW_COVERAGE:
                cpm = 60 * (sum_counts[hi] - sum_counts[lo]) / seconds
                series.append([t, round(cpm, 2), None, None])
        return series

    def _binned_series(self, start: int, end: int, step: int, tz_offset: int) -> list[list]:
        rows = self._db().execute(
            """
            SELECT (ts + :off) / :step AS bin, SUM(count), SUM(seconds),
                   MIN(CASE WHEN seconds >= :full THEN 60.0 * count / seconds END),
                   MAX(CASE WHEN seconds >= :full THEN 60.0 * count / seconds END)
            FROM minutes WHERE ts >= :start AND ts < :end
            GROUP BY bin ORDER BY bin
            """,
            {"off": tz_offset, "step": step, "full": FULL_MINUTE, "start": start, "end": end},
        )
        series = []
        for bin_, count, seconds, low, high in rows:
            if not seconds:
                continue
            t = bin_ * step - tz_offset + step // 2
            series.append([t, round(60 * count / seconds, 2), _r(low), _r(high)])
        return series

    def summary(self, start: int, end: int, calibration: Calibration | None) -> dict:
        rows = self._db().execute(
            "SELECT ts, count, seconds FROM minutes WHERE ts >= ? AND ts < ? AND seconds > 0",
            (start - start % 60, end),
        ).fetchall()
        total_counts = sum(r[1] for r in rows)
        total_seconds = sum(r[2] for r in rows)
        full = [(60 * c / s, ts) for ts, c, s in rows if s >= FULL_MINUTE]
        peak = max(full, default=None)
        low = min(full, default=None)
        dose = None
        if calibration:
            dose = sum(calibration.usv_h(60 * c / s) * s / 3600 for _, c, s in rows)
        return {
            "start": start,
            "end": end,
            "counts": total_counts,
            "seconds": total_seconds,
            "coverage": min(total_seconds / (end - start), 1.0) if end > start else 0.0,
            "mean_cpm": 60 * total_counts / total_seconds if total_seconds else None,
            "peak_cpm": peak[0] if peak else None,
            "peak_ts": peak[1] if peak else None,
            "min_cpm": low[0] if low else None,
            "dose_usv": dose,
        }

    def minutes_between(self, start: int, end: int) -> list[tuple[int, int, int]]:
        return self._db().execute(
            "SELECT ts, count, seconds FROM minutes WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start, end),
        ).fetchall()


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 2)

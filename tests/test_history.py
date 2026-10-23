import datetime

from geiger.history import ClockOffsets, device_epoch, history_intervals


def marker(when: datetime.datetime, mode: int) -> bytes:
    stamp = [when.year - 2000, when.month, when.day, when.hour, when.minute, when.second]
    return b"\x55\xaa\x00" + bytes(stamp) + b"\x55\xaa" + bytes([mode])


START = datetime.datetime(2026, 10, 23, 7, 0, 0)  # counter time
C0 = device_epoch(START)
CDT = 5 * 3600  # counter set to US Central daylight time: real = counter + 5 h
T0 = int(C0) + CDT
OFFSETS = ClockOffsets([[C0 - 86400, CDT]])


def test_per_second_records_start_where_their_second_starts():
    raw = marker(START, 1) + bytes([1, 2, 0, 3])
    rows, dropped = history_intervals(raw, OFFSETS)
    assert rows == [(T0, 1, 1), (T0 + 1, 1, 2), (T0 + 2, 1, 0), (T0 + 3, 1, 3)]
    assert dropped == 0


def test_cutoff_drops_records_ending_later():
    raw = marker(START, 1) + bytes([1, 2, 0, 3])
    rows, dropped = history_intervals(raw, OFFSETS, until=T0 + 3)
    assert [r[0] for r in rows] == [T0, T0 + 1, T0 + 2]
    assert dropped == 1


def test_counter_time_ignores_dst():
    # a counter left on daylight time keeps one offset across the November change
    before = datetime.datetime(2026, 10, 31, 12, 0, 0)
    after = datetime.datetime(2026, 11, 2, 12, 0, 0)
    raw = marker(before, 1) + bytes([1]) + marker(after, 1) + bytes([1])
    rows, _ = history_intervals(raw, OFFSETS)
    assert rows[1][0] - rows[0][0] == 2 * 86400


def test_offsets_follow_clock_changes():
    # clock was reset an hour back on Nov 1: records before keep the old offset
    reset = device_epoch(datetime.datetime(2026, 11, 1, 1, 0, 0))
    offsets = ClockOffsets([[C0 - 86400, CDT]])
    assert offsets.note(reset, CDT + 3600)
    assert not offsets.note(reset + 600, CDT + 3602)  # drift within tolerance
    assert offsets.at(reset - 1) == CDT
    assert offsets.at(reset + 1) == CDT + 3600
    assert offsets.at(C0 - 10 * 86400) == CDT  # older than any measurement
    assert ClockOffsets().at(C0) == 0.0


def test_setting_the_clock_back_hands_reused_times_to_the_new_clock():
    # counter ran 9 s fast; measured at counter time X, then set back to the true time
    x = C0 + 5000
    offsets = ClockOffsets([[C0, CDT - 9]])
    offsets.note(x, CDT - 9)  # same clock, no new entry
    assert offsets.note(x - 9 + 2, CDT)  # the reset clock reads 9 s earlier
    assert offsets.at(x + 3600) == CDT  # everything recorded after the reset
    assert offsets.at(x - 100) == CDT - 9  # recorded before it
    assert offsets.at(C0 - 1) == CDT - 9
    assert offsets.latest == CDT


def test_minute_hour_and_threshold_modes():
    raw = (
        marker(START, 2) + bytes([20, 25])  # CPM every minute
        + marker(START, 3) + bytes([18])  # average CPM every hour
        + marker(START, 5) + bytes([90])  # every minute above a threshold
    )
    rows, dropped = history_intervals(raw, OFFSETS)
    assert rows[:2] == [(T0, 60, 20), (T0 + 60, 60, 25)]
    hourly = rows[2:]
    # the hour's 1080 counts spread across its 60 minutes
    assert [r[0] for r in hourly] == [T0 + 60 * m for m in range(60)]
    assert all(r[1] == 60 and r[2] == 18 for r in hourly)
    assert dropped == 1


def test_uneven_hourly_counts_keep_their_total():
    rows, _ = history_intervals(marker(START, 3) + bytes([7]), OFFSETS)
    assert sum(r[2] for r in rows) == 7 * 60


def test_two_byte_counts():
    raw = marker(START, 1) + b"\x55\xaa\x01\x01\x2c"  # 300 counts
    assert history_intervals(raw, OFFSETS)[0] == [(T0, 1, 300)]


def test_empty_flash():
    assert history_intervals(b"", OFFSETS) == ([], 0)

import struct

import pytest

from geiger import demo, settings


def test_decode_reads_the_layout():
    values = settings.decode(demo.SETTINGS)
    assert values["save_mode"] == 2
    assert values["alarm_usv"] == pytest.approx(0.5)
    assert (values["cal_cpm_0"], values["cal_usv_0"]) == (1538, 10.0)
    assert settings.pygmc_config(demo.SETTINGS)["Calibration_uSv_2"] == 200.0


def test_encode_changes_only_what_is_asked():
    new = settings.encode(demo.SETTINGS, {"speaker": True, "alarm_cpm": 250, "alarm_usv": 1.25})
    changed = {i for i in range(settings.SIZE) if new[i] != demo.SETTINGS[i]}
    assert changed <= {2, 6, 7, 27, 28, 29, 30}
    assert new[2] == 1
    assert struct.unpack(">H", new[6:8])[0] == 250  # 16-bit values are big-endian
    assert struct.unpack("<f", new[27:31])[0] == 1.25  # floats little-endian on the 320


def test_backlight_timer_and_large_font_codes():
    # as read off the counter: Light OFF 0, Light ON 1, "Timeout: n" n + 1; CPM, µSv/h, mR/h
    new = settings.encode(demo.SETTINGS, {"backlight_timer": 6, "large_font_unit": 0})
    assert (new[4], new[52]) == (6, 0)
    timer = settings.BY_KEY["backlight_timer"].options
    assert (timer[0], timer[1], timer[6], timer[31]) == (
        "Always off", "Always on", "Turns off after 5 s", "Turns off after 30 s")
    with pytest.raises(ValueError):
        settings.encode(demo.SETTINGS, {"backlight_timer": 32})


def test_display_levels_stay_inside_the_counters_own_ranges():
    new = settings.encode(demo.SETTINGS, {"contrast": 40, "backlight_level": 0})
    assert (new[48], new[53]) == (40, 0)
    for changes in ({"contrast": 9}, {"contrast": 41}, {"backlight_level": 11}, {"contrast": 20.5}):
        with pytest.raises(ValueError):
            settings.encode(demo.SETTINGS, changes)


@pytest.mark.parametrize("changes, message", [
    ({"save_mode": 9}, "not one of"),
    ({"alarm_cpm": 0}, "between"),
    ({"alarm_cpm": 2.5}, "whole number"),
    ({"alarm_usv": 1e6}, "between"),
    ({"speaker": 2}, "on or off"),
    ({"baud_rate": 1}, "unknown setting"),
])
def test_encode_rejects_bad_values(changes, message):
    with pytest.raises(ValueError, match=message):
        settings.encode(demo.SETTINGS, changes)


def test_same_ignores_bytes_the_counter_rewrites_itself():
    stamped = bytearray(demo.SETTINGS)
    stamped[0] = 0xFF  # its power state, which it sets itself (seen as both 0x00 and 0xFF)
    stamped[49:51] = b"\x00\x0b"  # its highest CPM
    stamped[121:127] = bytes([26, 10, 23, 11, 0, 12])  # when the settings were saved
    assert settings.same(bytes(stamped), demo.SETTINGS)
    stamped[2] ^= 1
    assert not settings.same(bytes(stamped), demo.SETTINGS)


def test_incomplete_spots_an_erased_or_half_written_block():
    assert not settings.incomplete(demo.SETTINGS)
    assert settings.incomplete(b"\xff" * 256)
    half = demo.SETTINGS[:20] + b"\xff" * 236  # cut off before the save setting
    assert settings.incomplete(half)


def test_unknown_values_are_flagged_not_hidden():
    odd = bytearray(demo.SETTINGS)
    odd[33] = 7  # a rotation value this layout doesn't know
    swivel = next(f for f in settings.describe_fields(bytes(odd)) if f["key"] == "swivel")
    assert swivel["value"] == 7 and swivel["known"] is False

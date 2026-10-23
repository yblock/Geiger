"""The counter's 256-byte settings block: what each byte means, and safe edits.

Layout from GQ's own firmware enum (posted by GQ on their forum, topic 4948) and
pygmc's map, checked against a GMC-320S (firmware Re 1.17): every field below
decodes to the values set on that counter. Floats are little-endian on the
300/320 series, 16-bit integers big-endian.

Only the fields listed here are ever changed. Everything else, including bytes
whose meaning isn't known, is written back exactly as it was read.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

SIZE = 256

# The counter keeps these itself: its power state (byte 0 has read both 0x00 and
# 0xFF on a running counter, and it sets it again after an update), its highest
# CPM so far, and a timestamp of when the settings were last saved, all rewritten
# about a second after any settings update.
DEVICE_MANAGED = frozenset({0, 49, 50, *range(121, 127)})

# One-byte switches and choices, none of which has 0xFF as a valid value, spread
# across the block. An erased block reads 0xFF in all of them. (Multi-byte numbers
# can legitimately contain 0xFF, so they're no evidence either way.)
SENTINELS = (1, 2, 31, 32, 33, 44, 54, 55, 56, 57, 60, 64)


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    offset: int
    kind: str  # bool | choice | u8 | u16 | f32
    options: dict[int, str] = field(default_factory=dict)
    unit: str = ""
    low: float | None = None
    high: float | None = None
    help: str = ""

    @property
    def size(self) -> int:
        return {"bool": 1, "choice": 1, "u8": 1, "u16": 2, "f32": 4}[self.kind]

    def read(self, raw: bytes):
        b = raw[self.offset : self.offset + self.size]
        if self.kind in ("bool", "choice", "u8"):
            return b[0]
        if self.kind == "u16":
            return struct.unpack(">H", b)[0]
        value = struct.unpack("<f", b)[0]
        return round(value, 4) if math.isfinite(value) else None  # erased bytes read as NaN

    def check(self, value):
        """The value as stored, or ValueError explaining what's allowed.

        Values arrive from JSON, so anything but a plain number (bool for on/off
        fields) is refused rather than coerced.
        """
        if self.kind == "bool":
            if value not in (0, 1) or isinstance(value, float):
                raise ValueError(f"{self.label}: expected on or off")
            return int(value)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.label}: expected a number")
        if not math.isfinite(value):
            raise ValueError(f"{self.label}: expected a finite number")
        if self.kind in ("choice", "u8", "u16") and value != int(value):
            raise ValueError(f"{self.label}: expected a whole number")
        if self.kind == "choice":
            if int(value) not in self.options:
                raise ValueError(f"{self.label}: not one of {', '.join(self.options.values())}")
            return int(value)
        if not self.low <= value <= self.high:
            raise ValueError(f"{self.label}: must be between {self.low:g} and {self.high:g}")
        return float(value) if self.kind == "f32" else int(value)

    def write(self, buf: bytearray, value) -> None:
        if self.kind in ("bool", "choice", "u8"):
            buf[self.offset] = value
        elif self.kind == "u16":
            struct.pack_into(">H", buf, self.offset, value)
        else:
            struct.pack_into("<f", buf, self.offset, value)


ON_OFF = "bool"
SAVE_MODES = {
    0: "Off",
    1: "Every second",
    2: "Every minute",
    3: "Every hour (average)",
    4: "Every second, above threshold",
    5: "Every minute, above threshold",
}

FIELDS = (
    Field("speaker", "Speaker", "Sound and alarm", 2, ON_OFF, help="A click for every count"),
    Field("led", "LED", "Sound and alarm", 60, ON_OFF, help="A blink for every count"),
    Field("alarm", "Alarm", "Sound and alarm", 1, ON_OFF),
    Field("alarm_type", "Alarm triggers on", "Sound and alarm", 31, "choice", {0: "CPM", 1: "µSv/h"}),
    Field("alarm_cpm", "Alarm level", "Sound and alarm", 6, "u16", unit="CPM", low=1, high=65535),
    Field("alarm_usv", "Alarm level", "Sound and alarm", 27, "f32", unit="µSv/h", low=0.01, high=9999),
    Field("save_mode", "Save to memory", "Data saving", 32, "choice", SAVE_MODES,
          help="What the counter records while it's away from the Mac"),
    Field("threshold_type", "Threshold in", "Data saving", 64, "choice", {0: "CPM", 1: "µSv/h", 2: "mR/h"}),
    Field("threshold_cpm", "Threshold", "Data saving", 62, "u16", unit="CPM", low=1, high=65535),
    Field("threshold_usv", "Threshold", "Data saving", 65, "f32", unit="µSv/h", low=0.01, high=9999),
    Field("power_saving", "Power saving", "Display and power", 44, ON_OFF,
          help="Turns the screen off after 30 seconds without a key press"),
    Field("motion_detect", "Motion detection", "Display and power", 55, ON_OFF,
          help="Needed for auto-rotate and tap gestures"),
    Field("swivel", "Screen rotation", "Display and power", 33, "choice",
          {0: "Normal", 1: "Upside down", 2: "Automatic"}),
    Field("reverse_display", "Reverse display", "Display and power", 54, ON_OFF,
          help="Light text on a dark background"),
    # ranges as the counter's own menu allows them (checked on a GMC-320S): going past
    # them could leave a screen too faint or too dark to read the menu on
    Field("contrast", "Contrast", "Display and power", 48, "u8", low=10, high=40),
    Field("backlight_level", "Backlight level", "Display and power", 53, "u8", low=0, high=10),
    # codes read off a GMC-320S while its menu was changed by hand: Light OFF = 0,
    # Light ON = 1, and "Timeout: n" (1-30 seconds) = n + 1
    Field("backlight_timer", "Backlight", "Display and power", 4, "choice",
          {1: "Always on", 0: "Always off",
           **{n + 1: f"Turns off after {n} s" for n in range(1, 31)}}),
    Field("large_font_unit", "Large font mode shows", "Display and power", 52, "choice",
          {0: "CPM", 1: "µSv/h", 2: "mR/h"}),
    Field("battery", "Battery type", "Display and power", 56, "choice",
          {0: "Rechargeable", 1: "Non-rechargeable"}),
    *(
        Field(f"cal_{part}_{i}", f"Point {i + 1} {label}", "Calibration", offset, kind,
              unit=unit, low=low, high=high)
        for i, base in enumerate((8, 14, 20))
        for part, label, offset, kind, unit, low, high in (
            ("cpm", "CPM", base, "u16", "CPM", 1, 65535),
            ("usv", "µSv/h", base + 2, "f32", "µSv/h", 0.001, 9999),
        )
    ),
)
BY_KEY = {f.key: f for f in FIELDS}
CALIBRATION_KEYS = tuple(f.key for f in FIELDS if f.group == "Calibration")


def calibration_points(raw: bytes) -> list[list]:
    """[[cpm, usv_h], ...] for the three calibration points."""
    v = decode(raw)
    return [[v[f"cal_cpm_{i}"], v[f"cal_usv_{i}"]] for i in range(3)]


def calibration_changes(points: list[list]) -> dict:
    """The {key: value} changes that set the calibration to `points`."""
    changes = {}
    for i, (cpm, usv) in enumerate(points):
        changes[f"cal_cpm_{i}"], changes[f"cal_usv_{i}"] = cpm, usv
    return changes


def same_calibration(a: list[list], b: list[list]) -> bool:
    return all(ca == cb and abs(ua - ub) < 1e-4 for (ca, ua), (cb, ub) in zip(a, b))


def decode(raw: bytes) -> dict:
    return {f.key: f.read(raw) for f in FIELDS}


def encode(raw: bytes, changes: dict) -> bytes:
    """A copy of `raw` with `changes` ({key: value}) applied; ValueError if any is invalid."""
    if len(raw) != SIZE:
        raise ValueError("settings block must be 256 bytes")
    buf = bytearray(raw)
    for key, value in changes.items():
        f = BY_KEY.get(key)
        if f is None:
            raise ValueError(f"unknown setting: {key}")
        f.write(buf, f.check(value))
    return bytes(buf)


def same(a: bytes, b: bytes) -> bool:
    """Equal apart from the bytes the counter maintains itself."""
    return all(a[i] == b[i] for i in range(SIZE) if i not in DEVICE_MANAGED)


def incomplete(raw: bytes) -> bool:
    """True when the block looks erased or half-written.

    A last line of defense: a write that didn't finish is caught for certain by
    the write record the logger keeps (see ``logger.change_settings``).
    """
    return any(raw[i] == 0xFF for i in SENTINELS)


def pygmc_config(raw: bytes) -> dict:
    """The subset of settings the rest of Geiger uses, under pygmc's names."""
    v = decode(raw)
    config = {
        "Alarm": v["alarm"],
        "AlarmType": v["alarm_type"],
        "AlarmCPM": v["alarm_cpm"],
        "AlarmValue_uSv": v["alarm_usv"],
        "Speaker": v["speaker"],
        "SaveDataType": v["save_mode"],
    }
    for i in range(3):
        config[f"CalibrationCPM_{i}"] = v[f"cal_cpm_{i}"]
        config[f"Calibration_uSv_{i}"] = v[f"cal_usv_{i}"]
    return config


def describe_fields(raw: bytes) -> list[dict]:
    """Every editable setting with its current value, for the dashboard."""
    out = []
    for f in FIELDS:
        value = f.read(raw)
        item = {"key": f.key, "label": f.label, "group": f.group, "kind": f.kind, "value": value}
        if f.options:
            item["options"] = {str(k): v for k, v in f.options.items()}
            item["known"] = value in f.options
        if f.kind == "bool":
            item["known"] = value in (0, 1)
        if f.unit:
            item["unit"] = f.unit
        if f.low is not None:
            item["min"], item["max"] = f.low, f.high
        if f.help:
            item["help"] = f.help
        out.append(item)
    return out

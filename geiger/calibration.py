"""CPM to µSv/h conversion from the counter's own calibration points.

This mirrors pygmc's ``DeviceRFC1201.get_usv_h``: a piecewise-linear curve through
the origin and the three (CPM, µSv/h) points stored in the device config, with the
last segment extrapolated past the highest point. It lives here, apart from the
device object, so stored history can still be converted while the counter is
unplugged. ``tests/test_calibration.py`` checks it against pygmc itself.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Calibration:
    # (cpm, usv_h) pairs, in the order the device config stores them
    points: tuple[tuple[int, float], ...]

    @classmethod
    def from_config(cls, config: dict) -> Calibration:
        return cls(
            tuple(
                (int(config[f"CalibrationCPM_{i}"]), float(config[f"Calibration_uSv_{i}"]))
                for i in range(3)
            )
        )

    @property
    def segments(self) -> tuple[tuple[int, float, float], ...]:
        """(max_cpm, slope, intercept) per segment, built the way pygmc builds them."""
        points = [(0, 0.0), *self.points]
        segments = []
        for (cpm_s, usv_s), (cpm_e, usv_e) in zip(points, points[1:]):
            if cpm_e > cpm_s:
                slope = (usv_e - usv_s) / (cpm_e - cpm_s)
                segments.append((cpm_e, slope, usv_e - slope * cpm_e))
        return tuple(segments)

    def usv_h(self, cpm: float) -> float:
        segments = self.segments
        for max_cpm, slope, intercept in segments:
            if cpm <= max_cpm:
                return cpm * slope + intercept
        _, slope, intercept = segments[-1]
        return cpm * slope + intercept

    def to_dict(self) -> dict:
        return {"points": [list(p) for p in self.points]}

    @classmethod
    def from_dict(cls, data: dict) -> Calibration:
        return cls(tuple((int(c), float(u)) for c, u in data["points"]))

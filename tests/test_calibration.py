import pytest
from pygmc import GMC320S
from pygmc.connection import Connection

from geiger.calibration import Calibration

CONFIGS = [
    # this GMC-320S's factory calibration
    {"CalibrationCPM_0": 1538, "Calibration_uSv_0": 10.0,
     "CalibrationCPM_1": 15380, "Calibration_uSv_1": 100.0,
     "CalibrationCPM_2": 30760, "Calibration_uSv_2": 200.0},
    # bent curve, and a point lower than the one before it (pygmc skips that segment)
    {"CalibrationCPM_0": 100, "Calibration_uSv_0": 0.5,
     "CalibrationCPM_1": 1000, "Calibration_uSv_1": 7.0,
     "CalibrationCPM_2": 800, "Calibration_uSv_2": 6.0},
]


class FakeConnection(Connection):
    def __init__(self):
        pass

    def write(self, cmd, log=True):
        pass

    def reset_buffers(self):
        pass


def pygmc_usv_h(config: dict, cpm: float) -> float:
    device = GMC320S(port=None, connection=FakeConnection())
    device._config = dict(config)
    return device.get_usv_h(cpm=cpm)


@pytest.mark.parametrize("config", CONFIGS)
@pytest.mark.parametrize("cpm", [0, 1, 17, 99.5, 100, 101, 999, 1000, 1538, 20000, 40000])
def test_matches_pygmc(config, cpm):
    ours = Calibration.from_config(config).usv_h(cpm)
    assert ours == pytest.approx(pygmc_usv_h(config, cpm))


def test_round_trip():
    calibration = Calibration.from_config(CONFIGS[0])
    assert Calibration.from_dict(calibration.to_dict()) == calibration
    assert calibration.usv_h(17) == pytest.approx(0.1105, abs=1e-4)

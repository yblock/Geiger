import pytest

from geiger.cli import COMMANDS, build_parser


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    for name in ("GEIGER_DB", "GEIGER_DEVICE", "GEIGER_HTTP_PORT"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("command", ["run", "sync", "info", "set-clock", "install-agent", "uninstall-agent"])
def test_real_commands_default_to_the_real_database(command):
    args = build_parser().parse_args([command])
    assert args.db == "data/geiger.db"
    assert args.http_port == 8320


def test_demo_defaults_to_a_temporary_database_without_changing_the_others():
    parser = build_parser()
    assert parser.parse_args(["demo"]).db is None
    assert parser.parse_args(["run"]).db == "data/geiger.db"  # the bug: demo's default leaked
    assert parser.parse_args(["demo", "--db", "x.db"]).db == "x.db"


def test_every_command_has_a_handler():
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert set(choices) == set(COMMANDS)

from oncall.config import Settings
from oncall.safety import killswitch


def _settings(tmp_path) -> Settings:
    return Settings(KILL_SWITCH_FILE=str(tmp_path / ".killswitch"))


def test_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(killswitch.ENV_VAR, raising=False)
    s = _settings(tmp_path)
    assert killswitch.engaged(s) is False
    assert "off" in killswitch.status(s)


def test_engage_and_disengage_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv(killswitch.ENV_VAR, raising=False)
    s = _settings(tmp_path)
    path = killswitch.engage(s)
    assert path.exists()
    assert killswitch.engaged(s) is True
    assert "ENGAGED" in killswitch.status(s)
    assert killswitch.engage(s) == path  # idempotent
    assert killswitch.disengage(s) is True
    assert killswitch.engaged(s) is False
    assert killswitch.disengage(s) is False  # already off


def test_environment_variable_engages(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setenv(killswitch.ENV_VAR, "1")
    assert killswitch.engaged(s) is True
    assert killswitch.ENV_VAR in killswitch.status(s)
    monkeypatch.setenv(killswitch.ENV_VAR, "0")
    assert killswitch.engaged(s) is False


def test_cli_killswitch_on_status_off(tmp_path, monkeypatch, capsys):
    from oncall import main as cli

    monkeypatch.delenv(killswitch.ENV_VAR, raising=False)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / ".killswitch"))
    assert cli.main(["killswitch", "on"]) == 0
    assert (tmp_path / ".killswitch").exists()
    assert cli.main(["killswitch", "status"]) == 0
    assert "ENGAGED" in capsys.readouterr().out
    assert cli.main(["killswitch", "off"]) == 0
    assert not (tmp_path / ".killswitch").exists()
    assert cli.main(["killswitch", "status"]) == 0
    assert "off" in capsys.readouterr().out

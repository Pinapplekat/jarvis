import pytest

from jarvis.contract.envfile import load_secrets
from jarvis.core.settings import SettingsError, load_settings


def test_defaults(data_dir):
    s = load_settings()
    assert s.data_dir == data_dir
    assert (s.assistant_name, s.log_level, s.apps) == ("Falak", "INFO", {})


def test_default_data_dir(monkeypatch):
    monkeypatch.delenv("FALAK_DATA_DIR", raising=False)
    assert load_settings().data_dir.name == "Falak-dev"


def test_toml_overrides(data_dir):
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text(
        'assistant_name = "Test"\nlog_level = "DEBUG"\n[apps.camera]\nenabled = true\n'
    )
    s = load_settings()
    assert s.assistant_name == "Test" and s.log_level == "DEBUG"
    assert s.apps == {"camera": {"enabled": True}}


def test_unknown_key_raises(data_dir):
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text("assistent_name = 'typo'\n")
    with pytest.raises(SettingsError, match="assistent_name"):
        load_settings()


def test_env_parser(tmp_path):
    (tmp_path / ".env").write_text(
        "# comment\n\nA=1\nB=\"two words\"\n  C = 'x'  \nbad line\nD=a=b\n"
    )
    assert load_secrets(tmp_path) == {"A": "1", "B": "two words", "C": "x", "D": "a=b"}


def test_env_missing(tmp_path):
    assert load_secrets(tmp_path) == {}


def test_job_settings_have_defaults(data_dir):
    s = load_settings()
    assert (s.timezone, s.quiet_hours, s.catch_up_minutes, s.notify_channel, s.thinking_runs_per_day) == (
        "America/Los_Angeles", "23-7", 120, "discord", 12)
    assert s.tz().key == "America/Los_Angeles"


@pytest.mark.parametrize("line,fragment", [
    ('timezone = "Mars/Olympus"', "'timezone'"),
    ("timezone = 5", "'timezone' must be text"),
    ('quiet_hours = "late"', "'quiet_hours'"),
    ('quiet_hours = "25-7"', "'quiet_hours'"),
    ("quiet_hours = 3", "'quiet_hours'"),
    ("catch_up_minutes = 0", "'catch_up_minutes'"),
    ("thinking_runs_per_day = -1", "'thinking_runs_per_day'"),
    ('notify_channel = ""', "'notify_channel'"),
])
def test_bad_job_settings(data_dir, line, fragment):
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text(line + "\n")
    with pytest.raises(SettingsError, match=fragment):
        load_settings()


def test_quiet_hours_can_be_turned_off_and_the_zone_changed(data_dir):
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text('quiet_hours = ""\ntimezone = "America/New_York"\n')
    s = load_settings()
    assert s.quiet_hours == "" and s.tz().key == "America/New_York"

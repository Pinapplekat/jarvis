import pytest

from jarvis.core.settings import SettingsError, load_secrets, load_settings


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

import pytest

from jarvis.core.settings import load_settings


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "falak-data"
    monkeypatch.setenv("FALAK_DATA_DIR", str(d))
    return d


@pytest.fixture
def settings(data_dir):
    return load_settings()

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


@pytest.fixture
def real_apps(settings):
    from jarvis.core.apps import load_apps

    reg = load_apps(settings)
    yield reg
    reg.close()


@pytest.fixture
def echo(settings):
    from pathlib import Path

    from jarvis.core.apps import load_apps

    reg = load_apps(settings, Path(__file__).parent / "tests" / "fixtures" / "apps")
    mod = reg.apps["echo"].module
    for store in (mod.NOTES, mod.SENT, mod.WIPED):
        store.clear()
    mod.CONTEXT = ""
    yield reg
    reg.close()

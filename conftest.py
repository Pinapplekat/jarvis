import pytest

from jarvis.core.settings import load_settings


@pytest.fixture(autouse=True)
def _no_real_desk(monkeypatch):
    """No test starts the real desk: it downloads models, builds Falak Ears and opens it, which asks for the
    microphone (and, with the old named pipes, hung LaunchServices). A node test that loads every app, as the terminal
    link tests do, would otherwise do all of that."""
    try:
        from jarvis.apps.desk import actions as desk
    except ImportError:
        return
    monkeypatch.setattr(desk.Runner, "start", lambda self: None)


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


@pytest.fixture
def make_ctx(tmp_path):
    """An AppContext the way a device node builds one: no database, manifest-default settings plus overrides."""
    from jarvis.contract.api import AppContext
    from jarvis.contract.loader import DEFAULT_APPS_DIR
    from jarvis.contract.manifest import load_manifest

    def make(app: str, **overrides):
        manifest = load_manifest(DEFAULT_APPS_DIR / app / "manifest.toml")
        settings = {k: v["default"] for k, v in manifest.settings.items()} | overrides
        tmp = tmp_path / "node-tmp"
        tmp.mkdir(mode=0o700, exist_ok=True)
        ctx = AppContext(app, settings, {}, None, tmp, data_dir=tmp_path / "node-data" / app)
        ctx.notify_hook = lambda name, text: ctx.said.append(text)
        ctx.said = []  # what the app told Elijah on its own
        return ctx

    return make


@pytest.fixture
def jobs_reg(settings):
    """A registry holding only the ticker fixture app, whose jobs do whatever the test says."""
    from pathlib import Path

    from jarvis.core.apps import load_apps

    reg = load_apps(settings, Path(__file__).parent / "tests" / "fixtures" / "jobapps")
    reg.apps["ticker"].module.reset()
    yield reg
    reg.close()

import pytest
from browser_fakes import Clock, FakeDriver, Site, resolver

from jarvis.apps.browser import actions, manager as manager_mod
from jarvis.apps.browser.sites import Sites


@pytest.fixture
def driver():
    return FakeDriver({"https://example.com/": Site(), "https://example.com/next": Site(title="Next page")})


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sites(tmp_path):
    return Sites(tmp_path / "browser")


@pytest.fixture
def mgr(driver, sites, clock):
    m = manager_mod.Manager(driver, sites, clock=clock, autosweep=False, resolve=resolver())
    yield m
    m.stop()


@pytest.fixture
def app(driver, make_ctx, monkeypatch):
    """The app's handlers over the fake: `ctx` for calling them, `driver` for what happened."""
    monkeypatch.setattr(actions, "make_driver", lambda: driver)
    monkeypatch.setattr(actions, "_manager", None)
    monkeypatch.setattr(actions, "resolve", resolver())
    ctx = make_ctx("browser")
    yield ctx
    actions.stop(ctx)

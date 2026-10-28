"""fetch_json: a GET with the profile, listed sites only, and `signed_out` when the site sends him to a login."""

import pytest
from browser_fakes import Site

from jarvis.apps.browser import actions
from jarvis.contract.api import ActionError

API = "https://canvas.test/api/v1/courses"


@pytest.fixture
def canvas(app, driver):
    actions.manager(app).sites.add("canvas.test")
    return driver


def test_a_listed_site_returns_its_json_and_the_next_page(app, canvas):
    canvas.json[API] = (200, [{"id": 1}], "https://canvas.test/api/v1/courses?page=2", API)
    out = actions.fetch_json(app, API)
    assert out.data["json"] == [{"id": 1}] and out.data["signed_out"] is False
    assert out.data["next"].endswith("page=2") and canvas.gets == [API]


def test_an_unlisted_site_is_refused_without_any_request(app, driver):
    out = actions.fetch_json(app, "https://example.com/api/v1/courses")
    assert out.data["signed_out"] is True and out.data["listed"] is False
    assert driver.gets == [] and driver.launches == []


def test_a_redirect_to_a_login_page_means_signed_out(app, canvas):
    canvas.json[API] = (302, None, None, "https://canvas.test/login/saml")
    out = actions.fetch_json(app, API)
    assert out.data["signed_out"] is True and out.data["listed"] is True and "sign in again" in out.text


def test_a_401_or_an_html_page_means_signed_out(app, canvas):
    canvas.json[API] = (401, None, None, API)
    assert actions.fetch_json(app, API).data["signed_out"] is True
    canvas.json[API] = (200, None, None, API)  # 200, but not JSON: a login form
    assert actions.fetch_json(app, API).data["signed_out"] is True


def test_a_server_error_is_an_error_not_a_sign_in_request(app, canvas):
    canvas.json[API] = (500, None, None, API)
    with pytest.raises(ActionError, match="answered 500"):
        actions.fetch_json(app, API)


def test_it_never_reaches_this_mac(app, canvas):
    actions.manager(app).sites.add("inside.test")
    with pytest.raises(ActionError, match="local network"):
        actions.fetch_json(app, "https://inside.test/api/v1/courses")
    assert canvas.gets == []


def test_it_has_no_way_to_send_anything_but_a_get():
    from jarvis.contract.loader import DEFAULT_APPS_DIR
    from jarvis.contract.manifest import load_manifest
    spec = next(a for a in load_manifest(DEFAULT_APPS_DIR / "browser" / "manifest.toml").actions if a.name == "fetch_json")
    assert list(spec.input["properties"]) == ["url"] and spec.effect == "read" and spec.share and not spec.tool


def test_the_real_driver_only_ever_calls_get():
    import inspect
    from jarvis.apps.browser import engine
    src = inspect.getsource(engine._PwSession.get_json)
    assert "request.get(" in src and not any(f"request.{m}(" in src for m in ("post", "put", "delete", "patch", "fetch"))

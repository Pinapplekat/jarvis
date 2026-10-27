import subprocess
from types import SimpleNamespace

import pytest

from jarvis.apps.screen import actions as screen
from jarvis.contract.api import ActionError


@pytest.fixture
def mac(monkeypatch):
    """The window list, the lock state and screencapture replaced; `calls` records every subprocess."""
    state = {"window": screen.Window("Safari", "Falak notes"), "locked": False, "write": b"jpeg", "returncode": 0,
             "calls": []}

    def run(cmd, **kw):
        state["calls"].append(cmd)
        if cmd[0] == screen.SCREENCAPTURE and state["write"] is not None:
            open(cmd[-1], "wb").write(state["write"])
        return SimpleNamespace(returncode=state["returncode"], stdout="", stderr="")

    monkeypatch.setattr(screen, "front_window", lambda: state["window"])
    monkeypatch.setattr(screen, "locked", lambda: state["locked"])
    monkeypatch.setattr(screen.subprocess, "run", run)
    return state


def captured(mac):
    return [c for c in mac["calls"] if c[0] == screen.SCREENCAPTURE]


def test_look_takes_one_scaled_jpeg(make_ctx, mac):
    result = screen.look(make_ctx("screen"))
    (att,) = result.attachments
    assert att.mime == "image/jpeg" and att.path.read_bytes() == b"jpeg"
    assert captured(mac)[0][:4] == [screen.SCREENCAPTURE, "-x", "-t", "jpg"]
    assert [screen.SIPS, "-Z", "1600"] == mac["calls"][1][:3]


@pytest.mark.parametrize("app,title", [
    ("1Password", "Vault"), ("Messages", "Mom"), ("Safari", "Chase Bank - Accounts"),
    ("Chrome", "Sign in to your account"), ("Safari", "My PayPal"), ("Keychain Access", None),
])
def test_excluded_windows_are_not_captured(make_ctx, mac, app, title):
    mac["window"] = screen.Window(app, title)
    result = screen.look(make_ctx("screen"))
    assert result.text == screen.DIDNT_LOOK and result.attachments == ()
    assert captured(mac) == []


def test_extra_exclusions_come_from_settings(make_ctx, mac):
    mac["window"] = screen.Window("Notes", "Taxes 2026")
    assert screen.look(make_ctx("screen")).attachments  # fine by default
    ctx = make_ctx("screen", exclude_apps="Notes")
    assert screen.look(ctx).text == screen.DIDNT_LOOK
    mac["window"] = screen.Window("Pages", "Taxes 2026")
    assert screen.look(make_ctx("screen", exclude_titles=" taxes ,x")).text == screen.DIDNT_LOOK


def test_unreadable_front_window_means_no_picture(make_ctx, mac):
    mac["window"] = None
    result = screen.look(make_ctx("screen"))
    assert "didn't look" in result.text and captured(mac) == []


def test_locked_screen_is_an_error(make_ctx, mac):
    mac["locked"] = True
    with pytest.raises(ActionError, match="locked"):
        screen.look(make_ctx("screen"))
    assert captured(mac) == []


@pytest.mark.parametrize("change", [{"returncode": 1}, {"write": b""}])
def test_missing_permission_names_the_setting_and_leaves_no_file(make_ctx, mac, change):
    mac.update(change)
    ctx = make_ctx("screen")
    with pytest.raises(ActionError, match="Screen Recording"):
        screen.look(ctx)
    assert list(ctx._tmp_dir.iterdir()) == []


def test_live_view_captures_on_each_look_and_checks_every_time(make_ctx, mac):
    ctx = make_ctx("screen")
    started = screen.start_live(ctx)
    assert captured(mac) == []  # starting captures nothing
    assert screen.sample_live(ctx, started.state).attachments
    mac["window"] = screen.Window("1Password", "Vault")
    assert screen.sample_live(ctx, started.state).text == screen.DIDNT_LOOK
    assert len(captured(mac)) == 1


def test_manifest_flags():
    from jarvis.contract.loader import DEFAULT_APPS_DIR
    from jarvis.contract.manifest import load_manifest

    m = load_manifest(DEFAULT_APPS_DIR / "screen" / "manifest.toml")
    look = m.actions[0]
    assert look.untrusted_output and not look.log_args and look.effect == "read"
    assert m.sessions[0].untrusted_output and m.runs_on == "node:screen"


def test_timeout_is_a_plain_error(make_ctx, mac, monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 15)

    monkeypatch.setattr(screen.subprocess, "run", slow)
    with pytest.raises(ActionError, match="in time"):
        screen.look(make_ctx("screen"))

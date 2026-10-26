import subprocess
from types import SimpleNamespace

import pytest

from jarvis.apps.camera import actions as camera
from jarvis.core import actions

CLI = actions.Origin("cli")


@pytest.fixture
def snap(monkeypatch):
    """Replace imagesnap: `behave(path_arg_effects)` decides what the fake process does."""
    calls = []
    state = {"returncode": 0, "stderr": "", "write": b"jpegbytes", "raises": None}

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        if state["raises"]:
            raise state["raises"]
        if state["write"] is not None:
            open(cmd[-1], "wb").write(state["write"])
        return SimpleNamespace(returncode=state["returncode"], stdout="", stderr=state["stderr"])

    monkeypatch.setattr(camera, "_imagesnap", lambda: "/fake/imagesnap")
    monkeypatch.setattr(camera.subprocess, "run", fake_run)
    state["calls"] = calls
    return state


def shoot(reg):
    return actions.run(reg, "camera__take_photo", {}, CLI)


def test_takes_photo_and_release_deletes_it(real_apps, snap):
    out = shoot(real_apps)
    (att,) = out.result.attachments
    assert att.kind == "image" and att.mime == "image/jpeg" and att.path.read_bytes() == b"jpegbytes"
    cmd, kw = snap["calls"][0]
    assert cmd[:4] == ["/fake/imagesnap", "-q", "-w", "1"] and "-d" not in cmd and kw["timeout"] == 10
    actions.release(out)
    assert not att.path.exists()


def test_device_setting_and_no_args_logged(settings, snap):
    from jarvis.core.apps import load_apps
    from jarvis.core.settings import load_settings

    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "settings.toml").write_text('[apps.camera]\ndevice = "FaceTime HD"\n')
    reg = load_apps(load_settings())
    try:
        actions.release(shoot(reg))
        assert snap["calls"][0][0][4:6] == ["-d", "FaceTime HD"]
        assert actions.recent_log(reg)[-1]["args"] == "(not logged)"
    finally:
        reg.close()


@pytest.mark.parametrize("change,fragment", [
    ({"write": b"", "returncode": 0}, "Camera access is blocked"),
    ({"write": b"", "returncode": 1, "stderr": "No video devices found"}, "couldn't find a camera"),
    ({"write": b"", "returncode": 1, "stderr": "Not authorized"}, "Camera access is blocked"),
    ({"raises": subprocess.TimeoutExpired("imagesnap", 10)}, "didn't answer"),
])
def test_errors_are_plain_and_leave_no_file(real_apps, snap, change, fragment):
    snap.update(change)
    out = shoot(real_apps)
    assert isinstance(out, actions.Failed) and fragment in out.message
    assert list((real_apps.settings.data_dir / "tmp").iterdir()) == []


def test_imagesnap_missing(real_apps, monkeypatch):
    monkeypatch.setattr(camera, "_imagesnap", lambda: None)
    out = shoot(real_apps)
    assert isinstance(out, actions.Failed) and "brew install imagesnap" in out.message

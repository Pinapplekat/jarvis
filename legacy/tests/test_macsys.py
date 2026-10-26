import subprocess

import pytest

from jarvis import config, macsys


class Run:
    def __init__(self, out="40"):
        self.calls, self.out = [], out

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, self.out, "")


def test_volume_is_clamped_and_set():
    run = Run()
    assert macsys.control("volume", 140, run=run) == "Mac volume at 100%."
    assert macsys.control("volume_up", None, run=run) == "Mac volume at 50%."
    assert "set volume output volume 50" in run.calls[-1][-1]


def test_dark_mode_is_off_without_the_switch(monkeypatch):
    monkeypatch.setattr(config, "MAC_SYSTEM_EVENTS", False)
    run = Run()
    with pytest.raises(ValueError, match="MAC_SYSTEM_EVENTS"):
        macsys.control("dark_mode", "on", run=run)
    assert run.calls == []  # never even asked System Events


def test_unknown_ops_are_refused():
    with pytest.raises(ValueError):
        macsys.control("shutdown", run=Run())

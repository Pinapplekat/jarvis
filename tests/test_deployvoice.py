import time

import pytest

from jarvis import config, deployvoice, selfedit
from jarvis.memory import Memory


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(selfedit, "enabled", lambda: True)
    m = Memory(":memory:")
    edit = m.add_self_edit("request", "Barge-in tuning", "make barge-in adapt")
    m.db.execute("UPDATE self_edits SET status = 'ready' WHERE id = ?", (edit,))
    m.db.commit()
    return m, edit


def test_deploy_needs_an_offer_then_explicit_words(ready):
    m, edit = ready
    spawned = []
    assert deployvoice.answer(m, "yes, deploy it", spawn=lambda *a: spawned.append(a)) is None  # no offer yet
    assert "Say \"yes, deploy it\"" in deployvoice.offer(m)
    assert deployvoice.answer(m, "yes, deploy it", spawn=lambda *a: spawned.append(a)).startswith("Deploying")
    assert spawned == [("deploy", edit)]


@pytest.mark.parametrize("words", ["yes", "sure", "what's due tomorrow", "deploy", "no"])
def test_anything_but_a_clear_yes_deploy_withdraws_the_offer(ready, words):
    m, _ = ready
    spawned = []
    deployvoice.offer(m)
    assert deployvoice.answer(m, words, spawn=lambda *a: spawned.append(a)) is None
    assert deployvoice.answer(m, "yes, deploy it", spawn=lambda *a: spawned.append(a)) is None  # offer gone
    assert spawned == []


def test_offers_lapse(ready, monkeypatch):
    m, _ = ready
    deployvoice.offer(m)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + deployvoice.OFFER_SECONDS + 5)
    assert deployvoice.answer(m, "yes, deploy it", spawn=lambda *a: None) is None


def test_paused_self_edit_refuses(monkeypatch):
    monkeypatch.setattr(config, "JARVIS_REPO", "")
    with pytest.raises(ValueError, match="paused"):
        deployvoice.offer(Memory(":memory:"))

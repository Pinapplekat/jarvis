"""The browser app end to end: the host's action path, a real WebSocket, a real node runtime. Chromium is replaced by
the scripted fake from the app's own tests, so nothing here opens a browser or the network."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from node_harness import NodeThread, pair, start_hub, wait_for

from jarvis.apps.browser import actions as browser
from jarvis.contract.api import JobRun
from jarvis.contract.loader import DEFAULT_APPS_DIR
from jarvis.core import actions
from jarvis.core.apps import load_apps
from jarvis.core.settings import load_settings

CLI = actions.Origin("cli")
_spec = importlib.util.spec_from_file_location("browser_fakes", DEFAULT_APPS_DIR / "browser" / "tests" / "browser_fakes.py")
fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fakes)

FORM = fakes.Site(title="Contact", controls=[fakes.ctl(0, "text", "Your name"), fakes.ctl(1, "button", "Send")])


@pytest.fixture
def link(data_dir, tmp_path, monkeypatch):
    driver = fakes.FakeDriver({"https://example.com/": fakes.Site(title="Hello"), "https://example.com/form": FORM,
                              "https://example.com/next": fakes.Site(title="Next page")})
    monkeypatch.setattr(browser, "make_driver", lambda: driver)
    monkeypatch.setattr(browser, "resolve", fakes.resolver({"canvas.seattlecolleges.edu": fakes.PUBLIC}))
    monkeypatch.setattr(browser, "_manager", None)
    data_dir.mkdir(parents=True)
    reg = load_apps(load_settings())
    hub = start_hub(reg)
    node = NodeThread(tmp_path, hub.port, pair(reg), apps_dir=DEFAULT_APPS_DIR).start()
    node.wait_connected()
    wait_for(hub.connected, what="the hub to see the node")
    yield SimpleNamespace(reg=reg, driver=driver, node_dir=node.config.node_dir, node_runtime=node.runtime)
    node.runtime.close()
    node.stop()
    hub.stop()
    reg.close()


def test_read_comes_back_with_the_page_and_a_picture(link):
    out = actions.run(link.reg, "browser__read", {"url": "https://example.com/", "screenshot": True}, CLI)
    assert isinstance(out, actions.Ran) and out.result.text.startswith("Hello\nhttps://example.com/")
    (att,) = out.result.attachments
    assert att.mime == "image/jpeg" and att.path.read_bytes().startswith(b"\xff\xd8")
    actions.release(out)
    assert not att.path.exists()  # a picture reaches the model and is then deleted


def test_act_asks_with_the_exact_steps_and_a_no_runs_nothing(link):
    args = {"url": "https://example.com/form", "steps": ['fill "Your name" with "Elijah"', 'press "Send"'], "why": "test it"}
    ask = actions.run(link.reg, "browser__act", args, CLI)
    assert isinstance(ask, actions.Asked)
    assert ask.question == ('On https://example.com/form: fill "Your name" with "Elijah"; press "Send". '
                            "It's to test it. Go?")
    assert isinstance(actions.answer_ask(link.reg, ask.ask_id, False), actions.Declined)
    assert link.driver.ran == [] and link.driver.launches == []  # no browser was even started
    ask = actions.run(link.reg, "browser__act", args, CLI)
    done = actions.answer_ask(link.reg, ask.ask_id, True)
    assert isinstance(done, actions.Ran) and link.driver.ran == [args["steps"]]


def test_a_step_that_doesnt_match_fails_plainly_through_the_whole_path(link):
    ask = actions.run(link.reg, "browser__act", {"url": "https://example.com/form", "steps": ['press "Nope"'], "why": "x"}, CLI)
    out = actions.answer_ask(link.reg, ask.ask_id, True)
    assert isinstance(out, actions.Failed) and "Nothing ran" in out.message and link.driver.ran == []


def test_the_browse_session_over_the_link(link):
    started = actions.run(link.reg, "browser__browse_start", {"url": "https://example.com/"}, CLI)
    assert isinstance(started, actions.Ran) and "Opened browser browse #" in started.result.text
    sid = started.result.data["session_id"]
    followed = actions.run(link.reg, "browser__follow", {"link_text": "Next"}, CLI)
    assert isinstance(followed, actions.Ran) and "Next page" in followed.result.text
    stopped = actions.run(link.reg, "browser__browse_stop", {"session_id": sid}, CLI)
    assert isinstance(stopped, actions.Ran) and link.driver.sessions[0].pages[0].closed


def test_school_reads_canvas_through_the_node_and_the_signed_in_profile(link):
    (link.node_dir / "browser").mkdir(parents=True, exist_ok=True)
    (link.node_dir / "browser" / "sites.json").write_text(json.dumps(["canvas.seattlecolleges.edu"]))
    due = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = "https://canvas.seattlecolleges.edu/api/v1"
    for url, body in [
        (f"{base}/courses?per_page=100&enrollment_state=active", [{"id": 7, "course_code": "ECON 100"}]),
        (f"{base}/courses/7/assignments?per_page=100&include%5B%5D=submission",
         [{"id": 1, "name": "Quiz", "due_at": due, "html_url": "u", "submission_types": ["online_quiz"],
           "submission": {"workflow_state": "submitted", "missing": False}}]),
        (f"{base}/courses/7/modules?per_page=100", []),
    ]:
        link.driver.json[url] = (200, body, None, url)
    link.driver.json_fallback = lambda u: (200, [], None, u)  # the announcements: none
    school = link.reg.apps["school"]
    school.ctx.secrets = {}
    out = school.module.sync(school.ctx, JobRun(datetime.now(timezone.utc), None, "every", False))
    assert out.summary.startswith("1 deadlines, 1 new") and out.messages == ()
    row = link.reg.conn.execute("SELECT status, trackable, source FROM school_deadlines WHERE key='canvas:1'").fetchone()
    assert tuple(row) == ("submitted", 1, "canvas")
    assert link.driver.launches == [(True, False)]  # the profile, headless: Canvas is on the list


def test_the_node_closes_the_browser_when_it_shuts_down(link, tmp_path):
    actions.run(link.reg, "browser__read", {"url": "https://example.com/"}, CLI)
    assert link.driver.sessions[0].open
    link.node_runtime.close()
    assert not link.driver.sessions[0].open

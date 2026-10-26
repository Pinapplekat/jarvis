import json
from datetime import datetime, timedelta, timezone

import pytest

from jarvis import terminal
from jarvis.memory import Memory
from jarvis.repos import check_git, project_for
from jarvis.screen import Window, excluded, meaningfully_changed, redact
from jarvis.watch import Watcher, recent_activity

REPOS = ["/Users/e/Code/shop-site", "/Users/e/Code/JARVIS", "/Users/e/Code/JARVIS/vendor/lib"]


def test_redact_secrets():
    assert redact("export OPENAI_API_KEY=sk-abc123def456") == "export OPENAI_API_KEY=<redacted>"
    assert redact('password: "hunter22"') == "password=<redacted>"
    assert redact("TELEGRAM_BOT_TOKEN=1234567890:FAKEfakeFAKEfake123") == "TELEGRAM_BOT_TOKEN=<redacted>"
    assert "<redacted>" in redact("key ghp_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6")
    assert redact('export FAL_KEY="fal_sk_x:y"') == "export FAL_KEY=<redacted>"
    assert redact("STRIPE_KEY=abcd") == "STRIPE_KEY=<redacted>"
    assert redact("the keyboard is fine") == "the keyboard is fine"
    assert redact("git commit -m 'fix login page'") == "git commit -m 'fix login page'"
    assert redact("/Users/elijahryerson/Library/Mobile Documents") == "/Users/elijahryerson/Library/Mobile Documents"


def test_exclusions():
    assert excluded("Messages", "Chat with Mom")
    assert excluded("1Password", None)
    assert excluded("Google Chrome", "Chase Online - Account Summary")
    assert excluded("Safari", "Sign in to your account")
    assert not excluded("Discord", "#class-discussion")
    assert not excluded("Code", "app.py — shop-site")


def test_change_detection():
    assert meaningfully_changed(None, "hello")
    assert not meaningfully_changed("same text", "same text")
    base = "line\n" * 100
    assert not meaningfully_changed(base, base + "one more")
    assert meaningfully_changed(base, "totally different content " * 20)


def test_project_for():
    assert project_for(REPOS, cwd="/Users/e/Code/shop-site/src") == "shop-site"
    assert project_for(REPOS, cwd="/Users/e/Code/JARVIS/vendor/lib/x") == "lib"  # deepest repo wins
    assert project_for(REPOS, text="checkout.ts — shop-site — Visual Studio Code") == "shop-site"
    assert project_for(REPOS, cwd="/tmp") is None


class Clock:
    t = 1000.0


def make_watcher(mem, windows, texts, idle=5):
    w = Watcher(mem, log=lambda s: None, front=lambda: windows[0],
                capture=lambda win: texts[0], idle=lambda: idle)
    return w


def test_watcher_saves_changes_and_skips_idle_excluded_and_repeats():
    m = Memory(":memory:")
    m.set_kv("repos", json.dumps(REPOS))
    windows, texts = [Window(1, "Code", "app.py — shop-site")], ["def checkout(): ..."]
    w = make_watcher(m, windows, texts)
    assert w.look(1000).startswith("saved Code")
    assert w.look(1010) == "same window"
    assert w.look(1100) == "unchanged"
    texts[0] = "completely new code " * 30
    assert w.look(1200).startswith("saved")
    (first, second) = m.observations(datetime.now(timezone.utc) - timedelta(minutes=5))
    assert first["project"] == "shop-site" and first["source"] == "screen"

    windows[0] = Window(2, "Google Chrome", "Wells Fargo - Sign On")
    assert w.look(1300) == "excluded window title"
    assert len(m.observations(datetime.now(timezone.utc) - timedelta(minutes=5))) == 2

    idle_w = make_watcher(m, windows, texts, idle=600)
    assert idle_w.look(1400) == "idle"


def test_terminal_ingest(tmp_path):
    m = Memory(":memory:")
    m.set_kv("repos", json.dumps(REPOS))
    log = tmp_path / "terminal.log"
    log.write_text(
        "1760572800\t0\t/Users/e/Code/shop-site\t1.2\tnpm test\n"
        "1760572900\t1\t/Users/e/Code/shop-site\t0.4\texport API_KEY=abcd1234efgh\n"
        "1760573000\t0\t/tmp\t0.0\tpartial line no newline"
    )
    assert terminal.ingest(m, log) == 2
    obs = m.observations(datetime(2020, 1, 1, tzinfo=timezone.utc), source="terminal")
    assert obs[0]["text"] == "$ npm test  [ok, 1.2s]" and obs[0]["project"] == "shop-site"
    assert "API_KEY=<redacted>" in obs[1]["text"] and "exit 1" in obs[1]["text"]
    assert terminal.ingest(m, log) == 0
    with log.open("a") as f:
        f.write("\n")
    assert terminal.ingest(m, log) == 1


def test_install_hook_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "HOOK", tmp_path / "hook.zsh")
    monkeypatch.setattr(terminal, "SOURCE_LINE", f'source "{tmp_path / "hook.zsh"}"  # JARVIS terminal sense')
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text("export PATH=$PATH:/x")
    assert terminal.install_hook(zshrc).startswith("added")
    assert terminal.install_hook(zshrc) == "already installed"
    assert zshrc.read_text().count("JARVIS terminal sense") == 1
    assert "add-zsh-hook precmd" in (tmp_path / "hook.zsh").read_text()
    assert terminal.hook_installed(zshrc)


def test_git_events_after_baseline():
    m = Memory(":memory:")
    m.set_kv("repos", json.dumps(REPOS[:2]))
    states = {REPOS[0]: {"branch": "main", "sha": "a", "subject": "init"},
              REPOS[1]: {"branch": "main", "sha": "b", "subject": "x"}}
    assert check_git(m, state_of=lambda r: dict(states[r])) == 0  # baseline
    states[REPOS[0]] = {"branch": "checkout", "sha": "c", "subject": "Add cart"}
    assert check_git(m, state_of=lambda r: dict(states[r])) == 2
    texts = [o["text"] for o in m.observations(datetime(2020, 1, 1, tzinfo=timezone.utc), source="git")]
    assert texts == ["switched to branch checkout", "new commit on checkout: Add cart"]
    assert check_git(m, state_of=lambda r: None) == 0  # unreadable: keep old state, no events
    assert check_git(m, state_of=lambda r: dict(states[r])) == 0


def test_recent_activity_summary():
    m = Memory(":memory:")
    m.observe("screen", "Traceback: ImportError cart", app="Code", title="cart.py — shop-site", project="shop-site")
    m.observe("terminal", "$ pytest  [exit 1, 2.0s]", project="shop-site")
    out = recent_activity(m, 30)
    assert "Code | cart.py — shop-site [shop-site]" in out and "pytest  [exit 1" in out
    assert "untrusted data" in out and "ImportError cart" in out
    assert recent_activity(Memory(":memory:"), 30) == "(nothing observed recently)"


def test_prune():
    m = Memory(":memory:")
    m.observe("screen", "old", at=datetime.now(timezone.utc) - timedelta(days=10))
    m.observe("screen", "new")
    assert m.prune_observations(timedelta(days=7)) == 1


# ----- watches: things Elijah asked JARVIS to keep an eye on -----
from zoneinfo import ZoneInfo

from jarvis import config, location, watches
from jarvis.canvas import Deadline
from jarvis.mailrelay import InboxMessage
from jarvis.web import Page

TZ = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def wm(monkeypatch):
    for name, value in (("MAIL_WATCH", True), ("MAIL_ACCOUNT", "me@school.edu"), ("WATCH", True),
                        ("LOCATION", True), ("SIRI", True), ("CANVAS_ICS_URL", "https://feed"), ("CANVAS_TOKEN", ""),
                        ("WATCHES_MAX_ACTIVE", 20)):
        monkeypatch.setattr(config, name, value)
    return Memory(":memory:")


def now_local():
    return datetime.now(TZ)


def alerts(m):
    return [r["message"] for r in m.db.execute("SELECT message FROM alerts ORDER BY id")]


def run(m, at, judge=None, **src):
    return watches.run_due(m, at, watches.Sources(**src), judge=judge)


def snapshot(m, msgs):
    m.set_kv("inbox_snapshot", json.dumps({"at": datetime.now(timezone.utc).isoformat(), "messages": [
        {"id": i, "sender": s, "subject": subj, "received": rec.isoformat(), "read": False} for i, s, subj, rec in msgs]}))


def test_email_watch_fires_once_on_a_new_matching_message(wm):
    t = now_local()
    snapshot(wm, [("1", "Harvard Admissions <adm@harvard.edu>", "Old note", t - timedelta(days=2))])
    wid = watches.create(wm, "Harvard letter", "email", {"sender": "harvard"}, t)
    assert run(wm, t) == "checked 1, triggered 0"  # mail from before the watch isn't news
    snapshot(wm, [("2", "Harvard Admissions <adm@harvard.edu>", "Your decision", t + timedelta(minutes=3)),
                  ("3", "Newsletter <n@x.com>", "Sale", t + timedelta(minutes=3))])
    assert "triggered 1" in run(wm, t + timedelta(minutes=5))
    assert alerts(wm) == ["👁 Harvard letter: new email from Harvard Admissions: Your decision"]
    assert watches.get(wm, wid)["status"] == "fired"
    assert run(wm, t + timedelta(minutes=10)) == "checked 0, triggered 0"  # one-shot: never repeats
    assert len(alerts(wm)) == 1


def test_email_watch_needs_a_filter_and_validates(wm):
    with pytest.raises(ValueError, match="sender or a keyword"):
        watches.create(wm, "x", "email", {}, now_local())
    with pytest.raises(ValueError, match="unknown watch type"):
        watches.create(wm, "x", "carrier pigeon", {}, now_local())
    with pytest.raises(ValueError, match="short name"):
        watches.create(wm, "", "email", {"sender": "a"}, now_local())


def test_watch_cap(wm, monkeypatch):
    monkeypatch.setattr(config, "WATCHES_MAX_ACTIVE", 2)
    for i in range(2):
        watches.create(wm, f"w{i}", "battery", {"below": 20}, now_local())
    with pytest.raises(ValueError, match="cancel one first"):
        watches.create(wm, "w3", "battery", {"below": 20}, now_local())


def _place(m, name, lat, lon):
    cur = m.db.execute("INSERT INTO places (lat, lon, created_at, name) VALUES (?, ?, ?, ?)",
                       (lat, lon, datetime.now(timezone.utc).isoformat(), name))
    m.db.commit()
    return cur.lastrowid


def test_location_watch_leave_and_enter(wm):
    _place(wm, "school", 47.6160, -122.3210)
    _place(wm, "home", 47.6600, -122.3130)
    t = datetime.now(timezone.utc)
    location.record_point(wm, 47.6161, -122.3211, t, source="live")
    leave = watches.create(wm, "left school", "location", {"place": "school", "event": "leave"}, now_local())
    arrive = watches.create(wm, "home", "location", {"place": "Home", "event": "enter"}, now_local())
    assert "triggered 0" in run(wm, now_local())
    location.record_point(wm, 47.6400, -122.3170, t + timedelta(seconds=30), source="live")  # on the way
    assert "triggered 1" in run(wm, now_local() + timedelta(minutes=6))
    assert watches.get(wm, leave)["status"] == "fired" and watches.get(wm, arrive)["status"] == "active"
    location.record_point(wm, 47.6601, -122.3131, t + timedelta(seconds=60), source="live")
    assert "triggered 1" in run(wm, now_local() + timedelta(minutes=12))
    assert alerts(wm) == ["👁 left school: you've left school", "👁 home: you've reached home"]
    with pytest.raises(ValueError, match="no place called 'gym'"):
        watches.create(wm, "gym", "location", {"place": "gym", "event": "enter"}, now_local())


def test_location_route_corridor(wm):
    a = {"lat": 47.6160, "lon": -122.3210}
    b = {"lat": 47.6600, "lon": -122.3130}
    assert watches._near_route(47.6380, -122.3170, a, b)      # halfway along
    assert not watches._near_route(47.6380, -122.2900, a, b)  # 2 km off to the side


def test_location_watch_waits_on_stale_position(wm):
    _place(wm, "school", 47.6160, -122.3210)
    location.record_point(wm, 47.6161, -122.3211, datetime.now(timezone.utc) - timedelta(hours=3), source="live")
    wid = watches.create(wm, "left", "location", {"place": "school", "event": "leave"}, now_local())
    assert watches.get(wm, wid)["last_state"] is None  # no fresh position, no baseline guessed
    run(wm, now_local())
    assert alerts(wm) == []


def test_screen_watch_text_and_change(wm):
    t = now_local() - timedelta(minutes=1)
    wm.observe("screen", "old chatter", app="Discord", title="#arboreal")
    text = watches.create(wm, "Arboreal reply", "screen", {"app": "Discord", "title": "arboreal", "text": "Emily"}, t)
    change = watches.create(wm, "Arboreal change", "screen", {"app": "Discord", "title": "arboreal"}, t)
    assert watches.get(wm, change)["last_state"]  # baseline from what was already on screen
    wm.observe("screen", "Code stuff", app="Code", title="app.py")
    assert "triggered 0" in run(wm, now_local())
    wm.observe("screen", "Emily: are you coming tonight?", app="Discord", title="#arboreal")
    assert "triggered 2" in run(wm, now_local() + timedelta(minutes=6))
    assert "👁 Arboreal reply: 'Emily' showed up in #arboreal" in alerts(wm)
    assert "👁 Arboreal change: #arboreal changed" in alerts(wm)
    assert watches.get(wm, text)["status"] == "fired"


def test_screen_watch_refuses_windows_never_read(wm):
    with pytest.raises(ValueError, match="never read"):
        watches.create(wm, "texts", "screen", {"app": "Messages", "text": "Emily"}, now_local())


def test_web_watch_keyword_and_change(wm):
    pages = {"text": "Ferry on time"}
    fetch = lambda url: Page(url, title="Ferry", text=pages["text"])  # noqa: E731
    kw = watches.create(wm, "ferry", "web", {"url": "https://wsdot.example/ferry", "keyword": "cancelled"}, now_local())
    ch = watches.create(wm, "page", "web", {"url": "https://wsdot.example/ferry"}, now_local())
    assert watches.get(wm, kw)["interval_min"] == 60
    assert "triggered 0" in run(wm, now_local(), fetch_page=fetch)  # first look is the baseline
    pages["text"] = "Ferry CANCELLED today"
    assert "checked 0" in run(wm, now_local() + timedelta(minutes=20), fetch_page=fetch)  # not due yet
    assert "triggered 2" in run(wm, now_local() + timedelta(minutes=61), fetch_page=fetch)
    assert alerts(wm) == ["👁 ferry: 'cancelled' is on Ferry", "👁 page: Ferry changed"]
    with pytest.raises(ValueError, match="banking or payment"):
        watches.create(wm, "bank", "web", {"url": "https://www.chase.com/x"}, now_local())


def test_web_watch_load_error_is_not_a_change(wm):
    watches.create(wm, "page", "web", {"url": "https://x.example"}, now_local())
    run(wm, now_local(), fetch_page=lambda u: Page(u, text="a"))
    run(wm, now_local() + timedelta(hours=2), fetch_page=lambda u: Page(u, error="timeout"))
    assert alerts(wm) == []


def test_canvas_watch_new_items_and_graded_needs_token(wm):
    due = datetime.now(TZ) + timedelta(days=3)
    items = [Deadline("canvas:1", "ECON 100", "Module 3", due, None, "unknown", False)]
    wid = watches.create(wm, "econ", "canvas", {"event": "new", "course": "econ"}, now_local())
    assert "triggered 0" in run(wm, now_local(), deadlines=lambda: list(items))
    assert "triggered 0" in run(wm, now_local() + timedelta(minutes=31), deadlines=lambda: [])  # sync failed
    items.append(Deadline("canvas:2", "ECON 100", "Module 4 quiz", due, None, "unknown", False))
    items.append(Deadline("canvas:3", "ENGL 101", "Essay", due, None, "unknown", False))
    assert "triggered 1" in run(wm, now_local() + timedelta(minutes=62), deadlines=lambda: list(items))
    assert alerts(wm) == ["👁 econ: New on Canvas: ECON 100: Module 4 quiz"]
    assert watches.get(wm, wid)["status"] == "fired"
    with pytest.raises(ValueError, match="token"):
        watches.create(wm, "grades", "canvas", {"event": "graded"}, now_local())


def test_time_and_battery_watches(wm):
    t = now_local()
    watches.create(wm, "leave for ferry", "time", {"at": f"{t + timedelta(minutes=10):%Y-%m-%d %H:%M}"}, t)
    batt = watches.create(wm, "battery", "battery", {"below": 20}, t, recurring=True)
    level = {"v": (50, False)}
    assert "triggered 0" in run(wm, t, battery=lambda: level["v"])
    level["v"] = (15, False)
    assert "triggered 2" in run(wm, t + timedelta(minutes=12), battery=lambda: level["v"])
    assert "triggered 0" in run(wm, t + timedelta(minutes=24), battery=lambda: level["v"])  # still low: once
    level["v"] = (60, True)
    run(wm, t + timedelta(minutes=36), battery=lambda: level["v"])
    level["v"] = (12, False)
    assert "triggered 1" in run(wm, t + timedelta(minutes=48), battery=lambda: level["v"])  # recurring re-arms
    assert watches.get(wm, batt)["status"] == "active"
    assert alerts(wm)[0] == "👁 leave for ferry: it's time"
    assert alerts(wm).count("👁 battery: battery at 15% (on battery)") == 1


def test_battery_reader():
    class R:
        returncode, stdout = 0, "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t42%; discharging; 3:10 remaining"
    from jarvis.macsys import battery
    assert battery(run=lambda *a, **k: R()) == (42, False)
    R.stdout = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining"
    assert battery(run=lambda *a, **k: R()) == (100, True)


def test_between_window(wm):
    assert watches._in_window("9:00-15:30", datetime(2026, 10, 20, 10, 0, tzinfo=TZ))
    assert not watches._in_window("09:00-15:30", datetime(2026, 10, 20, 16, 0, tzinfo=TZ))
    assert watches._in_window("22:00-06:00", datetime(2026, 10, 20, 23, 0, tzinfo=TZ))


def test_expiry_mentions_once_and_cancel(wm):
    t = now_local()
    watches.create(wm, "Harvard letter", "email", {"sender": "harvard"}, t, expires=f"{t + timedelta(hours=1):%Y-%m-%d %H:%M}")
    other = watches.create(wm, "ferry page", "web", {"url": "https://x.example"}, t)
    assert "expired 1" in run(wm, t + timedelta(hours=2), fetch_page=lambda u: Page(u, text="a"))
    assert "expired" not in run(wm, t + timedelta(hours=3), fetch_page=lambda u: Page(u, text="a"))
    assert alerts(wm) == ['👁 The watch "Harvard letter" ran out without anything happening, sir.']
    with pytest.raises(ValueError, match="no running watch"):
        watches.cancel(wm, "harvard")
    assert watches.cancel(wm, "ferry") == ["ferry page"]
    assert watches.get(wm, other)["status"] == "cancelled"
    assert watches.list_text(wm, t) == "No watches running."


def test_cancel_ambiguous_names_ask(wm):
    watches.create(wm, "ferry page", "battery", {"below": 5}, now_local())
    wid = watches.create(wm, "ferry text", "battery", {"below": 6}, now_local())
    with pytest.raises(ValueError, match="several"):
        watches.cancel(wm, "ferry")
    assert watches.cancel(wm, str(wid)) == ["ferry text"]


class SpyJudge:
    def __init__(self, yes):
        self.calls, self.yes = [], yes

    def __call__(self, state, questions):
        from jarvis.judge import Answer
        self.calls.append(state)
        return {"hit": Answer(self.yes, 0.9)}


def test_cheap_gate_never_calls_the_model_without_a_match(wm):
    pages = {"text": "nothing yet"}
    watches.create(wm, "tickets", "web", {"url": "https://t.example", "keyword": "tickets",
                                          "ask": "Are tickets actually on sale now?"}, now_local())
    judge = SpyJudge(yes=False)
    for i in range(3):
        watches.run_due(wm, now_local() + timedelta(hours=i * 2), watches.Sources(fetch_page=lambda u: Page(u, text=pages["text"])), judge=judge)
    assert judge.calls == []  # filter never matched: no model call
    pages["text"] = "tickets: sold out, sorry"
    watches.run_due(wm, now_local() + timedelta(hours=8), watches.Sources(fetch_page=lambda u: Page(u, text=pages["text"])), judge=judge)
    assert len(judge.calls) == 1 and "untrusted" in judge.calls[0].lower()
    assert alerts(wm) == []  # the judge said no
    assert not watches.gate({"name": "x"}, {}, watches.Result(False), SpyJudge(True))[0]


def test_gate_fires_when_judge_agrees(wm):
    p = {"ask": "Is this the admission decision?"}
    judge = SpyJudge(yes=True)
    assert watches.gate({"name": "x"}, p, watches.Result(True, "new email", detail="Subject: decision"), judge) == (True, "")
    assert watches.gate({"name": "x"}, {}, watches.Result(True, "s"), judge)[0] and len(judge.calls) == 1


def test_follow_up_text_is_only_queued_for_his_yes(wm, monkeypatch):
    from jarvis import siri
    sent = []
    monkeypatch.setattr(siri, "send_text", lambda *a, **k: sent.append(a), raising=False)
    wm.remember("personal", "emily_number", "206-555-0100")
    t = now_local()
    snapshot(wm, [])
    watches.create(wm, "Harvard letter", "email", {"sender": "harvard"}, t,
                   follow_up={"to": "Emily", "body": "Harvard wrote back!"})
    snapshot(wm, [("9", "adm@harvard.edu", "Decision", t + timedelta(minutes=1))])
    run(wm, t + timedelta(minutes=5))
    pending = wm.pending_actions(kind="send_text")
    assert len(pending) == 1 and json.loads(pending[0]["payload"])["body"] == "Harvard wrote back!"
    assert "waiting for your yes" in alerts(wm)[0]
    assert sent == []  # nothing goes out without his yes
    with pytest.raises(ValueError, match="asked for it himself"):
        watches.create(wm, "x", "battery", {"below": 5}, t, follow_up={"to": "Emily", "body": "hi"}, trusted=False)


def test_chat_actions_create_list_cancel(wm):
    from jarvis.chat import apply_actions
    said = []
    problems = apply_actions(wm, [{"type": "watch_create", "name": "Harvard letter", "kind": "email",
                                   "params": {"sender": "harvard"}},
                                  {"type": "watch_list"}], TZ, said=said, channel="telegram")
    assert problems == [] and "Harvard letter: new email from harvard" in said[0]
    problems = apply_actions(wm, [{"type": "watch_create", "name": "bad", "kind": "email", "params": {}}], TZ)
    assert problems and "sender or a keyword" in problems[0]
    problems = apply_actions(wm, [{"type": "watch_create", "name": "t", "kind": "battery", "params": {"below": 5},
                                   "follow_up": {"to": "Emily", "body": "hi"}}], TZ, channel=None)
    assert problems and "asked for it himself" in problems[0]
    said = []
    assert apply_actions(wm, [{"type": "watch_cancel", "name": "harvard"}], TZ, said=said) == []
    assert said == ["Stopped watching: Harvard letter."]


def test_watches_in_the_prompt(wm):
    from jarvis.chat import build_prompt
    watches.create(wm, "Harvard letter", "email", {"sender": "harvard"}, now_local())
    prompt = build_prompt(wm, "hi", now_local(), "(none)")
    assert "<watches" in prompt and "Harvard letter: new email from harvard" in prompt

import json
import re
import shutil
import stat
import subprocess
import time
from datetime import datetime, timezone

import pytest

from jarvis import inbox
from jarvis.approvals import _handle, notify_pending
from jarvis.memory import Memory
from jarvis.telegram import Update
from tests.test_approvals import FakeTelegram, _draft


def messages():
    return inbox.read()["messages"]


def test_everything_sent_to_telegram_is_mirrored_with_its_buttons():
    tg = inbox.Mirror(FakeTelegram())
    tg.send("Hello, sir.")
    mid = tg.send("Run it?", buttons=[("Run ▶️", "cmd_run:4"), ("Reject ✖️", "cmd_reject:4")])
    assert [m["text"] for m in messages()] == ["Hello, sir.", "Run it?"]
    assert messages()[1]["buttons"] == [["Run ▶️", "cmd_run:4"], ["Reject ✖️", "cmd_reject:4"]]
    assert inbox.read()["waiting"] == 1
    tg.edit(mid, "Run it?\n\n— Ran OK")  # pressed in Telegram: the Mac's copy follows
    assert messages()[1]["buttons"] == [] and messages()[1]["text"].endswith("Ran OK") and inbox.read()["waiting"] == 0


def test_only_real_open_buttons_can_be_pressed():
    lid = inbox.record("jarvis", "Build it?", [("Yes, build it ✅", "plan_yes:3"), ("Not now", "plan_no:3")])
    assert inbox.open_button(lid, "plan_yes:3")["text"] == "Build it?"
    assert inbox.open_button(lid, "se_deploy:3") is None  # not a button on that message
    inbox.resolve(lid, note="Building it")
    assert inbox.open_button(lid, "plan_yes:3") is None and messages()[0]["note"] == "Building it"


def test_approving_from_the_mac_goes_through_the_same_code_and_updates_telegram():
    m, real = Memory(":memory:"), FakeTelegram()
    tg = inbox.Mirror(real)
    aid = _draft(m)
    notify_pending(m, tg)
    [ask] = messages()
    mac = inbox.MacChat(tg, local_id=ask["id"])
    u = Update(update_id=0, sender_id=0, sent_at=datetime.now(timezone.utc), callback_id="inbox",
               callback_data=f"approve:{aid}", message_id=ask["tg_id"], message_text=ask["text"])
    assert _handle(m, mac, u)
    assert m.get_action(aid)["status"] == "approved"
    assert real.edits and real.edits[-1][0] == ask["tg_id"]  # Telegram's copy loses its buttons too
    [after] = messages()
    assert after["buttons"] == [] and after["note"].startswith("Approved the email")


def test_replies_to_what_he_types_on_the_mac_stay_on_the_mac_but_questions_reach_telegram():
    real = FakeTelegram()
    mac = inbox.MacChat(inbox.Mirror(real))
    mac.send("Here you are, sir.")
    mac.send("Run it?", buttons=[("Run ▶️", "cmd_run:1")])
    assert [t for t, _ in real.sent] == ["Run it?"]
    assert [(m["text"], m["via"]) for m in messages()] == [("Here you are, sir.", "Mac"), ("Run it?", "")]


def test_requests_are_taken_once_and_old_ones_dropped():
    inbox.REQUESTS.mkdir(parents=True)
    (inbox.REQUESTS / "1.json").write_text(json.dumps({"type": "text", "text": "hi", "at": time.time()}))
    (inbox.REQUESTS / "2.json").write_text(json.dumps({"type": "text", "text": "old", "at": time.time() - 900}))
    (inbox.REQUESTS / "3.json").write_text("not json")
    assert [r["text"] for r in inbox.take_requests()] == ["hi"] and inbox.take_requests() == []


def test_seed_starts_with_the_recent_conversation_without_prefixes():
    m = Memory(":memory:")
    m.add_chat("user", "(typed in the HUD on his Mac) what's due?")
    m.add_chat("assistant", "Two things, sir.")
    assert inbox.seed(m) == 2 and inbox.seed(m) == 0
    assert [(x["who"], x["text"], x["via"]) for x in messages()] == [("you", "what's due?", "HUD"), ("jarvis", "Two things, sir.", "")]


def test_unanswered_buttons_lapse():
    lid = inbox.record("jarvis", "Deploy it?", [("Deploy ✅", "se_deploy:1")])
    with inbox._editing() as data:
        data["messages"][0]["at"] -= inbox.STALE_AFTER + 1
    assert inbox.open_button(lid, "se_deploy:1") is None and inbox.read()["waiting"] == 0


# ----- the step trace -----
TID = "0123456789abcdef0123456789abcdef"


def test_steps_show_live_on_the_turns_reply_and_telegram_gets_none():
    real = FakeTelegram()
    tg = inbox.Mirror(real)
    with inbox.turn() as tid:
        inbox.record_step(tid, 1, "claude", "model_1", "running", 0, "Thinking…")
        [m] = messages()
        assert m["text"] == "" and m["streaming"] and m["steps"][0]["status"] == "running"  # live, before any reply
        inbox.record_step(tid, 1, "claude", "model_1", "ok", 840, "proposed run_command")
        inbox.record_step(tid, 2, "tool", "run_command", "ok", 120, "two files")
        inbox.record("jarvis", "📷 a chart", reply=False)  # a picture (Mirror.send_photo) isn't the reply
        tg.send("Two files, sir.")
        tg.send("Run it?", buttons=[("Run ▶️", "cmd_run:4")])  # nor is a question
    reply, photo, ask = messages()
    assert reply["text"] == "Two files, sir." and reply["tg_id"] is not None and not reply["streaming"]
    assert [(s["n"], s["kind"], s["status"], s["ms"]) for s in reply["steps"]] == [(1, "claude", "ok", 840), (2, "tool", "ok", 120)]
    assert set(reply["steps"][0]) == {"n", "kind", "action", "status", "ms", "summary"}
    assert "steps" not in photo and "steps" not in ask
    assert [t for t, _ in real.sent] == ["Two files, sir.", "Run it?"]  # no trace in Telegram
    inbox.record_step(tid, 3, "resume", "task", "ok", 0, "tests pass")  # a resume, later, on another thread
    assert [s["n"] for s in messages()[0]["steps"]] == [1, 2, 3]


def test_a_turn_with_no_steps_leaves_no_trace_and_no_turn_records_nothing():
    with inbox.turn() as tid:
        inbox.record_step(tid, 1, "claude", "model_1", "running", 0, "Thinking…")
        with inbox._editing() as data:  # (as if the step never came: an empty placeholder)
            data["messages"][0]["steps"] = []
    assert messages() == []  # nothing recorded for it, so nothing left behind
    inbox.record_step(TID, 1, "tool", "inspect", "ok", 1, "x")  # not this thread's turn, no message: dropped
    assert messages() == []


@pytest.mark.parametrize("bad", ["../feed", "..", "../../.ssh/id_rsa", "ABCDEF0123", "short", "a" * 41, "abc/defgh1",
                                 "abcdefgh.json", "abcdefgh\n", "", None, 12345678])
def test_turn_ids_are_strict(bad):
    assert not inbox.valid_turn_id(bad)
    with pytest.raises(ValueError):
        inbox.read_turn(bad)
    inbox.record_step(bad, 1, "tool", "x", "ok", 0, "x", given="x")
    assert not inbox.TURNS.exists()


def test_the_app_reads_only_well_formed_ids_and_only_turn_files():
    assert inbox.valid_turn_id(TID) and inbox.TURN_ID.pattern == "^[a-z0-9]{8,40}$"
    assert '"^[a-z0-9]{8,40}$"' in inbox.SWIFT
    assert 'inboxDir + "/turns/" + turn + ".json"' in inbox.SWIFT
    reads = re.findall(r"contentsOfFile: ([^,)]+)", inbox.SWIFT) + re.findall(r"contents\(atPath: ([^)]+)\)", inbox.SWIFT)
    assert sorted(reads) == ['feedPath', 'inboxDir + "/turns/" + turn + ".json"']
    # a step request never becomes a request file for `jarvis listen`
    assert inbox.SWIFT.index('"step"') < inbox.SWIFT.index("requestsDir + \"/\" + name")


def test_turn_files_are_private_capped_and_redacted():
    with inbox.turn() as tid:
        inbox.record_step(tid, 1, "tool", "run_command", "ok", 5, "listed", given="$ env",
                          got="ANTHROPIC=sk-ant-abcdefghijklmnopqrstuvwxyz0123\nAPI_KEY=hunter2hunter2\n" + "x" * 30_000)
        inbox.record("jarvis", "Done, sir.")
    assert stat.S_IMODE(inbox.TURNS.stat().st_mode) == 0o700
    path = inbox.TURNS / f"{tid}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    out = inbox.read_turn(tid)["steps"]["1"]["output"]
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz0123" not in out and "hunter2hunter2" not in out
    assert "[truncated:" in out and len(out.encode()) < inbox.STEP_CAP + 100
    assert path.stat().st_size < 2 * inbox.STEP_CAP + 1000  # about 20 KB a step at most
    assert inbox.read_turn(tid)["steps"]["1"]["input"] == "$ env"


def test_turn_files_are_pruned_with_their_messages(monkeypatch):
    monkeypatch.setattr(inbox, "KEEP", 3)
    with inbox.turn() as old:
        inbox.record_step(old, 1, "tool", "inspect", "ok", 1, "read", given="a", got="b")
        inbox.record("jarvis", "First, sir.")
    assert (inbox.TURNS / f"{old}.json").exists()
    for i in range(3):
        inbox.record("you", f"hi {i}")
    assert not (inbox.TURNS / f"{old}.json").exists()
    assert all(m.get("turn") != old for m in messages())


NODE_HARNESS = r"""
var made = [], html = [];
function El(tag) {
  this.tagName = tag; this.children = []; this.className = ''; this.dataset = {}; this.style = {};
  this._text = ''; made.push(this);
}
El.prototype.appendChild = function (c) { this.children.push(c); return c; };
El.prototype.addEventListener = function () {};
El.prototype.querySelector = function () { return null; };
El.prototype.querySelectorAll = function () { return []; };
El.prototype.scrollIntoView = function () {};
El.prototype.focus = function () {};
Object.defineProperty(El.prototype, 'textContent', {
  get: function () { return this._text + this.children.map(function (c) { return c.textContent; }).join(''); },
  set: function (v) { this._text = String(v); this.children = []; }});
['innerHTML', 'outerHTML'].forEach(function (p) {
  Object.defineProperty(El.prototype, p, {set: function (v) { html.push(v); }, get: function () { return ''; }}); });
El.prototype.insertAdjacentHTML = function (w, v) { html.push(v); };
var byId = {};
var document = {
  createElement: function (t) { return new El(t); },
  getElementById: function (id) { return byId[id] || (byId[id] = new El('div')); },
  write: function (v) { html.push(v); }};
var posted = [];
var webkit = {messageHandlers: {jarvis: {postMessage: function (m) { posted.push(m); }}}};
function requestAnimationFrame() {} function setInterval() {}
SCRIPT
var evil = '<img src=x onerror=alert(1)><script>alert(2)</script>';
render({messages: [{id: 1, who: 'jarvis', text: 'Done, sir.', at: 0, via: '', buttons: [], note: '', turn: 'TID',
  steps: [{n: 1, kind: 'tool', action: evil, status: 'ok', ms: 5, summary: evil}]}]});
var rows = made.filter(function (e) { return e.className === 'row'; });
rows[0].onclick();
stepDetail('TID', JSON.stringify({steps: {'1': {input: evil, output: evil}}}));
var pres = made.filter(function (e) { return e.tagName === 'pre'; });
console.log(JSON.stringify({html: html, tags: made.map(function (e) { return e.tagName; }),
  row: rows[0].textContent, pres: pres.map(function (e) { return e.textContent; }), posted: posted}));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page's script")
def test_a_step_containing_html_or_script_renders_inert(tmp_path):
    script = inbox.PAGE.split("<script>")[1].split("</script>")[0]
    js = tmp_path / "page.js"
    js.write_text(NODE_HARNESS.replace("SCRIPT", script).replace("TID", TID))
    out = json.loads(subprocess.run([shutil.which("node"), str(js)], capture_output=True, text=True,
                                    check=True).stdout)
    evil = "<img src=x onerror=alert(1)><script>alert(2)</script>"
    assert out["html"] == []  # nothing ever went in as markup
    assert "script" not in out["tags"] and "img" not in out["tags"]
    assert evil in out["row"] and out["pres"] == [evil, evil]  # shown as plain text
    assert out["posted"] == [{"type": "step", "turn": TID}]


def test_the_page_never_uses_markup_apis():
    for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert api not in inbox.PAGE


def test_a_reply_typed_for_on_the_mac_streams_into_one_message_then_settles(monkeypatch):
    monkeypatch.setattr(inbox, "STREAM_EVERY", 0)
    mac = inbox.MacChat(inbox.Mirror(FakeTelegram()))
    mac.stream("Two")
    mac.stream("Two things due")
    [m] = messages()
    assert m["text"] == "Two things due" and m["streaming"]
    mac.finish("Two things due today, sir.")
    [m] = messages()
    assert m["text"] == "Two things due today, sir." and not m["streaming"]

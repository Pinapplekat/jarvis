import subprocess
from types import SimpleNamespace

import pytest

from jarvis.apps.messages import actions as messages
from jarvis.contract.api import ActionError

FS, RS = messages.FIELD, messages.RECORD
CONTACTS = {"Mom": [("Mom", "(206) 555-0100")], "Julian": [("Julian Linton", "206‑555‑0199"),
            ("Julian Moore", "+1 425 555 0111")], "Linton": [("Julian Linton", "206‑555‑0199")],
            "Sam": [("Sam Lee", "work"), ("Sam Lee", "+1 (206) 555-0142")]}


@pytest.fixture
def mac(monkeypatch):
    calls = []

    def run(cmd, input=None, **kw):
        calls.append((cmd, input))
        if cmd[0] == "open":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "Contacts" in input:
            q = cmd[2].lower()  # Contacts' "name contains" ignores case
            people = {(n, p) for rows in CONTACTS.values() for n, p in rows if q in n.lower()}
            out = "".join(f"{n}{FS}{p}{RS}" for n, p in sorted(people))
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(messages.subprocess, "run", run)
    return calls


def test_numbers_are_normalized_the_way_contacts_stores_them():
    assert messages.normalize_number("(206) 555-0100") == "+12065550100"
    assert messages.normalize_number("+1 (206) 555‑0199") == "+12065550199"
    assert messages.normalize_number("‪206-555-0199‬") == "+12065550199"
    assert messages.normalize_number("tel:206-555-0199") == "+12065550199"
    assert messages.normalize_number("555-0199") is None and messages.normalize_number("mom") is None


def test_close_names_allow_nicknames_and_mishearings_but_not_other_people():
    assert messages.close_name("Michael Chen", "Mike Chen")
    assert messages.close_name("Julian Linton", "Julian Lanton")
    assert not messages.close_name("Julian Moore", "Julian Linton")


def test_find_contact_lists_names_and_numbers(make_ctx, mac):
    out = messages.find_contact(make_ctx("messages"), "Mom")
    assert out.text == "- Mom: +12065550100"
    assert out.data == {"contacts": [{"name": "Mom", "number": "+12065550100"}]}
    many = messages.find_contact(make_ctx("messages"), "Julian")
    assert "ask Elijah which" in many.text and len(many.data["contacts"]) == 2


def test_a_misheard_name_is_searched_word_by_word(make_ctx, mac):
    out = messages.find_contact(make_ctx("messages"), "Julian Lanton")
    assert out.data["contacts"] == [{"name": "Julian Linton", "number": "+12065550199"}]


def test_nobody_found_says_to_ask(make_ctx, mac):
    assert "Ask Elijah for the number" in messages.find_contact(make_ctx("messages"), "Zed").text


def test_send_text_passes_everything_as_arguments(make_ctx, mac):
    out = messages.send_text(make_ctx("messages"), "+12065550100", 'running late"; do shell script "rm', "Mom")
    assert out.text.startswith("Sent to Mom")
    cmd, script = mac[-1]
    assert cmd == ["osascript", "-", "+12065550100", 'running late"; do shell script "rm']
    assert "rm" not in script


def test_a_name_has_to_own_the_number(make_ctx, mac):
    with pytest.raises(ActionError, match="doesn't have \\+14255550111 for Mom"):
        messages.send_text(make_ctx("messages"), "+1 425 555 0111", "hi", "Mom")
    assert not any("Messages" in (s or "") for _, s in mac)


def test_a_typed_number_or_imessage_email_needs_no_name(make_ctx, mac):
    messages.send_text(make_ctx("messages"), "206 555 0123", "hi")
    assert mac[-1][0][2] == "+12065550123"
    messages.send_text(make_ctx("messages"), "friend@icloud.com", "hi")
    assert mac[-1][0][2] == "friend@icloud.com"
    with pytest.raises(ActionError, match="isn't a phone number"):
        messages.send_text(make_ctx("messages"), "mom", "hi")


def test_call_opens_a_tel_link(make_ctx, mac):
    out = messages.call(make_ctx("messages"), "(206) 555-0100", "Mom")
    assert mac[-1][0] == ["open", "tel:+12065550100"] and "Calling Mom" in out.text
    with pytest.raises(ActionError, match="only call a phone number"):
        messages.call(make_ctx("messages"), "friend@icloud.com")


def test_missing_automation_permission_says_where_to_allow_it(make_ctx, monkeypatch):
    monkeypatch.setattr(messages.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="", stderr="execution error: Not authorized to send Apple events to Messages. (-1743)"))
    with pytest.raises(ActionError, match="Privacy & Security > Automation"):
        messages.send_text(make_ctx("messages"), "+12065550100", "hi")


def test_a_hung_script_times_out_in_plain_words(make_ctx, monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired("osascript", 25)
    monkeypatch.setattr(messages.subprocess, "run", hang)
    with pytest.raises(ActionError, match="didn't answer in time"):
        messages.find_contact(make_ctx("messages"), "Mom")


def test_texts_and_calls_ask_with_the_number_and_name(real_apps):
    from jarvis.core import actions

    out = actions.run(real_apps, "messages__send_text", {"number": "+12065550100", "name": "Mom", "body": "late"},
                      actions.Origin("discord"))
    assert isinstance(out, actions.Asked) and out.question == 'Text Mom at +12065550100: "late"?'
    out = actions.run(real_apps, "messages__call", {"number": "+12065550100", "name": "Mom"}, actions.Origin("discord"))
    assert isinstance(out, actions.Asked) and out.question == "Call Mom at +12065550100?"

"""act: the ask says the steps in plain words, a step that doesn't match runs nothing, secrets are never typed."""

import pytest
from browser_fakes import Site, ctl

from jarvis.apps.browser import actions, steps
from jarvis.contract.api import ActionError
from jarvis.core import actions as core

FORM = Site(title="Contact", controls=[
    ctl(0, "text", "Your name"), ctl(1, "text", "Email address"), ctl(2, "select", "Topic", ["Billing", "Other"]),
    ctl(3, "checkbox", "Send me a copy"), ctl(4, "password", "Password"), ctl(5, "button", "Send"),
    ctl(6, "button", "Cancel"), ctl(7, "text", "Code", autocomplete="one-time-code")], after_press="https://example.com/thanks")
STEPS = ['fill "Your name" with "Elijah"', 'select "Topic" as "Other"', 'check "Send me a copy"', 'press "Send"']


@pytest.fixture
def form(driver):
    driver.sites["https://example.com/form"] = FORM
    driver.sites["https://example.com/thanks"] = Site(title="Thanks", text="Thanks, we got it.")
    return driver


def test_the_steps_parse_and_read_back_the_same():
    parsed = steps.parse(STEPS)
    assert [s.words() for s in parsed] == STEPS
    assert steps.parse(['fill "Say \\"hi\\"" with "a \\"b\\" c"'])[0].value == 'a "b" c'


@pytest.mark.parametrize("bad", ["click the button", 'press Send', 'fill "A"', 'type "x" into "y"', "", 'press "A" now'])
def test_a_step_that_isnt_in_the_grammar_is_refused(bad):
    with pytest.raises(ActionError, match="Steps look like"):
        steps.parse([bad])


def test_a_press_must_come_last_and_there_must_be_steps():
    with pytest.raises(ActionError, match="last step"):
        steps.parse(['press "Send"', 'check "Box"'])
    with pytest.raises(ActionError, match="no steps"):
        steps.parse([])
    with pytest.raises(ActionError, match="at most"):
        steps.parse(['check "A"'] * 13)


def test_act_is_outbound_and_asks_with_the_steps_in_plain_words(real_apps):
    out = core.run(real_apps, "browser__act", {"url": "https://example.com/form", "steps": STEPS, "why": "send a note"},
                   core.Origin("cli"))
    assert isinstance(out, core.Asked)
    assert 'fill "Your name" with "Elijah"; select "Topic" as "Other"; check "Send me a copy"; press "Send"' \
        in out.question and "It's to send a note. Go?" in out.question


def test_the_ask_is_bound_to_the_exact_steps(real_apps):
    out = core.run(real_apps, "browser__act", {"url": "https://example.com/form", "steps": STEPS, "why": "x"},
                   core.Origin("cli"))
    row = real_apps.conn.execute("SELECT args_json FROM asks WHERE id=?", (out.ask_id,)).fetchone()
    import json
    assert json.loads(row["args_json"])["steps"] == STEPS  # a yes runs what was stored, nothing else
    log = real_apps.conn.execute("SELECT args, result FROM action_log ORDER BY id DESC LIMIT 1").fetchone()
    assert "Elijah" not in log["args"] + log["result"]  # what he typed is not kept in the log


def test_a_yes_runs_the_steps_and_reports_the_page(app, form):
    out = actions.act(app, "https://example.com/form", STEPS, "send a note")
    assert form.ran == [STEPS] and "Thanks, we got it." in out.text and out.data["url"].endswith("/thanks")


def test_a_step_that_doesnt_match_runs_nothing_and_says_what_the_page_has(app, form):
    with pytest.raises(ActionError) as e:
        actions.act(app, "https://example.com/form", ['fill "Your name" with "E"', 'fill "Phone" with "1"'], "x")
    assert form.ran == []
    assert "no fill target “Phone”" in str(e.value) and 'text "Your name"' in str(e.value)


def test_a_label_must_match_exactly_not_loosely(app, form):
    with pytest.raises(ActionError, match="no fill target"):
        actions.act(app, "https://example.com/form", ['fill "Name" with "E"'], "x")
    assert form.ran == []


def test_two_matches_are_never_guessed(app, driver):
    driver.sites["https://example.com/two"] = Site(controls=[ctl(0, "text", "Name"), ctl(1, "text", "Name")])
    with pytest.raises(ActionError, match="matches 2"):
        actions.act(app, "https://example.com/two", ['fill "Name" with "E"'], "x")
    assert driver.ran == []


def test_a_wrong_option_names_the_options(app, form):
    with pytest.raises(ActionError, match="Billing, Other"):
        actions.act(app, "https://example.com/form", ['select "Topic" as "Refund"'], "x")
    assert form.ran == []


@pytest.mark.parametrize("label", ["Password", "Code"])
def test_a_password_or_code_field_is_refused_even_when_named(app, form, label):
    with pytest.raises(ActionError, match="always yours"):
        actions.act(app, "https://example.com/form", [f'fill "{label}" with "hunter2"'], "x")
    assert form.ran == []


def test_card_details_and_files_are_refused_by_label_or_type(app, driver):
    driver.sites["https://example.com/pay"] = Site(controls=[
        ctl(0, "text", "Card number"), ctl(1, "file", "Upload"), ctl(2, "text", "Security code"),
        ctl(3, "text", "Billing", autocomplete="cc-number")])
    for label in ("Card number", "Security code", "Billing"):
        with pytest.raises(ActionError, match="always yours"):
            actions.act(app, "https://example.com/pay", [f'fill "{label}" with "4111"'], "x")
    assert driver.ran == []


def test_a_switched_off_control_is_refused(app, driver):
    driver.sites["https://example.com/off"] = Site(controls=[ctl(0, "button", "Send", disabled=True)])
    with pytest.raises(ActionError, match="switched off"):
        actions.act(app, "https://example.com/off", ['press "Send"'], "x")


def test_a_banking_page_is_never_opened_for_a_form(app):
    with pytest.raises(ActionError, match="banking"):
        actions.act(app, "https://www.chase.com/login", ['press "Sign in"'], "x")


def test_outside_content_makes_even_a_trusted_act_ask(real_apps):
    # outbound always asks when the turn was steered by a page, even with a learned rule for the tool
    out = core.run(real_apps, "browser__act", {"url": "https://example.com/form", "steps": STEPS, "why": "x"},
                   core.Origin("cli", outside_content=True), allowed={"browser__act"})
    assert isinstance(out, core.Asked)

"""[[jobs]] in a manifest: every key validates, and every bad value has its own error."""

import pytest

from test_manifest import GOOD, load, make

from jarvis.contract.manifest import ManifestError, load_manifest
from jarvis.core.apps import load_apps

JOB = '''
[[jobs]]
name = "deliver"
description = "Send reminders that have come due."
handler = "deliver_due"
kind = "deliver"
every = "15m"
quiet = "ignore"
needs = []
timeout_s = 30
'''
CODE = "def go(ctx):\n    pass\n\ndef deliver_due(ctx, run):\n    pass\n"


def manifest(job=JOB, extra=""):
    return GOOD + extra + job


def test_a_job_loads_with_every_key(tmp_path, settings):
    reg = load_apps(settings, make(tmp_path, manifest=manifest(), actions=CODE))
    job = reg.apps["demo"].manifest.jobs[0]
    assert (job.name, job.handler, job.kind, job.every, job.at, job.quiet, job.needs, job.timeout_s) == (
        "deliver", "deliver_due", "deliver", 900, (), "ignore", (), 30)
    reg.close()


def test_at_takes_a_string_or_a_list_and_a_setting(tmp_path, settings):
    one = JOB.replace('every = "15m"', 'at = "07:00"')
    many = JOB.replace('every = "15m"', 'at = ["07:00", "21:30"]')
    both = JOB.replace('every = "15m"', 'every = "6h"\nat = "03:00"')
    named = JOB.replace('every = "15m"', 'at = "$when"')
    setting = '\n[settings.when]\ntype = "string"\ndefault = "07:00"\n'
    for text, extra in ((one, ""), (many, ""), (both, ""), (named, setting)):
        reg = load_apps(settings, make(tmp_path / str(hash(text)), manifest=manifest(text, extra), actions=CODE))
        reg.close()


def test_defaults_are_hold_and_a_sixty_second_timeout(tmp_path):
    path = make(tmp_path, manifest=manifest(JOB.replace('quiet = "ignore"\n', "").replace("timeout_s = 30\n", "")),
                actions=CODE)
    job = load_manifest(path / "demo" / "manifest.toml").jobs[0]
    assert (job.quiet, job.timeout_s, job.title) == ("hold", 60, "deliver")


def swap(old, new):
    return manifest(JOB.replace(old, new))


BAD = [
    ("name not snake_case", swap('name = "deliver"', 'name = "Deliver"'), "snake_case"),
    ("duplicate name", manifest(JOB + JOB), "duplicate job name 'deliver'"),
    ("no description", swap('description = "Send reminders that have come due."\n', ""), "'description'"),
    ("handler missing", swap('"deliver_due"', '"nope"'), "handler 'nope' not found"),
    ("handler not a name", swap('"deliver_due"', '"a b"'), "handler must be a function name"),
    ("kind", swap('"deliver"\nevery', '"think2"\nevery'), "kind must be one of deliver, sync, think"),
    ("quiet", swap('"ignore"', '"later"'), "quiet must be one of ignore, hold, skip"),
    ("needs", swap("needs = []", 'needs = ["power"]'), "needs takes only: network"),
    ("needs not a list", swap("needs = []", 'needs = "network"'), "needs takes only"),
    ("no trigger", swap('every = "15m"\n', ""), "needs 'every' or 'at'"),
    ("every unit", swap('"15m"', '"15 minutes"'), "every must look like"),
    ("every under 30s", swap('"15m"', '"10s"'), "at least 30s"),
    ("every not text", swap('"15m"', "15"), "every must look like"),
    ("at not a time", swap('every = "15m"', 'at = "7am"'), "must look like 07:00"),
    ("at out of range", swap('every = "15m"', 'at = ["07:00", "25:00"]'), "'25:00' must look like 07:00"),
    ("at not text", swap('every = "15m"', "at = 7"), "at must be a time"),
    ("at setting missing", swap('every = "15m"', 'at = "$when"'), 'must name a string setting'),
    ("timeout too small", swap("timeout_s = 30", "timeout_s = 0"), "timeout_s must be a whole number from 1 to 600"),
    ("timeout too big", swap("timeout_s = 30", "timeout_s = 601"), "timeout_s must be a whole number from 1 to 600"),
    ("timeout not int", swap("timeout_s = 30", 'timeout_s = "30"'), "timeout_s must be a whole number"),
    ("unknown key", swap("timeout_s = 30", "timeout_s = 30\nspeed = 1"), "unknown key(s) in a job: speed"),
    ("think may not ignore", swap('kind = "deliver"', 'kind = "think"'), 'a think job may not use quiet = "ignore"'),
    ("title empty", swap("timeout_s = 30", 'timeout_s = 30\ntitle = ""'), "title must be text"),
]


@pytest.mark.parametrize("label,text,fragment", BAD, ids=[b[0] for b in BAD])
def test_job_errors(tmp_path, settings, label, text, fragment):
    with pytest.raises(ManifestError) as e:
        load(tmp_path, settings, manifest=text, actions=CODE)
    assert "manifest.toml" in str(e.value) and fragment in str(e.value)


def test_a_think_job_may_hold_or_skip(tmp_path, settings):
    for quiet in ("hold", "skip"):
        reg = load_apps(settings, make(tmp_path / quiet, manifest=swap('kind = "deliver"\nevery = "15m"\nquiet = "ignore"',
                                                                      f'kind = "think"\nevery = "15m"\nquiet = "{quiet}"'),
                                       actions=CODE))
        reg.close()


def test_a_node_app_may_not_have_jobs(tmp_path, settings):
    text = manifest().replace('runs_on = "host"', 'runs_on = "node:camera"')
    with pytest.raises(ManifestError, match="only run for host apps"):
        load(tmp_path, settings, manifest=text, actions=CODE)


def test_an_app_may_have_only_jobs(tmp_path, settings):
    only_jobs = GOOD[:GOOD.index("[[actions]]")] + JOB
    reg = load_apps(settings, make(tmp_path, manifest=only_jobs, actions=CODE))
    assert reg.apps["demo"].manifest.actions == () and len(reg.apps["demo"].manifest.jobs) == 1
    reg.close()

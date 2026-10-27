import pytest

from jarvis.core.apps import load_apps, settings_for_missing_apps
from jarvis.contract.manifest import ManifestError
from jarvis.core.settings import SettingsError, load_settings

GOOD = '''
[app]
name = "demo"
description = "A demo."
runs_on = "host"
secrets = []

[settings.size]
type = "integer"
default = 3
minimum = 1
maximum = 10

[[actions]]
name = "go"
description = "Go."
effect = "read"
[actions.input]
type = "object"
additionalProperties = false
'''
ACTIONS = "def go(ctx):\n    pass\n"


def make(tmp_path, manifest=GOOD, actions=ACTIONS, folder="demo"):
    app = tmp_path / "apps" / folder
    app.mkdir(parents=True)
    (app / "manifest.toml").write_text(manifest)
    (app / "actions.py").write_text(actions)
    return tmp_path / "apps"


def load(tmp_path, settings, **kw):
    reg = load_apps(settings, make(tmp_path, **kw))
    reg.close()
    return reg


def test_good_manifest_loads(tmp_path, settings):
    reg = load_apps(settings, make(tmp_path))
    assert [t.name for t in reg.actions()] == ["demo__go"]
    reg.close()


BAD = [
    ("unknown app key", GOOD.replace('runs_on = "host"', 'runs_on = "host"\ncolor = 1'), "unknown key(s) in [app]: color"),
    ("unknown action key", GOOD.replace('effect = "read"', 'effect = "read"\nspeed = 1'), "unknown key(s) in an action"),
    ("unknown top key", GOOD + "\n[extra]\nx = 1\n", "unknown key(s) in the manifest: extra"),
    ("name mismatch", GOOD.replace('name = "demo"', 'name = "other"'), "must equal the folder name"),
    ("runs_on", GOOD.replace('"host"', '"cloud"'), "runs_on must be"),
    ("node runs_on ok but empty cap", GOOD.replace('"host"', '"node:"'), "runs_on must be"),
    ("effect", GOOD.replace('"read"', '"write"'), "effect must be one of"),
    ("undo on read", GOOD.replace('effect = "read"', 'effect = "read"\nundo = "go"'), "only allowed on local"),
    ("undo handler missing", GOOD.replace('"read"', '"local"\nundo = "nope"'), "undo handler 'nope' not found"),
    ("outbound without confirm", GOOD.replace('"read"', '"outbound"'), "need a 'confirm'"),
    ("irreversible without confirm", GOOD.replace('"read"', '"irreversible"'), "need a 'confirm'"),
    ("plan without confirm", GOOD.replace('"read"', '"plan"'), "need a 'confirm'"),
    ("confirm on read", GOOD.replace('effect = "read"', 'effect = "read"\nconfirm = "Sure?"'), "only for outbound"),
    ("sends_data without confirm", GOOD.replace('effect = "read"', 'effect = "read"\nsends_data = true'),
     "actions that send data need a 'confirm'"),
    ("confirm field not property", GOOD.replace('"read"', '"outbound"\nconfirm = "Send to {who}?"'), "{who}"),
    ("handler missing", GOOD.replace('name = "go"', 'name = "go"\nhandler = "nope"'), "handler 'nope' not found"),
    ("schema keyword", GOOD.replace('additionalProperties = false', 'additionalProperties = false\npattern = "x"'),
     "unsupported keyword 'pattern'"),
    ("duplicate action", GOOD + GOOD[GOOD.index("[[actions]]"):GOOD.index("[actions.input]")] +
     '[actions.input]\ntype = "object"\n', "duplicate tool name 'go'"),
    ("bad action name", GOOD.replace('name = "go"', 'name = "Go__x"'), "snake_case"),
    ("no actions", GOOD[:GOOD.index("[[actions]]")], "at least one [[actions]]"),
    ("invalid toml", "[app", "Expected"),
    ("shared local action", GOOD.replace('effect = "read"', 'effect = "local"\nshare = true'),
     "only read actions can be shared"),
    ("hidden and unshared", GOOD.replace('effect = "read"', 'effect = "read"\ntool = false'), "must be shared"),
    ("share not a bool", GOOD.replace('effect = "read"', 'effect = "read"\nshare = "yes"'), "share must be true or false"),
    ("uses itself", GOOD.replace('runs_on = "host"', 'runs_on = "host"\nuses = ["demo"]'), "doesn't list itself"),
    ("uses not names", GOOD.replace('runs_on = "host"', 'runs_on = "host"\nuses = "school"'), "uses must be a list"),
]


@pytest.mark.parametrize("label,manifest,fragment", BAD, ids=[b[0] for b in BAD])
def test_manifest_errors(tmp_path, settings, label, manifest, fragment):
    with pytest.raises(ManifestError) as e:
        load(tmp_path, settings, manifest=manifest)
    assert "manifest.toml" in str(e.value) and fragment in str(e.value)


def test_missing_actions_file(tmp_path, settings):
    apps = make(tmp_path)
    (apps / "demo" / "actions.py").unlink()
    with pytest.raises(ManifestError, match="actions.py is missing"):
        load_apps(settings, apps)


def with_settings(data_dir, text):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.toml").write_text(text)
    return load_settings()


def test_app_settings_override_defaults(tmp_path, data_dir):
    reg = load_apps(with_settings(data_dir, "[apps.demo]\nsize = 9\n"), make(tmp_path))
    assert reg.apps["demo"].ctx.settings["size"] == 9
    reg.close()


@pytest.mark.parametrize("section,fragment", [
    ("[apps.demo]\nsiez = 9\n", "unknown setting 'siez'"),
    ('[apps.demo]\nsize = "big"\n', "size: must be an integer"),
    ("[apps.demo]\nsize = 0\n", "size: must be ≥ 1"),
    ("[apps.demo]\nsize = 11\n", "size: must be ≤ 10"),
    ("[apps.demo]\nsize = true\n", "size: must be an integer"),
])
def test_bad_app_settings(tmp_path, data_dir, section, fragment):
    with pytest.raises(SettingsError, match="demo") as e:
        load_apps(with_settings(data_dir, section), make(tmp_path))
    assert fragment in str(e.value)


def test_disabled_app_is_skipped_and_never_imported(tmp_path, data_dir):
    apps = make(tmp_path, actions="raise RuntimeError('imported')")
    reg = load_apps(with_settings(data_dir, "[apps.demo]\nenabled = false\n"), apps)
    assert reg.apps == {}
    reg.close()


def test_secrets_limited_to_manifest_keys(tmp_path, data_dir):
    (data_dir).mkdir(parents=True)
    (data_dir / ".env").write_text("WANTED=1\nOTHER=2\n")
    apps = make(tmp_path, manifest=GOOD.replace("secrets = []", 'secrets = ["WANTED", "MISSING"]'))
    reg = load_apps(load_settings(), apps)
    assert dict(reg.apps["demo"].ctx.secrets) == {"WANTED": "1"}
    reg.close()


SETTING_BAD = [
    ("scalar shorthand", "[settings]\nsize = 3\n", "setting 'size'"),
    ("no default", '[settings.size]\ntype = "integer"\n', "needs a default"),
    ("bad default", '[settings.size]\ntype = "integer"\ndefault = 99\nmaximum = 10\n', "default is invalid"),
    ("object type", '[settings.size]\ntype = "object"\ndefault = 1\n', "type must be one of"),
    ("unknown keyword", '[settings.size]\ntype = "integer"\ndefault = 1\nstep = 2\n', "unsupported keyword"),
]


@pytest.mark.parametrize("label,block,fragment", SETTING_BAD)
def test_bad_setting_declarations(tmp_path, settings, label, block, fragment):
    manifest = GOOD.replace('[settings.size]\ntype = "integer"\ndefault = 3\nminimum = 1\nmaximum = 10\n', block)
    with pytest.raises(ManifestError, match=fragment):
        load(tmp_path, settings, manifest=manifest)


def test_unknown_app_sections_warn_and_are_listed(tmp_path, data_dir, caplog):
    settings = with_settings(data_dir, "[apps.demo]\nsize = 2\n[apps.demoo]\nenabled = false\n")
    with caplog.at_level("WARNING", logger="jarvis.core.apps"):
        reg = load_apps(settings, make(tmp_path))
    reg.close()
    assert [r.getMessage() for r in caplog.records] == ["settings.toml has [apps.demoo] but no app named demoo"]
    assert settings_for_missing_apps(settings, tmp_path / "apps") == ["demoo"]


def test_disabled_app_section_is_not_missing(tmp_path, data_dir):
    settings = with_settings(data_dir, "[apps.demo]\nenabled = false\n")
    assert settings_for_missing_apps(settings, make(tmp_path)) == []


def test_context_handler_must_exist(tmp_path, settings):
    manifest = GOOD.replace('runs_on = "host"', 'runs_on = "host"\ncontext = "nope"')
    with pytest.raises(ManifestError, match="context handler 'nope' not found"):
        load(tmp_path, settings, manifest=manifest)


def test_untrusted_output_must_be_a_bool(tmp_path, settings):
    manifest = GOOD.replace('effect = "read"', 'effect = "read"\nuntrusted_output = "yes"')
    with pytest.raises(ManifestError, match="untrusted_output must be true or false"):
        load(tmp_path, settings, manifest=manifest)


SESSION = '''
[[sessions]]
name = "live_view"
description = "A live view."
effect = "read"
start = "go"
sample = "go"
stop = "go"
idle_minutes = 5
'''


def test_session_generates_three_tools(tmp_path, settings):
    reg = load_apps(settings, make(tmp_path, manifest=GOOD + SESSION, actions="def go(ctx, *a, **k):\n    pass\n"))
    reg.close()
    tools = {t.name: t for t in reg.actions()}
    assert {"demo__live_view_start", "demo__live_view_look", "demo__live_view_stop"} <= set(tools)
    manifest = reg.apps["demo"].manifest
    assert manifest.sessions[0].idle_minutes == 5
    assert [(t.phase, t.session) for t in manifest.tools[1:]] == [("start", "live_view"), ("look", "live_view"), ("stop", "live_view")]


@pytest.mark.parametrize("change,message", [
    ('effect = "read"', 'effect = "outbound"'),
    ("idle_minutes = 5", "idle_minutes = 0"),
    ("idle_minutes = 5", "idle_minutes = 121"),
    ("idle_minutes = 5", 'idle_minutes = "soon"'),
    ('stop = "go"', ""),
    ('start = "go"', 'start = "not valid!"'),
])
def test_bad_sessions_are_refused(tmp_path, settings, change, message):
    bad = GOOD + SESSION.replace(*([change, message] if isinstance(change, str) else change))
    with pytest.raises(ManifestError, match="session 'live_view'"):
        load(tmp_path, settings, manifest=bad)


def test_session_handlers_must_exist(tmp_path, settings):
    with pytest.raises(ManifestError, match="session 'live_view': start handler 'begin' not found"):
        load(tmp_path, settings, manifest=GOOD + SESSION.replace('start = "go"', 'start = "begin"'),
             actions="def go(ctx):\n    pass\n")


def test_session_tool_names_cannot_collide_with_actions(tmp_path, settings):
    clash = GOOD + SESSION + GOOD[GOOD.index("[[actions]]"):].replace('name = "go"', 'name = "live_view_look"')
    with pytest.raises(ManifestError, match="duplicate tool name 'live_view_look'"):
        load(tmp_path, settings, manifest=clash)


def test_an_app_may_be_only_sessions(tmp_path, settings):
    only = GOOD[:GOOD.index("[[actions]]")] + SESSION
    reg = load_apps(settings, make(tmp_path, manifest=only, actions="def go(ctx, *a, **k):\n    pass\n"))
    reg.close()
    assert len(reg.actions()) == 3


def test_timeout_is_bounded(tmp_path, settings):
    with pytest.raises(ManifestError, match="timeout_s must be a whole number from 1 to 300"):
        load(tmp_path, settings, manifest=GOOD.replace('effect = "read"', 'effect = "read"\ntimeout_s = 0'))

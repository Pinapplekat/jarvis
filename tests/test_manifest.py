import pytest

from jarvis.core.apps import load_apps
from jarvis.core.manifest import ManifestError
from jarvis.core.settings import SettingsError, load_settings

GOOD = '''
[app]
name = "demo"
description = "A demo."
runs_on = "host"
secrets = []

[settings]
size = 3

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
    ("sessions", GOOD + "\n[sessions]\n", "'sessions' is not valid yet"),
    ("jobs", GOOD + "\n[[jobs]]\n", "'jobs' is not valid yet"),
    ("name mismatch", GOOD.replace('name = "demo"', 'name = "other"'), "must equal the folder name"),
    ("runs_on", GOOD.replace('"host"', '"cloud"'), "runs_on must be"),
    ("node runs_on ok but empty cap", GOOD.replace('"host"', '"node:"'), "runs_on must be"),
    ("effect", GOOD.replace('"read"', '"write"'), "effect must be one of"),
    ("undo on read", GOOD.replace('effect = "read"', 'effect = "read"\nundo = "go"'), "only allowed on local"),
    ("undo handler missing", GOOD.replace('"read"', '"local"\nundo = "nope"'), "undo handler 'nope' not found"),
    ("outbound without confirm", GOOD.replace('"read"', '"outbound"'), "need a 'confirm'"),
    ("irreversible without confirm", GOOD.replace('"read"', '"irreversible"'), "need a 'confirm'"),
    ("confirm on read", GOOD.replace('effect = "read"', 'effect = "read"\nconfirm = "Sure?"'), "only for outbound"),
    ("confirm field not property", GOOD.replace('"read"', '"outbound"\nconfirm = "Send to {who}?"'), "{who}"),
    ("handler missing", GOOD.replace('name = "go"', 'name = "go"\nhandler = "nope"'), "handler 'nope' not found"),
    ("schema keyword", GOOD.replace('additionalProperties = false', 'additionalProperties = false\npattern = "x"'),
     "unsupported keyword 'pattern'"),
    ("duplicate action", GOOD + GOOD[GOOD.index("[[actions]]"):GOOD.index("[actions.input]")] +
     '[actions.input]\ntype = "object"\n', "duplicate action name 'go'"),
    ("bad action name", GOOD.replace('name = "go"', 'name = "Go__x"'), "snake_case"),
    ("no actions", GOOD[:GOOD.index("[[actions]]")], "at least one [[actions]]"),
    ("invalid toml", "[app", "Expected"),
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
    ('[apps.demo]\nsize = "big"\n', "expected int"),
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

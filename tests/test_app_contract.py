"""Every real app: manifest valid, handlers match their schema, migrations stay in the app's tables."""

import inspect
import re

import pytest

from types import SimpleNamespace

from jarvis.contract.loader import DEFAULT_APPS_DIR, check_handlers, import_actions
from jarvis.contract.manifest import load_manifest

APPS = sorted(p.parent.name for p in DEFAULT_APPS_DIR.glob("*/manifest.toml"))
TABLE_DDL = re.compile(
    r"\b(?:CREATE|ALTER|DROP)\s+(?:TEMP\s+|TEMPORARY\s+)?TABLE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?[\"`]?(\w+)", re.I
)


def test_apps_exist():
    assert {"calculator", "memory", "camera", "screen"} <= set(APPS)


@pytest.fixture
def app(request):
    """Read the way the node does: the manifest plus the imported code, whichever machine the app runs on."""
    manifest = load_manifest(DEFAULT_APPS_DIR / request.param / "manifest.toml")
    module = import_actions(DEFAULT_APPS_DIR, request.param)
    check_handlers(manifest, module)
    return SimpleNamespace(manifest=manifest, module=module)


@pytest.mark.parametrize("app", APPS, indirect=True)
def test_handlers_match_schema(app):
    for action in app.manifest.actions:
        fn = getattr(app.module, action.handler)
        params = list(inspect.signature(fn).parameters.values())[1:]  # skip ctx
        props = set(action.input.get("properties", {}))
        assert {p.name for p in params} == props, f"{action.name}: parameters differ from input properties"
        required = set(action.input.get("required", []))
        defaults = {n for n, s in action.input.get("properties", {}).items() if "default" in s}
        no_default = {p.name for p in params if p.default is inspect.Parameter.empty}
        assert required <= no_default, f"{action.name}: required properties need parameters without defaults"
        assert no_default <= required | defaults, f"{action.name}: optional property needs a default"


@pytest.mark.parametrize("app", APPS, indirect=True)
def test_untrusted_output_flag_and_context_handler(app):
    assert all(type(a.untrusted_output) is bool for a in app.manifest.actions)
    if app.manifest.context:
        fn = getattr(app.module, app.manifest.context)
        assert len(inspect.signature(fn).parameters) == 1  # (ctx)


@pytest.mark.parametrize("app", APPS, indirect=True)
def test_sessions_have_handlers_and_sane_settings(app):
    for session in app.manifest.sessions:
        assert session.effect in ("read", "local")
        assert 1 <= session.idle_minutes <= 120
        start, sample, stop = (getattr(app.module, h) for h in session.handlers())
        props = set(session.input.get("properties", {}))
        assert {p for p in inspect.signature(start).parameters if p != "ctx"} == props
        assert len(inspect.signature(sample).parameters) == 2  # (ctx, state)
        assert len(inspect.signature(stop).parameters) == 2


@pytest.mark.parametrize("app", APPS, indirect=True)
def test_effects_confirm_and_undo(app):
    for a in app.manifest.actions:
        assert a.effect in ("read", "local", "outbound", "irreversible")
        assert (a.confirm is not None) == (a.effect in ("outbound", "irreversible"))
        assert a.undo is None or a.effect == "local"
        if a.undo:
            fn = getattr(app.module, a.undo)
            assert len(inspect.signature(fn).parameters) == 2


@pytest.mark.parametrize("name", APPS)
def test_migrations_touch_only_own_tables(name):
    for sql in (DEFAULT_APPS_DIR / name / "migrations").glob("*.sql"):
        assert re.match(r"^\d{4}_.+\.sql$", sql.name)
        for table in TABLE_DDL.findall(sql.read_text()):
            assert table.startswith(f"{name}_"), f"{sql.name} touches table {table}"


def test_migration_check_catches_a_violation():
    assert TABLE_DDL.findall("DROP TABLE IF EXISTS facts; create table memory_x (a);") == ["facts", "memory_x"]


@pytest.mark.parametrize("name", APPS)
def test_app_has_tests(name):
    assert any((DEFAULT_APPS_DIR / name / "tests").glob("test_*.py"))


def test_core_names_no_app():
    for path in DEFAULT_APPS_DIR.parent.joinpath("core").rglob("*.py"):
        text = path.read_text()
        for name in APPS:
            assert not re.search(rf"[\"']{name}[\"']", text), f"{path.name} names the {name} app"

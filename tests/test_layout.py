"""The architecture rules, checked from the imports under jarvis/ (absolute and relative)."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "jarvis"


def modules():
    for path in PKG.rglob("*.py"):
        yield path, ".".join(path.relative_to(ROOT).with_suffix("").parts)


def imports(path, name):
    """Every module this file imports, with relative imports resolved against its own package."""
    package = name.split(".")[:-1]  # jarvis.core.db and jarvis.core.__init__ both live in jarvis.core
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - (node.level - 1)] if node.level else []
            if node.module:
                yield ".".join([*base, node.module])
            else:  # `from .. import node`: each name may be a module
                yield from (".".join([*base, a.name]) for a in node.names)


def within(name, prefix):
    return name == prefix or name.startswith(prefix + ".")


def hits(importer, banned, allowed=()):
    found = []
    for path, name in modules():
        if within(name, importer):
            found += [
                f"{name} imports {i}"
                for i in imports(path, name)
                if any(within(i, b) for b in banned) and not any(within(i, a) for a in allowed)
            ]
    return found


def test_relative_imports_are_resolved(tmp_path):
    f = tmp_path / "db.py"
    f.write_text("from ..node import runtime\nfrom . import settings\nfrom ..protocol import x\n")
    assert list(imports(f, "jarvis.core.db")) == ["jarvis.node", "jarvis.core.settings", "jarvis.protocol"]


def test_contract_imports_nothing_else_from_jarvis():
    assert hits("jarvis.contract", ["jarvis"], allowed=["jarvis.contract"]) == []


def test_core_independent_of_node_apps_channels_host_legacy():
    banned = ["jarvis.node", "jarvis.apps", "jarvis.channels", "jarvis.host", "jarvis.speech", "legacy"]
    assert hits("jarvis.core", banned) == []


def test_channels_import_only_the_channel_interface_contract_and_protocol():
    allowed = ["jarvis.core.channel", "jarvis.contract", "jarvis.protocol", "jarvis.speech", "jarvis.channels"]
    assert hits("jarvis.channels", ["jarvis"], allowed=allowed) == []


def test_only_the_discord_adapter_imports_discord():
    importers = [n for p, n in modules() if any(within(i, "discord") for i in imports(p, n))]
    assert importers == ["jarvis.channels.discord"]


def test_node_never_imports_core():
    assert hits("jarvis.node", ["jarvis.core"]) == []


def test_speech_imports_nothing_else_from_jarvis():
    assert hits("jarvis.speech", ["jarvis"], allowed=["jarvis.speech"]) == []


def test_only_channels_and_the_host_import_speech():
    importers = sorted({n for p, n in modules() if not within(n, "jarvis.speech")
                        and any(within(i, "jarvis.speech") for i in imports(p, n)) and ".tests." not in n})
    assert all(within(n, "jarvis.channels") or n == "jarvis.host" for n in importers), importers


def test_protocol_imports_nothing_else_from_jarvis():
    assert hits("jarvis.protocol", ["jarvis"], allowed=["jarvis.protocol"]) == []


def app_violations(apps_dir, package="jarvis.apps"):
    """Imports in each app's own code (not its tests/) that go beyond the contract api, protocol and itself."""
    found = []
    for path in sorted(apps_dir.rglob("*.py")):
        rel = path.relative_to(apps_dir)
        if len(rel.parts) < 2 or "tests" in rel.parts:  # tests may drive the core
            continue
        name = ".".join([package, *rel.with_suffix("").parts])
        own = f"{package}.{rel.parts[0]}"
        found += [
            f"{name} imports {i}"
            for i in imports(path, name)
            if within(i, "jarvis") and not any(within(i, a) for a in ("jarvis.contract.api", "jarvis.protocol", own))
        ]
    return found


def test_apps_use_only_the_contract_api_and_protocol():
    assert app_violations(PKG / "apps") == []


def test_app_check_catches_violations_in_actions_but_not_tests(tmp_path):
    app = tmp_path / "demo"
    (app / "tests").mkdir(parents=True)
    (app / "actions.py").write_text("from jarvis.core import db\nfrom jarvis.contract.api import Result\n")
    (app / "tests" / "test_x.py").write_text("from jarvis.core import db\n")
    assert app_violations(tmp_path) == ["jarvis.apps.demo.actions imports jarvis.core"]


def test_apps_cannot_import_each_other(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "actions.py").write_text("from jarvis.apps.b import actions\n")
    assert app_violations(tmp_path) == ["jarvis.apps.a.actions imports jarvis.apps.b"]


def test_nothing_imports_legacy():
    assert hits("jarvis", ["legacy"]) == []


def test_only_the_sdk_brain_and_the_terminal_tasks_import_the_sdk():
    importers = [n for p, n in modules() if ".tests." not in n and any(within(i, "claude_agent_sdk") for i in imports(p, n))]
    assert sorted(importers) == ["jarvis.apps.terminal.agent", "jarvis.core.brain_sdk"]  # tasks: the files are on the node


def test_modules_under_400_lines():
    long = [n for p, n in modules() if len(p.read_text().splitlines()) > 400]
    assert long == []

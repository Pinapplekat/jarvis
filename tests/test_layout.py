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


def test_core_independent_of_node_apps_legacy():
    assert hits("jarvis.core", ["jarvis.node", "jarvis.apps", "legacy"]) == []


def test_node_never_imports_core():
    assert hits("jarvis.node", ["jarvis.core"]) == []


def test_protocol_imports_nothing_else_from_jarvis():
    assert hits("jarvis.protocol", ["jarvis"], allowed=["jarvis.protocol"]) == []


def test_apps_use_only_the_core_api_and_protocol():
    found = []
    for app_dir in (p for p in (PKG / "apps").iterdir() if p.is_dir() and not p.name.startswith("__")):
        own = f"jarvis.apps.{app_dir.name}"
        found += hits(own, ["jarvis"], allowed=["jarvis.core.api", "jarvis.protocol", own])
    assert found == []


def test_nothing_imports_legacy():
    assert hits("jarvis", ["legacy"]) == []


def test_modules_under_400_lines():
    long = [n for p, n in modules() if len(p.read_text().splitlines()) > 400]
    assert long == []

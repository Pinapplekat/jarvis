"""The architecture rules, checked from the imports under jarvis/."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "jarvis"


def modules():
    for path in PKG.rglob("*.py"):
        yield path, ".".join(path.relative_to(ROOT).with_suffix("").parts)


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def hits(prefix_of_importer, banned):
    found = []
    for path, name in modules():
        if name.startswith(prefix_of_importer):
            found += [
                f"{name} imports {i}"
                for i in imports(path)
                if any(i == b or i.startswith(b + ".") for b in banned)
            ]
    return found


def test_core_independent_of_node_apps_legacy():
    assert hits("jarvis.core", ["jarvis.node", "jarvis.apps", "legacy"]) == []


def test_node_never_imports_core():
    assert hits("jarvis.node", ["jarvis.core"]) == []


def test_nothing_imports_legacy():
    assert hits("jarvis", ["legacy"]) == []


def test_modules_under_400_lines():
    long = [n for p, n in modules() if len(p.read_text().splitlines()) > 400]
    assert long == []

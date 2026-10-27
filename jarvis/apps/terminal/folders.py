"""Where the terminal app may write: the work folders, the read-only ones, and which project a folder belongs to."""

from __future__ import annotations

import glob
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _paths(text: str) -> tuple[Path, ...]:
    """A comma-separated setting as resolved folders. `*` in an entry matches (worktrees of one checkout)."""
    found: list[Path] = []
    for raw in text.split(","):
        raw = os.path.expanduser(raw.strip())
        if raw:
            found += [Path(m).resolve() for m in sorted(glob.glob(raw))] or [Path(raw).resolve()]
    return tuple(dict.fromkeys(found))


@dataclass(frozen=True)
class Folders:
    work_roots: tuple[Path, ...]
    read_only: tuple[Path, ...]
    fixed: Path | None = None  # a task's folder: it is the project root, whatever repo it sits in

    @classmethod
    def from_settings(cls, settings: Mapping[str, object]) -> "Folders":
        return cls(_paths(str(settings["work_roots"])), _paths(str(settings["read_only"])))

    def for_task(self, folder: Path) -> "Folders":
        return Folders((folder,), self.read_only, folder)

    def is_read_only(self, path: Path) -> bool:
        return any(inside(path, r) for r in self.read_only)

    def in_work_roots(self, path: Path) -> bool:
        return any(inside(path, r) for r in self.work_roots)

    def project_root(self, cwd: Path) -> Path | None:
        """The git repo containing `cwd` if that repo is under a work root, else `cwd` if it is, else None
        (outside the work roots nothing is writable without asking)."""
        cwd = cwd.resolve()
        if self.fixed is not None:
            return self.fixed if inside(cwd, self.fixed) else None
        for folder in (cwd, *cwd.parents):
            if (folder / ".git").exists():
                if self.in_work_roots(folder):
                    return folder
                break
        return cwd if self.in_work_roots(cwd) else None

    def writable_root(self, cwd: Path) -> Path | None:
        """The project root, unless it is one of the read-only folders."""
        root = self.project_root(cwd)
        return None if root is None or self.is_read_only(root) else root

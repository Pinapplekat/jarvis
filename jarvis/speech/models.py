"""Finding model files. Folders are searched read-only (the old JARVIS's models may be used by path, never written)."""

from __future__ import annotations

from pathlib import Path


def hf_snapshot(root: Path, repo: str) -> Path | None:
    """The newest snapshot of `repo` in a Hugging Face style cache folder under `root`, without touching the cache
    (huggingface_hub would create lock files in it)."""
    snapshots = root / f"models--{repo.replace('/', '--')}" / "snapshots"
    found = sorted((p for p in snapshots.glob("*") if p.is_dir()), key=lambda p: p.stat().st_mtime) if snapshots.is_dir() else []
    return found[-1] if found else None


def find(dirs: list[Path], repo: str, folder: str | None = None) -> Path | None:
    """`repo` as a plain folder called `folder` or as a cache entry, in the first of `dirs` that has it."""
    for root in dirs:
        if folder and (root / folder).is_dir():
            return root / folder
        if (snap := hf_snapshot(root, repo)) is not None:
            return snap
    return None


def download(repo: str, dest: Path, allow: list[str] | None = None) -> Path:
    """Fetch a repo into the plain folder `dest` (only ever called for Falak's own models folder)."""
    from huggingface_hub import snapshot_download

    dest.mkdir(parents=True, exist_ok=True)
    return Path(snapshot_download(repo, local_dir=dest, allow_patterns=allow))

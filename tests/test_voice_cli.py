"""`jarvis voice`: reads models from the old JARVIS folder without writing to it, writes only to Falak's folder."""

import os

from jarvis import cli, voice_cli
from jarvis.channels import desk


def fake_old_whisper(root):
    snap = root / "models--Systran--faster-whisper-base.en" / "snapshots" / "abc"
    blobs = root / "models--Systran--faster-whisper-base.en" / "blobs"
    snap.mkdir(parents=True)
    blobs.mkdir()
    (blobs / "b1").write_text("weights")
    os.symlink("../../blobs/b1", snap / "model.bin")  # the cache links its files, like the real one
    return snap


def snapshot_of(root):
    return sorted((str(p.relative_to(root)), p.is_symlink()) for p in root.rglob("*"))


def test_status_says_what_is_missing(data_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(desk, "LEGACY_MODELS", tmp_path / "no-old-jarvis")
    assert cli.main(["voice", "status"]) == 0
    out = capsys.readouterr().out
    assert out.count("missing") == 2 and "jarvis voice setup" in out


def test_setup_copies_the_old_whisper_model_and_never_writes_there(data_dir, tmp_path, monkeypatch, capsys):
    old = tmp_path / "old-jarvis-models"
    fake_old_whisper(old)
    before = snapshot_of(old)
    monkeypatch.setattr(desk, "LEGACY_MODELS", old)
    downloads = []
    import jarvis.speech.models as models

    monkeypatch.setattr(models, "download", lambda repo, dest, allow=None: downloads.append((repo, dest, allow)) or dest)
    assert cli.main(["voice", "setup"]) == 0
    assert snapshot_of(old) == before  # read only: not a file added, changed or removed
    copied = data_dir / "models" / "models--Systran--faster-whisper-base.en" / "snapshots" / "copied" / "model.bin"
    assert copied.read_text() == "weights" and not copied.is_symlink()
    [(repo, dest, allow)] = downloads
    assert repo == "mlx-community/Kokoro-82M-bf16" and dest == data_dir / "models" / "kokoro-mlx"
    assert "voices/*.safetensors" in allow and not any(a.endswith(".pt") for a in allow)


def test_setup_stops_when_the_download_would_fill_the_disk(data_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(desk, "LEGACY_MODELS", tmp_path / "none")
    monkeypatch.setattr(voice_cli.shutil, "disk_usage", lambda p: type("U", (), {"free": 2 * 1024**3})())
    assert cli.main(["voice", "setup"]) == 1
    assert "under 2 GiB" in capsys.readouterr().err

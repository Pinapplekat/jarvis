#!/usr/bin/env python3
"""Measure the voice engines on this Mac (M9 section 0).

Run it with Claude Code, a browser and `falak start` open as usual, so the numbers include the memory pressure
Elijah actually has. Each benchmark is its own process, so the resident memory it reports is its own.

    scripts/voice_bench.py all --engines-python <venv>/bin/python     # everything, then a summary
    <venv>/bin/python scripts/voice_bench.py omnivoice                 # one benchmark
    uv run python scripts/voice_bench.py brain

The engines (torch, omnivoice, kokoro, faster-whisper, mlx-whisper) are imported only when their benchmark runs, so
they can live in a throwaway venv and not in the project. Nothing here touches Falak-dev, Falak-node or the old
JARVIS folder: downloads and test audio go under a scratch folder (--scratch, else FALAK_DATA_DIR, else a new temp
folder), and the old JARVIS models are only read. Before any download it checks the free disk and stops if the
download would leave less than 2 GiB.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import os
import re
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SENTENCE = "Good morning, sir. You have three meetings tomorrow, the first at nine."  # 12 words
VOICE_DESIGN = "male, low pitch, british accent"
QUESTION = "What is the capital of France? Answer in one short sentence."
STT_TEXT = "Good morning sir, what is on my calendar for tomorrow afternoon, and is anything after five?"
MIN_FREE = 2 * 1024**3
OLD_MODELS = Path.home() / "Library/Application Support/JARVIS/models"  # read only
PROTECTED = [Path.home() / "Library/Application Support" / n for n in ("Falak-dev", "Falak-node", "JARVIS")]


# ---- the machine -----------------------------------------------------------------------------------------------

def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def rss_mb() -> float:
    return int(sh("ps", "-o", "rss=", "-p", str(os.getpid())) or 0) / 1024


def footprint_mb() -> float:
    """What macOS charges this process, including pages it has compressed or swapped (rss alone hides those)."""
    m = re.search(r"Footprint:\s*([\d.]+)\s*(KB|MB|GB)", sh("footprint", "-p", str(os.getpid())))
    return float(m.group(1)) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2)] if m else 0.0


def swap_mb() -> float:
    m = re.search(r"used = ([\d.]+)M", sh("sysctl", "-n", "vm.swapusage"))
    return float(m.group(1)) if m else 0.0


def pageouts() -> int:
    m = re.search(r"Pageouts:\s+(\d+)", sh("vm_stat"))
    return int(m.group(1)) if m else 0


def free_percent() -> int:
    m = re.search(r"free percentage: (\d+)%", sh("memory_pressure"))
    return int(m.group(1)) if m else -1


def machine() -> dict:
    top = sh("ps", "-axo", "rss=,comm=").splitlines()
    big = sorted(((int(l.split(None, 1)[0]), l.split(None, 1)[1]) for l in top if l.strip()), reverse=True)[:6]
    return {
        "ram_gb": int(sh("sysctl", "-n", "hw.memsize")) / 1024**3,
        "swap_used_mb": swap_mb(),
        "memory_free_percent": free_percent(),
        "free_disk_gib": shutil.disk_usage("/").free / 1024**3,
        "falak_running": bool(sh("pgrep", "-f", "jarvis.cli node run") or sh("pgrep", "-f", "jarvis start")),
        "biggest_processes": [f"{kb // 1024} MB {Path(name).name}" for kb, name in big],
    }


class Meter:
    """Memory and swap around a stretch of work."""

    def __init__(self) -> None:
        self.swap0, self.out0, self.t0 = swap_mb(), pageouts(), time.perf_counter()

    def report(self) -> dict:
        return {
            "rss_mb": round(rss_mb()),
            "footprint_mb": round(footprint_mb()),
            "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2),  # bytes on macOS
            "swap_before_mb": round(self.swap0),
            "swap_after_mb": round(swap_mb()),
            "pageouts_during": pageouts() - self.out0,
            "free_percent_after": free_percent(),
        }


def stats(times: list[float]) -> dict:
    return {"runs": [round(t, 3) for t in times], "median_s": round(statistics.median(times), 3)}


# ---- scratch folder and downloads ------------------------------------------------------------------------------

def scratch_dir(arg: str | None) -> Path:
    path = Path(arg or os.environ.get("FALAK_DATA_DIR") or tempfile.mkdtemp(prefix="voice-bench-")).resolve()
    for protected in PROTECTED:
        if path == protected or protected in path.parents:
            sys.exit(f"refusing to use {path}: it is inside {protected}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def download(repo: str, scratch: Path, allow: list[str] | None = None) -> Path:
    """Fetch a Hugging Face repo into the scratch folder, after checking it leaves 2 GiB of disk free."""
    from huggingface_hub import HfApi, snapshot_download

    cache = scratch / "hf"
    files = HfApi().model_info(repo, files_metadata=True).siblings or []
    size = sum(f.size or 0 for f in files if not allow or any(fnmatch.fnmatch(f.rfilename, p) for p in allow))
    free = shutil.disk_usage(scratch).free
    print(f"# {repo}: {size / 1024**3:.2f} GiB to download, {free / 1024**3:.2f} GiB free", file=sys.stderr)
    if free - size < MIN_FREE:
        sys.exit(f"stopping: downloading {repo} would leave under 2 GiB free")
    return Path(snapshot_download(repo, cache_dir=cache, allow_patterns=allow))


def test_clip(scratch: Path, seconds: float = 4.0) -> tuple[Path, bytes]:
    """A 4-second 16 kHz mono clip of synthetic speech (macOS `say`), cut or padded to length. No microphone."""
    wav = scratch / "clip.wav"
    subprocess.run(["say", "-o", str(wav), "--data-format=LEI16@16000", STT_TEXT], check=True)
    pcm = wav.read_bytes()[44:]  # the canonical 44-byte header; the rest is PCM16
    want = int(16000 * seconds) * 2
    pcm = pcm[:want].ljust(want, b"\0")
    import wave

    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(pcm)
    return wav, pcm


# ---- benchmarks ------------------------------------------------------------------------------------------------

def bench_omnivoice(args: argparse.Namespace, scratch: Path) -> dict:
    import torch
    from omnivoice import OmniVoice

    path = download("k2-fsa/OmniVoice", scratch)
    meter = Meter()
    t = time.perf_counter()
    model = OmniVoice.from_pretrained(str(path), device_map=args.device, dtype=torch.float16)
    out = {"device": args.device, "load_s": round(time.perf_counter() - t, 2)}
    out["after_load"] = meter.report()

    def say(steps: int) -> tuple[float, float]:
        t = time.perf_counter()
        audio = model.generate(text=SENTENCE, instruct=VOICE_DESIGN, num_step=steps)
        return time.perf_counter() - t, len(audio[0]) / model.sampling_rate

    first, seconds = say(32)
    out["first_32_steps_s"], out["audio_s"] = round(first, 3), round(seconds, 2)
    for steps in (32, 16):
        out[f"warm_{steps}_steps"] = stats([say(steps)[0] for _ in range(args.runs)])
    out["after_runs"] = meter.report()
    return out


def bench_kokoro(args: argparse.Namespace, scratch: Path) -> dict:
    import numpy as np
    import torch
    from kokoro import KModel, KPipeline
    from misaki import espeak

    def no_espeak(*_a, **_k):  # the espeakng-loader wheel is broken on this Mac and kills the process; Kokoro then
        raise RuntimeError("espeak unavailable")  # skips its fallback for words missing from its dictionary

    espeak.EspeakFallback = no_espeak

    src = OLD_MODELS / "kokoro-82m"
    meter = Meter()
    t = time.perf_counter()
    kmodel = KModel(config=str(src / "config.json"), model=str(src / "kokoro-v1_0.pth")).to(args.device).eval()
    pipeline = KPipeline(lang_code="b", model=kmodel)
    out = {"device": args.device, "load_s": round(time.perf_counter() - t, 2), "voice": "bm_george"}
    voice = str(src / "voices" / "bm_george.pt")

    def say() -> tuple[float, float]:
        t = time.perf_counter()
        with torch.no_grad():
            audio = np.concatenate([a.numpy() for _, _, a in pipeline(SENTENCE, voice=voice, speed=1.0)])
        return time.perf_counter() - t, len(audio) / 24000

    first, seconds = say()
    out["first_s"], out["audio_s"] = round(first, 3), round(seconds, 2)
    out["warm"] = stats([say()[0] for _ in range(args.runs)])
    out["after_runs"] = meter.report()
    return out


def bench_stt(args: argparse.Namespace, scratch: Path) -> dict:
    """One engine and size per process, so the memory is that model's own. The download is removed afterwards."""
    import numpy as np

    _, pcm = test_clip(scratch)
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768  # arrays, so neither engine needs ffmpeg or av
    if args.engine == "faster-whisper":
        from faster_whisper import WhisperModel

        path = download(f"Systran/faster-whisper-{args.size}", scratch)
        meter = Meter()
        t = time.perf_counter()
        model = WhisperModel(str(path), device="cpu", compute_type="int8")
        load = time.perf_counter() - t

        def run() -> tuple[float, str]:
            t = time.perf_counter()
            segs, _ = model.transcribe(audio, language="en", beam_size=1)
            text = " ".join(s.text.strip() for s in segs)
            return time.perf_counter() - t, text
    else:
        import mlx_whisper

        path = download(f"mlx-community/whisper-{args.size}-mlx", scratch, ["*.json", "*.npz", "*.safetensors"])
        meter, load = Meter(), None

        def run() -> tuple[float, str]:
            t = time.perf_counter()
            res = mlx_whisper.transcribe(audio, path_or_hf_repo=str(path), language="en")
            return time.perf_counter() - t, res["text"].strip()

    first, heard = run()  # for mlx this includes loading the weights
    out = {"engine": args.engine, "size": args.size, "clip_s": 4.0, "load_s": load and round(load, 2),
           "first_s": round(first, 3), "warm": stats([run()[0] for _ in range(args.runs)]), "heard": heard,
           **meter.report()}
    if args.purge:
        shutil.rmtree(scratch / "hf", ignore_errors=True)
    return out


def bench_brain(args: argparse.Namespace, scratch: Path) -> dict:
    """Time to the first text from the SDK, set up like SdkBrain (no tools, none of Elijah's settings)."""
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import StreamEvent

    async def once(model: str) -> dict:
        options = ClaudeAgentOptions(
            tools=[], setting_sources=[], strict_mcp_config=True, system_prompt="You are Falak. Be brief.",
            model=model, max_turns=1, cwd=scratch, extra_args={"no-session-persistence": None},
            include_partial_messages=True,
        )
        start = time.perf_counter()
        first_message = first_text = None
        async for message in query(prompt=QUESTION, options=options):
            now = time.perf_counter() - start
            first_message = first_message or now
            if isinstance(message, StreamEvent) and first_text is None:
                event = message.event
                if event.get("type") == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
                    first_text = now
        return {"first_message": first_message, "first_text": first_text, "total": time.perf_counter() - start}

    out: dict = {}
    for model in args.models:
        runs = [asyncio.run(once(model)) for _ in range(args.runs)]
        out[model] = {k: stats([r[k] for r in runs if r[k] is not None]) for k in ("first_message", "first_text", "total")}
    return out


BENCHES = {"omnivoice": bench_omnivoice, "kokoro": bench_kokoro, "stt": bench_stt, "brain": bench_brain}


# ---- running them all ------------------------------------------------------------------------------------------

def run_all(args: argparse.Namespace, scratch: Path) -> dict:
    py = args.engines_python or sys.executable
    plan = [
        ("omnivoice", py, ["--device", "mps"]),
        ("kokoro", py, ["--device", "cpu"]),
        ("kokoro", py, ["--device", "mps"]),
        *[("stt", py, ["--engine", e, "--size", z, "--purge"])
          for e in ("faster-whisper", "mlx-whisper") for z in ("base.en", "small.en")],
        ("brain", sys.executable, ["--models", "sonnet", "haiku"]),
    ]
    results: dict = {"machine": machine()}
    env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    for name, python, extra in plan:
        label = f"{name} {' '.join(extra)}"
        print(f"== {label}", file=sys.stderr)
        r = subprocess.run([python, __file__, name, "--scratch", str(scratch), "--runs", str(args.runs), *extra],
                           capture_output=True, text=True, env=env)
        sys.stderr.write(r.stderr[-1500:])
        try:
            results[label] = json.loads(r.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            results[label] = {"error": f"exit {r.returncode}: {r.stderr[-400:]}"}
    results["machine_after"] = machine()
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bench", choices=[*BENCHES, "all"])
    p.add_argument("--scratch")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--device", default="mps")
    p.add_argument("--models", nargs="+", default=["sonnet", "haiku"])
    p.add_argument("--engine", choices=["faster-whisper", "mlx-whisper"], default="faster-whisper")
    p.add_argument("--size", choices=["base.en", "small.en"], default="base.en")
    p.add_argument("--purge", action="store_true", help="stt: delete the downloaded model afterwards")
    p.add_argument("--engines-python", help="python with the engines installed, for `all`")
    args = p.parse_args()
    scratch = scratch_dir(args.scratch)
    result = run_all(args, scratch) if args.bench == "all" else BENCHES[args.bench](args, scratch)
    if args.bench == "all":
        (scratch / "voice-bench.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()

# Voice measurements (M9 section 0)

Measured 2026-10-28 by `scripts/voice_bench.py` on this Mac (8 GB, Apple silicon), with Claude Code, Chrome, Discord,
Spotify, VS Code and `falak start` running as usual. Nothing touched Falak-dev, Falak-node or the old JARVIS folder:
engines ran from a throwaway venv, downloads and test audio went to a scratch folder, and Kokoro and the Whisper
`base.en` weights were only read or re-downloaded.

**Status: OmniVoice is not measured.** The script stopped before downloading it (3.04 GiB needed, 4.76 GiB free, which
would leave 1.7 GiB; the rule is 2 GiB). The decision rule can't be applied until it runs. See "What's needed".

## The machine while measuring

- Swap was **already 3.5–3.9 GB used** before any benchmark started, with Falak running and nothing of ours loaded.
  This Mac is under memory pressure at rest; every number below was taken in that state.
- Free disk 4.8 GiB. Swap files live on the same disk, so heavy memory use also eats disk.
- "RAM" below is `footprint` (what macOS charges the process, including compressed and swapped pages) next to `rss`
  (resident only). On MPS and MLX, `rss` is misleadingly low because the weights sit in GPU-mapped memory.

## Brain (Claude Agent SDK, no tools, one-line question, 3 runs)

Set up like `SdkBrain` (no built-in tools, none of Elijah's settings, no session persistence) plus partial messages.

| Model | first SDK message | **first text** (median) | whole reply |
|---|---|---|---|
| haiku | 0.66 s | **1.53 s** (1.51–1.86) | 2.30 s |
| sonnet | 0.75 s | **1.78 s** (1.71–2.58) | 2.93 s |

About 0.65–0.75 s of that is starting `claude`. Falak's real turns add its tools, so expect a little more. The brain
alone is already most of the 2 s "first audio" target for a no-tool reply, which is why the first sentence must be
spoken while the rest is still being written.

## Speech-to-text (a 4-second clip of synthetic speech, one model per process)

All four transcribed the clip correctly.

| Engine | Model | warm time | first call | footprint | rss |
|---|---|---|---|---|---|
| faster-whisper (int8, CPU) | base.en | 0.47 s | 0.48 s | 600 MB | 640 MB |
| faster-whisper (int8, CPU) | small.en | 1.96 s | 2.01 s | 891 MB | 778 MB |
| mlx-whisper (GPU) | base.en | **0.17 s** | 0.50 s | 960 MB | 515 MB |
| mlx-whisper (GPU) | small.en | 0.61 s | 1.67 s | 1410 MB | 87 MB |

`mlx-whisper` installed cleanly. `small.en` on faster-whisper is too slow for a turn; `base.en` is fine on either.
mlx is about 3x faster at the cost of more charged memory (GPU-mapped).

## Kokoro (`kokoro-82m`, `bm_george`, one 12-word sentence, 4.9 s of audio)

| Device | load | first | **warm (median of 3)** | footprint | rss | swap change |
|---|---|---|---|---|---|---|
| CPU | 1.5 s | 1.14 s | **0.99 s** | 1364 MB | 1042 MB | none |
| MPS | 1.9 s | 1.84 s | **0.75 s** | 2151 MB | 148 MB | +440 MB |

Real-time factor about 0.15–0.20. A first sentence of this length costs about 0.75–1 s; shorter sentences cost less.
On CPU Kokoro doesn't add swap; on MPS it does. Kokoro's British voices need `espeak-ng` for words missing from its
dictionary, and the `espeakng-loader` wheel is broken on this Mac (it kills the process). The bench skips that
fallback, so a rare unknown word is dropped. Fixing it is a section 1 decision (`brew install espeak-ng`, or a
fallback of our own).

## OmniVoice (`k2-fsa/OmniVoice`, MPS, fp16, `"male, low pitch, british accent"`)

Not measured (disk guard). What is known without running it:

- The repo is 3.27 GB (2.45 GB for the model, 0.81 GB for the audio tokenizer), so a download plus the swap it
  provokes won't leave 2 GiB free on this disk.
- The code is Apache-2.0, but the README says **the pre-trained weights are CC-BY-NC** (non-commercial). The brief says
  Apache-2.0. Fine for Elijah's personal use; worth knowing if Falak is ever shared.
- Installing it with `--no-deps` plus torch, torchaudio, transformers>=5.3 and librosa works; Gradio isn't needed.
- It needs `torch>=2.4`. The scratch venv with all engines is 1.4 GB.

## Against the decision rule

| Rule | OmniVoice | Kokoro |
|---|---|---|
| warm sentence ≤ 1 s | not measured | 0.75 s (MPS), 0.99 s (CPU): passes |
| resident RAM ≤ 2.5 GB | not measured | 1.4 GB (CPU), 2.2 GB (MPS): passes |
| no swap with everything open | already swapping 3.6 GB at rest, so this is unlikely | CPU adds none; MPS adds ~0.4 GB |

Kokoro on CPU meets all three. OmniVoice would need to beat 1 s warm in about 2.5 GB on a Mac that is already
swapping, and it would be sharing the machine with Whisper (0.6–1.4 GB) at the same time.

## What's needed to finish section 0

Pick one:

1. **Free about 1 GiB of disk** (the uv cache is 4.2 GB: `uv cache clean`), then rerun
   `scripts/voice_bench.py omnivoice --scratch <scratch>`, and I'll record the numbers and apply the rule.
2. **Choose Kokoro now** (CPU, `bm_george`; OmniVoice stays a setting for the Mac mini) and skip the OmniVoice run.

Not yet measured: whether echo cancellation holds on speakers and AirPods (needs the Swift helper from section 3).

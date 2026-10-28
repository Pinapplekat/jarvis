"""Speech detection for calls: Silero VAD on onnx (the same model the desk's wake word library bundles), speech
probability of a 30 ms frame of 16 kHz PCM16. numpy and onnxruntime come from the `desk` extra."""

from __future__ import annotations

from pathlib import Path

SILERO_URL = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/silero_vad.onnx"
FRAME_SAMPLES = 480


class SileroVad:
    def __init__(self, model_path: Path) -> None:
        import numpy as np
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.inter_op_num_threads = options.intra_op_num_threads = 1
        self._np = np
        self._model = ort.InferenceSession(str(model_path), sess_options=options, providers=["CPUExecutionProvider"])
        self._sr = np.array(16000).astype(np.int64)
        self.reset()

    def reset(self) -> None:
        self._h = self._np.zeros((2, 1, 64), dtype="float32")
        self._c = self._np.zeros((2, 1, 64), dtype="float32")

    def __call__(self, pcm16: bytes) -> float:
        samples = self._np.frombuffer(pcm16, dtype=self._np.int16).astype(self._np.float32) / 32767
        out, self._h, self._c = self._model.run(None, {"input": samples[None, :], "h": self._h, "c": self._c, "sr": self._sr})
        return float(out[0][0])

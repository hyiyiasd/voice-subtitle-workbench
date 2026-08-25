from __future__ import annotations

from pathlib import Path

import numpy as np
import onnxruntime as ort

from .media import SpeechRange, read_pcm16_mono


class SileroVAD:
    def __init__(
        self,
        model_path: Path,
        *,
        threshold: float = 0.5,
        min_speech_ms: int = 250,
        min_silence_ms: int = 500,
        speech_pad_ms: int = 150,
    ) -> None:
        self.session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self.threshold = threshold
        self.min_speech_ms = min_speech_ms
        self.min_silence_ms = min_silence_ms
        self.speech_pad_ms = speech_pad_ms

    def speech_ranges(self, wav_path: Path) -> list[SpeechRange]:
        samples, sample_rate = read_pcm16_mono(wav_path)
        if sample_rate not in (8_000, 16_000):
            raise ValueError("Silero VAD 只接受 8kHz 或 16kHz 音频。")
        window_size = 512 if sample_rate == 16_000 else 256
        context_size = 64 if sample_rate == 16_000 else 32
        state = np.zeros((2, 1, 128), dtype=np.float32)
        context = np.zeros((1, context_size), dtype=np.float32)
        sample_rate_input = np.array(sample_rate, dtype=np.int64)
        probabilities: list[tuple[int, float]] = []
        for offset in range(0, len(samples), window_size):
            chunk = samples[offset : offset + window_size]
            if len(chunk) < window_size:
                chunk.extend([0.0] * (window_size - len(chunk)))
            model_input = np.concatenate((context, np.asarray([chunk], dtype=np.float32)), axis=1)
            output, state = self.session.run(
                None,
                {
                    "input": model_input,
                    "state": state,
                    "sr": sample_rate_input,
                },
            )
            context = model_input[:, -context_size:]
            probabilities.append((offset, float(np.asarray(output).reshape(-1)[0])))
        return self._probabilities_to_ranges(probabilities, sample_rate, len(samples))

    def _probabilities_to_ranges(
        self, values: list[tuple[int, float]], sample_rate: int, total_samples: int
    ) -> list[SpeechRange]:
        silence_samples = round(self.min_silence_ms * sample_rate / 1000)
        minimum_speech = round(self.min_speech_ms * sample_rate / 1000)
        pad_samples = round(self.speech_pad_ms * sample_rate / 1000)
        active_start: int | None = None
        silence_start: int | None = None
        raw_ranges: list[tuple[int, int]] = []
        for offset, probability in values:
            if probability >= self.threshold:
                if active_start is None:
                    active_start = offset
                silence_start = None
            elif active_start is not None:
                if silence_start is None:
                    silence_start = offset
                if offset - silence_start >= silence_samples:
                    if silence_start - active_start >= minimum_speech:
                        raw_ranges.append((active_start, silence_start))
                    active_start = None
                    silence_start = None
        if active_start is not None and total_samples - active_start >= minimum_speech:
            raw_ranges.append((active_start, total_samples))
        padded = [
            SpeechRange(
                start_ms=round(max(0, start - pad_samples) / sample_rate * 1000),
                end_ms=round(min(total_samples, end + pad_samples) / sample_rate * 1000),
            )
            for start, end in raw_ranges
        ]
        return _merge_overlapping(padded)


def _merge_overlapping(values: list[SpeechRange]) -> list[SpeechRange]:
    result: list[SpeechRange] = []
    for value in values:
        if result and value.start_ms <= result[-1].end_ms:
            result[-1] = SpeechRange(result[-1].start_ms, max(result[-1].end_ms, value.end_ms))
        else:
            result.append(value)
    return result

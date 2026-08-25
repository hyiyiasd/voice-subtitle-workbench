from __future__ import annotations

from pathlib import Path

from .domain import ASRCandidate, AudioChunk


class ASRDependencyError(RuntimeError):
    pass


class FasterWhisperProvider:
    id = "faster-whisper"

    def __init__(self, model_path: Path, *, device: str = "cpu", compute_type: str = "int8"):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ASRDependencyError("未安装 faster-whisper 可选组件。") from exc
        if not model_path.is_dir():
            raise FileNotFoundError(f"本地模型不存在：{model_path}")
        self._model = WhisperModel(str(model_path), device=device, compute_type=compute_type)

    def transcribe(self, chunk: AudioChunk, *, model: str) -> list[ASRCandidate]:
        segments, _ = self._model.transcribe(
            chunk.path,
            language=None if chunk.language_hint in (None, "auto") else chunk.language_hint,
            vad_filter=False,
        )
        return [
            ASRCandidate(
                start_ms=chunk.start_ms + round(item.start * 1000),
                end_ms=chunk.start_ms + round(item.end * 1000),
                text=item.text.strip(),
                language=chunk.language_hint or "auto",
                confidence=None,
            )
            for item in segments
            if item.text.strip()
        ]

    def close(self) -> None:
        self._model = None


class ReazonSpeechProvider:
    id = "reazonspeech-k2"

    def __init__(self, model_path: Path, *, language: str = "ja", device: str = "cpu") -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise ASRDependencyError("未安装 sherpa-onnx 运行组件。") from exc
        self.language = language
        epoch = 99 if language == "ja" else 35
        self._model = sherpa_onnx.OfflineRecognizer.from_transducer(
            tokens=str(model_path / "tokens.txt"),
            encoder=str(model_path / f"encoder-epoch-{epoch}-avg-1.int8.onnx"),
            decoder=str(model_path / f"decoder-epoch-{epoch}-avg-1.int8.onnx"),
            joiner=str(model_path / f"joiner-epoch-{epoch}-avg-1.int8.onnx"),
            num_threads=1,
            sample_rate=16_000,
            feature_dim=80,
            decoding_method="greedy_search",
            provider=device,
        )

    def transcribe(self, chunk: AudioChunk, *, model: str) -> list[ASRCandidate]:
        import numpy as np

        from .media import read_pcm16_mono

        samples, sample_rate = read_pcm16_mono(Path(chunk.path))
        stream = self._model.create_stream()
        stream.accept_waveform(sample_rate, np.asarray(samples, dtype=np.float32))
        self._model.decode_stream(stream)
        text = str(stream.result.text).strip()
        if not text:
            return []
        return [
            ASRCandidate(
                start_ms=chunk.start_ms,
                end_ms=chunk.end_ms,
                text=text,
                language=self.language,
            )
        ]

    def close(self) -> None:
        self._model = None

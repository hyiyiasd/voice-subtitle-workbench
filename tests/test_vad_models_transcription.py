from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import pytest

from voice_subtitle_translator.domain import ProjectSettings
from voice_subtitle_translator.media import SpeechRange
from voice_subtitle_translator.model_manager import (
    ModelIntegrityError,
    ModelManager,
    _download_urls,
)
from voice_subtitle_translator.paths import AppPaths
from voice_subtitle_translator.project import Project
from voice_subtitle_translator.transcription import TranscriptionService
from voice_subtitle_translator.vad import SileroVAD


def _paths(root: Path) -> AppPaths:
    data = root / "data"
    result = AppPaths(
        root=root,
        data=data,
        config=data / "config",
        models=data / "models",
        cache=data / "cache",
        logs=data / "logs",
        temp=data / "temp",
        gpu_runtime=data / "gpu-runtime",
    )
    result.ensure()
    return result


def _write_silence(path: Path, duration_ms: int = 1_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\0\0" * round(16_000 * duration_ms / 1_000))


def test_vad_probability_ranges_apply_padding_and_merge() -> None:
    vad = object.__new__(SileroVAD)
    vad.threshold = 0.5
    vad.min_speech_ms = 100
    vad.min_silence_ms = 100
    vad.speech_pad_ms = 50
    values = [
        (0, 0.0),
        (1_600, 0.9),
        (3_200, 0.8),
        (4_800, 0.0),
        (6_400, 0.0),
    ]
    assert vad._probabilities_to_ranges(values, 16_000, 8_000) == [SpeechRange(50, 350)]


def test_model_manager_verifies_size_and_sha(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    payload = b"local model"
    digest = hashlib.sha256(payload).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "tiny",
                        "display_name": "Tiny",
                        "source": "local",
                        "version": "1",
                        "license": "MIT",
                        "size_bytes": len(payload),
                        "languages": ["ja"],
                        "recommended_vram_mb": 0,
                        "runtime": "test",
                        "downloadable": False,
                        "artifacts": [
                            {
                                "relative_path": "model.bin",
                                "url": "https://invalid.example/model.bin",
                                "size_bytes": len(payload),
                                "sha256": digest,
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manager = ModelManager(paths, manifest)
    target = manager.model_path("tiny") / "model.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    manager.verify("tiny")
    target.write_bytes(b"tampered!!")
    with pytest.raises(ModelIntegrityError):
        manager.verify("tiny")


def test_bundled_manifest_has_descriptions_and_consistent_sizes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    manifest = Path(__file__).parents[1] / "models" / "manifest.json"
    manager = ModelManager(paths, manifest)
    assert {
        "faster-whisper-tiny",
        "faster-whisper-base",
        "faster-whisper-small",
        "faster-whisper-medium",
        "faster-whisper-large-v3-turbo",
    }.issubset(manager.models)
    for model in manager.models.values():
        assert model.recommendation
        assert len(model.description) >= 40
        if model.artifacts:
            assert model.descriptor.size_bytes == sum(
                artifact.size_bytes for artifact in model.artifacts
            )
    tiny = manager.models["faster-whisper-tiny"]
    tiny_weights = next(
        artifact for artifact in tiny.artifacts if artifact.relative_path == "model.bin"
    )
    urls = _download_urls(tiny, tiny_weights)
    assert urls[0].startswith("https://modelscope.cn/models/Systran/")
    assert urls[1].startswith("https://hf-mirror.com/Systran/")
    assert urls[-1].startswith("https://huggingface.co/Systran/")


def test_transcription_saves_batch_and_removes_chunk(tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    project_path = tmp_path / "sample.vstproj"
    media = tmp_path / "sample.media"
    media.write_bytes(b"owned test media")
    ffmpeg = tmp_path / "runtime" / "ffmpeg.exe"
    ffmpeg.parent.mkdir()
    ffmpeg.write_bytes(b"placeholder")

    class FakeManager:
        def verify(self, model_id: str) -> None:
            assert model_id in {"silero-vad-v6", "reazonspeech-k2-ja"}

        def model_path(self, model_id: str) -> Path:
            value = paths.models / model_id
            value.mkdir(parents=True, exist_ok=True)
            return value

    class FakeWorker:
        instances = 0

        def __init__(self, *, cwd: Path) -> None:
            assert cwd == paths.root
            self.index = FakeWorker.instances
            FakeWorker.instances += 1

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def call(self, method: str, params: dict) -> dict:
            if method == "vad.detect":
                assert self.index == 0
                assert Path(params["model_path"]).name == "silero_vad.onnx"
                assert Path(params["wav_path"]).is_file()
                return {"ranges": [{"start_ms": 100, "end_ms": 800}]}
            assert method == "asr.transcribe"
            assert self.index == 1
            assert Path(params["chunk"]["path"]).is_file()
            return {
                "candidates": [
                    {
                        "start_ms": 100,
                        "end_ms": 800,
                        "text": "テスト",
                        "language": "ja",
                        "confidence": 0.9,
                    }
                ]
            }

    def fake_normalize(_ffmpeg: Path, _source: Path, destination: Path) -> None:
        _write_silence(destination)

    monkeypatch.setattr("voice_subtitle_translator.transcription.normalize_audio", fake_normalize)
    monkeypatch.setattr("voice_subtitle_translator.transcription.WorkerClient", FakeWorker)

    with Project.create(project_path, ProjectSettings()) as project:
        project.set_media(media)
        task_id = TranscriptionService(
            project,
            paths=paths,
            model_manager=FakeManager(),  # type: ignore[arg-type]
            ffmpeg_path=ffmpeg,
        ).run(model_id="reazonspeech-k2-ja")
        assert project.task(task_id)["status"] == "completed"
        assert project.task(task_id)["completed_batches"] == 1
        assert [item.source_text for item in project.list_segments()] == ["テスト"]
    assert not list(paths.temp.glob(f"{task_id}-*.wav"))

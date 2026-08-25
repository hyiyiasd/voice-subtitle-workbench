from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .domain import ASRCandidate, AudioChunk, TaskKind, TaskStatus
from .media import SpeechRange, extract_wav_range, merge_speech_ranges, normalize_audio
from .model_manager import ModelManager
from .paths import AppPaths
from .project import Project, quick_file_fingerprint
from .quality import apply_quality_flags
from .worker_client import WorkerClient


class TranscriptionService:
    def __init__(
        self,
        project: Project,
        *,
        paths: AppPaths,
        model_manager: ModelManager,
        ffmpeg_path: Path,
    ) -> None:
        self.project = project
        self.paths = paths
        self.model_manager = model_manager
        self.ffmpeg_path = ffmpeg_path

    def run(
        self,
        *,
        model_id: str,
        device: str = "cpu",
        compute_type: str | None = None,
    ) -> str:
        media = self.project.resolve_media()
        if media is None:
            raise FileNotFoundError("项目没有可用的媒体文件，请先重新定位媒体。")
        self.model_manager.verify("silero-vad-v6")
        self.model_manager.verify(model_id)
        settings = self.project.get_settings()
        normalized = self.paths.cache / "audio" / f"{quick_file_fingerprint(media)}.wav"
        if not normalized.is_file():
            normalize_audio(self.ffmpeg_path, media, normalized)
        with WorkerClient(cwd=self.paths.root) as vad_worker:
            vad_result = vad_worker.call(
                "vad.detect",
                {
                    "model_path": str(
                        self.model_manager.model_path("silero-vad-v6") / "silero_vad.onnx"
                    ),
                    "wav_path": str(normalized),
                },
            )
        ranges = merge_speech_ranges(
            [SpeechRange(**value) for value in vad_result["ranges"]]
        )
        task_id = self.project.create_task(
            TaskKind.TRANSCRIBE,
            total_batches=len(ranges),
            payload={"media": str(media), "model_id": model_id},
        )
        self.project.set_task_status(task_id, TaskStatus.RUNNING)
        provider_type = (
            "reazonspeech-k2" if model_id.startswith("reazonspeech") else "faster-whisper"
        )
        language = "ja-en" if model_id.endswith("ja-en") else settings.source_language
        selected_compute_type = compute_type or (
            "int8_float16" if device == "cuda" else "int8"
        )
        try:
            with WorkerClient(cwd=self.paths.root) as worker:
                for index, speech_range in enumerate(ranges):
                    chunk_path = self.paths.temp / f"{task_id}-{index:06d}.wav"
                    try:
                        extract_wav_range(normalized, chunk_path, speech_range)
                        chunk = AudioChunk(
                            id=f"{task_id}:{index}",
                            path=str(chunk_path),
                            start_ms=speech_range.start_ms,
                            end_ms=speech_range.end_ms,
                            language_hint=language,
                        )
                        response = worker.call(
                            "asr.transcribe",
                            {
                                "provider": provider_type,
                                "model_id": model_id,
                                "model_path": str(self.model_manager.model_path(model_id)),
                                "language": language,
                                "device": device,
                                "compute_type": selected_compute_type,
                                "chunk": asdict(chunk),
                            },
                        )
                    finally:
                        chunk_path.unlink(missing_ok=True)
                    candidates = [
                        ASRCandidate(**candidate) for candidate in response["candidates"]
                    ]
                    created = self.project.apply_asr_candidates(
                        candidates, provider=provider_type, model=model_id
                    )
                    self.project.complete_batch(
                        task_id,
                        index,
                        {"segment_ids": [segment.id for segment in created]},
                    )
            self._save_quality_flags()
            self.project.set_task_status(task_id, TaskStatus.COMPLETED)
            return task_id
        except Exception as exc:
            self.project.set_task_status(task_id, TaskStatus.FAILED, str(exc))
            raise

    def _save_quality_flags(self) -> None:
        segments = self.project.list_segments()
        apply_quality_flags(segments, self.project.get_settings().subtitle)
        with self.project.connection:
            for segment in segments:
                self.project.connection.execute(
                    "UPDATE segments SET quality_flags_json=? WHERE id=?",
                    (json.dumps(sorted(segment.quality_flags), ensure_ascii=False), segment.id),
                )

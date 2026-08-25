from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from .domain import ASRCandidate, AudioChunk, TaskKind, TaskStatus
from .media import SpeechRange, extract_wav_range, merge_speech_ranges, normalize_audio
from .model_manager import ModelManager
from .paths import AppPaths
from .project import Project, quick_file_fingerprint
from .quality import apply_quality_flags
from .worker_client import WorkerClient


class TranscriptionCancelledError(RuntimeError):
    pass


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
        on_progress: Callable[[str, int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_worker_change: Callable[[WorkerClient | None], None] | None = None,
    ) -> str:
        def check_stop() -> None:
            if should_stop and should_stop():
                raise TranscriptionCancelledError("识别任务已被用户强制暂停。")

        def report(stage: str, completed: int) -> None:
            if on_progress:
                on_progress(stage, completed, 100)

        check_stop()
        media = self.project.resolve_media()
        if media is None:
            raise FileNotFoundError("项目没有可用的媒体文件，请先重新定位媒体。")
        self.model_manager.verify("silero-vad-v6")
        self.model_manager.verify(model_id)
        settings = self.project.get_settings()
        normalized = self.paths.cache / "audio" / f"{quick_file_fingerprint(media)}.wav"
        report("正在标准化音频", 3)
        if not normalized.is_file():
            normalize_audio(self.ffmpeg_path, media, normalized)
        check_stop()
        report("音频标准化完成，正在检测语音区间", 12)
        try:
            with WorkerClient(cwd=self.paths.root) as vad_worker:
                if on_worker_change:
                    on_worker_change(vad_worker)
                vad_result = vad_worker.call(
                    "vad.detect",
                    {
                        "model_path": str(
                            self.model_manager.model_path("silero-vad-v6")
                            / "silero_vad.onnx"
                        ),
                        "wav_path": str(normalized),
                    },
                )
        except Exception as exc:
            if should_stop and should_stop():
                raise TranscriptionCancelledError(
                    "识别任务已被用户强制暂停。"
                ) from exc
            raise
        finally:
            if on_worker_change:
                on_worker_change(None)
        check_stop()
        ranges = merge_speech_ranges(
            [SpeechRange(**value) for value in vad_result["ranges"]]
        )
        report(f"语音检测完成：{len(ranges)} 个片段", 20)
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
                if on_worker_change:
                    on_worker_change(worker)
                for index, speech_range in enumerate(ranges):
                    check_stop()
                    recognition_progress = 20 + round(
                        index / max(len(ranges), 1) * 75
                    )
                    report(
                        f"正在识别片段 {index + 1}/{len(ranges)}",
                        recognition_progress,
                    )
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
                        check_stop()
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
                    report(
                        f"已识别片段 {index + 1}/{len(ranges)}",
                        20 + round((index + 1) / max(len(ranges), 1) * 75),
                    )
            if on_worker_change:
                on_worker_change(None)
            check_stop()
            report("正在检查字幕质量并保存", 97)
            self._save_quality_flags()
            self.project.set_task_status(task_id, TaskStatus.COMPLETED)
            report("识别完成", 100)
            return task_id
        except Exception as exc:
            if on_worker_change:
                on_worker_change(None)
            if isinstance(exc, TranscriptionCancelledError) or (
                should_stop and should_stop()
            ):
                self.project.set_task_status(task_id, TaskStatus.INTERRUPTED, str(exc))
                if isinstance(exc, TranscriptionCancelledError):
                    raise
                raise TranscriptionCancelledError("识别任务已被用户强制暂停。") from exc
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

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .cache import translation_cache_key
from .domain import (
    ProjectSettings,
    Segment,
    TaskKind,
    TaskStatus,
    TranslationItem,
    TranslationResult,
)
from .project import Project
from .providers.base import TranslationProvider
from .providers.common import TranslationResponseError


@dataclass(slots=True)
class TranslationRunResult:
    task_id: str
    completed: int = 0
    cached: int = 0
    stopped_by_switch: bool = False


class PipelineCoordinator:
    def __init__(self, project: Project) -> None:
        self.project = project

    def set_translation_enabled(self, enabled: bool) -> None:
        settings = self.project.get_settings()
        settings.translation_enabled = enabled
        self.project.save_settings(settings)

    def translate_pending(
        self,
        provider: TranslationProvider,
        *,
        prompt: str = "",
        context: str = "",
        glossary: list[tuple[str, str]] | None = None,
        batch_size: int = 20,
        provider_parameters: dict | None = None,
        on_batch_complete: Callable[[int, int], None] | None = None,
    ) -> TranslationRunResult:
        settings = self.project.get_settings()
        if not settings.translation_enabled:
            raise RuntimeError("当前项目未启用翻译。")
        pending = [
            segment
            for segment in self.project.list_segments()
            if not segment.has_valid_translation
        ]
        batches = [
            pending[index : index + batch_size]
            for index in range(0, len(pending), batch_size)
        ]
        task_id = self.project.create_task(TaskKind.TRANSLATE, total_batches=len(batches))
        result = TranslationRunResult(task_id=task_id)
        self.project.set_task_status(task_id, TaskStatus.RUNNING)
        try:
            for batch_index, batch in enumerate(batches):
                if not self.project.get_settings().translation_enabled:
                    result.stopped_by_switch = True
                    self.project.set_task_status(task_id, TaskStatus.PAUSED)
                    break
                completed, cached = self._translate_batch_with_split(
                    provider,
                    batch,
                    settings=settings,
                    prompt=prompt,
                    context=context,
                    glossary=glossary or [],
                    provider_parameters=provider_parameters or {},
                )
                result.completed += completed
                result.cached += cached
                self.project.complete_batch(
                    task_id,
                    batch_index,
                    {"segment_ids": [segment.id for segment in batch]},
                )
                if on_batch_complete:
                    on_batch_complete(batch_index + 1, len(batches))
            else:
                self.project.set_task_status(task_id, TaskStatus.COMPLETED)
        except Exception as exc:
            self.project.set_task_status(task_id, TaskStatus.FAILED, str(exc))
            raise
        return result

    def _translate_batch_with_split(
        self,
        provider: TranslationProvider,
        segments: list[Segment],
        *,
        settings: ProjectSettings,
        prompt: str,
        context: str,
        glossary: list[tuple[str, str]],
        provider_parameters: dict,
    ) -> tuple[int, int]:
        uncached: list[tuple[Segment, str]] = []
        cached_count = 0
        for segment in segments:
            key = translation_cache_key(
                source_text=segment.source_text,
                source_revision=segment.source_revision,
                model=settings.translation_model,
                target_language=settings.target_language,
                prompt=prompt,
                glossary=glossary,
                provider_parameters=provider_parameters,
            )
            cached = self.project.cached_translation(key)
            if cached is not None:
                self.project.save_translation(segment.id, cached, key)
                cached_count += 1
            else:
                uncached.append((segment, key))
        if not uncached:
            return 0, cached_count
        try:
            translated = provider.translate(
                [TranslationItem(segment.id, segment.source_text) for segment, _ in uncached],
                target_language=settings.target_language,
                model=settings.translation_model,
                context=context,
                glossary=glossary,
                style_instruction=prompt,
            )
            by_id: dict[str, TranslationResult] = {item.segment_id: item for item in translated}
            for segment, key in uncached:
                self.project.save_translation(segment.id, by_id[segment.id].translated_text, key)
            return len(uncached), cached_count
        except (TranslationResponseError, KeyError):
            if len(uncached) == 1:
                raise
            middle = len(uncached) // 2
            first = self._translate_batch_with_split(
                provider,
                [item[0] for item in uncached[:middle]],
                settings=settings,
                prompt=prompt,
                context=context,
                glossary=glossary,
                provider_parameters=provider_parameters,
            )
            second = self._translate_batch_with_split(
                provider,
                [item[0] for item in uncached[middle:]],
                settings=settings,
                prompt=prompt,
                context=context,
                glossary=glossary,
                provider_parameters=provider_parameters,
            )
            return first[0] + second[0], cached_count + first[1] + second[1]

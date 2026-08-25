from __future__ import annotations

from pathlib import Path

import pytest

from voice_subtitle_translator.domain import (
    ProjectSettings,
    ProviderCapabilities,
    Segment,
    TranslationItem,
    TranslationResult,
)
from voice_subtitle_translator.pipeline import PipelineCoordinator
from voice_subtitle_translator.project import Project
from voice_subtitle_translator.providers.common import (
    TranslationResponseError,
    parse_translation_response,
)


class FakeProvider:
    id = "fake"
    capabilities = ProviderCapabilities(structured_output=True)

    def __init__(self) -> None:
        self.calls = 0

    def translate(self, items: list[TranslationItem], **kwargs) -> list[TranslationResult]:
        self.calls += 1
        return [TranslationResult(item.segment_id, "译：" + item.source_text) for item in items]

    def close(self) -> None:
        pass


def test_response_validation_rejects_duplicate_missing_and_unknown_ids() -> None:
    with pytest.raises(TranslationResponseError, match="重复"):
        parse_translation_response(
            '{"translations":[{"id":"a","text":"1"},{"id":"a","text":"2"}]}',
            ["a"],
        )
    with pytest.raises(TranslationResponseError, match="缺少"):
        parse_translation_response('{"translations":[]}', ["a"])
    with pytest.raises(TranslationResponseError, match="未知"):
        parse_translation_response('{"translations":[{"id":"b","text":"1"}]}', ["a"])


def test_switch_off_stops_new_batches_but_keeps_completed_results(tmp_path: Path) -> None:
    settings = ProjectSettings(
        translation_enabled=True,
        translation_model="fake-model",
    )
    with Project.create(tmp_path / "demo.vstproj", settings) as project:
        project.add_segments(
            [
                Segment(
                    order_key=index,
                    start_ms=index * 1000,
                    end_ms=(index + 1) * 1000,
                    source_text=str(index),
                )
                for index in range(3)
            ]
        )
        coordinator = PipelineCoordinator(project)
        result = coordinator.translate_pending(
            FakeProvider(),
            batch_size=1,
            on_batch_complete=lambda done, total: coordinator.set_translation_enabled(False)
            if done == 1
            else None,
        )
        segments = project.list_segments()
        assert result.stopped_by_switch
        assert segments[0].has_valid_translation
        assert not segments[1].has_valid_translation
        assert segments[0].translated_text == "译：0"


def test_reenable_translates_only_invalid_or_missing(tmp_path: Path) -> None:
    settings = ProjectSettings(translation_enabled=True, translation_model="fake")
    provider = FakeProvider()
    with Project.create(tmp_path / "demo.vstproj", settings) as project:
        first = Segment(order_key=0, start_ms=0, end_ms=1000, source_text="a")
        second = Segment(order_key=1, start_ms=1000, end_ms=2000, source_text="b")
        project.add_segments([first, second])
        project.save_translation(first.id, "已有")
        result = PipelineCoordinator(project).translate_pending(provider)
        assert result.completed == 1
        assert project.get_segment(first.id).translated_text == "已有"

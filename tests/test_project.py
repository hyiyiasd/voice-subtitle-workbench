from __future__ import annotations

from pathlib import Path

from voice_subtitle_translator.domain import (
    ASRCandidate,
    ProjectSettings,
    Segment,
    TaskKind,
    TaskStatus,
)
from voice_subtitle_translator.project import Project


def test_new_project_defaults_to_transcribe_only(tmp_path: Path) -> None:
    with Project.create(tmp_path / "demo.vstproj") as project:
        assert project.get_settings().translation_enabled is False
        assert project.get_settings().pipeline_mode.value == "transcribe_only"


def test_existing_project_keeps_its_translation_setting(tmp_path: Path) -> None:
    path = tmp_path / "demo.vstproj"
    with Project.create(path, ProjectSettings(translation_enabled=True)):
        pass
    with Project.open(path) as project:
        assert project.get_settings().translation_enabled is True


def test_manual_edit_invalidates_but_preserves_translation(tmp_path: Path) -> None:
    with Project.create(tmp_path / "demo.vstproj") as project:
        segment = Segment(start_ms=0, end_ms=2_000, source_text="こんにちは")
        project.add_segment(segment)
        project.save_translation(segment.id, "你好")
        assert project.get_segment(segment.id).has_valid_translation
        edited = project.update_source_text(segment.id, "こんばんは")
        assert edited.translated_text == "你好"
        assert not edited.has_valid_translation
        assert edited.human_locked


def test_locked_segment_is_not_overwritten_by_asr(tmp_path: Path) -> None:
    with Project.create(tmp_path / "demo.vstproj") as project:
        locked = Segment(
            start_ms=0,
            end_ms=2_000,
            source_text="人工文本",
            human_locked=True,
        )
        project.add_segment(locked)
        project.apply_asr_candidates(
            [ASRCandidate(0, 2_000, "模型文本", "ja")],
            provider="test",
            model="mock",
            replace_segment_ids=[locked.id],
        )
        assert project.get_segment(locked.id).source_text == "人工文本"
        count = project.connection.execute("SELECT COUNT(*) FROM segment_candidates").fetchone()[0]
        assert count == 1


def test_running_task_becomes_interrupted_when_project_reopens(tmp_path: Path) -> None:
    path = tmp_path / "demo.vstproj"
    with Project.create(path) as project:
        task_id = project.create_task(TaskKind.TRANSCRIBE, total_batches=3)
        project.set_task_status(task_id, TaskStatus.RUNNING)
        project.complete_batch(task_id, 0, {"ok": True})
    with Project.open(path) as project:
        task = project.task(task_id)
        assert task["status"] == TaskStatus.INTERRUPTED.value
        assert task["completed_batches"] == 1


def test_media_can_be_resolved_relative_to_project(tmp_path: Path) -> None:
    media = tmp_path / "audio.wav"
    media.write_bytes(b"fake wave")
    with Project.create(tmp_path / "demo.vstproj") as project:
        project.set_media(media)
        assert project.resolve_media() == media.resolve()


def test_prompt_and_glossary_are_stored_in_project(tmp_path: Path) -> None:
    with Project.create(tmp_path / "demo.vstproj") as project:
        project.save_active_prompt("保持角色语气")
        project.save_glossary([("杏菜", "Anna"), ("部長", "部长")])
        assert project.active_prompt() == "保持角色语气"
        assert project.glossary() == [("杏菜", "Anna"), ("部長", "部长")]

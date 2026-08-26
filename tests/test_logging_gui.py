from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from voice_subtitle_translator.domain import Segment
from voice_subtitle_translator.gpu_runtime import GPURuntimeManager
from voice_subtitle_translator.gui.batch_operation_dialog import BatchOperationDialog
from voice_subtitle_translator.gui.gpu_settings_dialog import GPUSettingsDialog
from voice_subtitle_translator.gui.main_window import MainWindow
from voice_subtitle_translator.gui.model_manager_dialog import (
    ModelManagerDialog,
    _format_size,
)
from voice_subtitle_translator.logging_utils import redact
from voice_subtitle_translator.model_manager import ModelManager
from voice_subtitle_translator.paths import AppPaths
from voice_subtitle_translator.project import Project, quick_file_fingerprint
from voice_subtitle_translator.subtitles import ExportContent, parse_srt


def _paths(root: Path) -> AppPaths:
    data = root / "data"
    return AppPaths(
        root=root,
        data=data,
        config=data / "config",
        models=data / "models",
        cache=data / "cache",
        logs=data / "logs",
        temp=data / "temp",
        gpu_runtime=data / "gpu-runtime",
    )


def test_secrets_are_redacted() -> None:
    value = redact("Authorization: Bearer secret-value api_key=abcdef sk-abcdefghijklmnop")
    assert "secret-value" not in value
    assert "abcdef" not in value
    assert "sk-abcdefghijklmnop" not in value


def test_model_size_uses_every_binary_unit() -> None:
    assert _format_size(2_327_524) == "2.2 MB"
    assert _format_size(160_372_200) == "152.9 MB"
    assert _format_size(1_530_571_713) == "1.4 GB"


def test_model_manager_shows_detailed_introduction(qtbot, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    manifest = Path(__file__).parents[1] / "models" / "manifest.json"
    dialog = ModelManagerDialog(ModelManager(paths, manifest), offline=False)
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 11
    assert "这不是转文字模型" in dialog.details.toPlainText()
    assert str(paths.models.resolve()) in dialog.status.text()
    assert str((paths.models / "silero-vad-v6").resolve()) in dialog.details.toPlainText()
    dialog.table.selectRow(3)
    assert "尚未集成 NeMo/PyTorch" in dialog.details.toPlainText()
    assert not dialog.download_button.isEnabled()
    dialog.table.selectRow(5)
    assert "modelscope.cn → hf-mirror.com → huggingface.co" in dialog.details.toPlainText()


def test_model_manager_progress_supports_packages_larger_than_two_gb(
    qtbot, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    manifest = Path(__file__).parents[1] / "models" / "manifest.json"
    dialog = ModelManagerDialog(ModelManager(paths, manifest), offline=False)
    qtbot.addWidget(dialog)
    dialog._download_progress(1_545_417_851, 3_090_835_702)
    assert dialog.progress_bar.maximum() == 10_000
    assert dialog.progress_bar.value() == 5_000
    assert "GB" in dialog.progress_bar.format()


def test_gpu_settings_explain_rtx50_and_green_runtime(qtbot, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    dialog = GPUSettingsDialog(
        GPURuntimeManager(paths),
        profile_id="rtx50_int8_float16",
        offline=False,
    )
    qtbot.addWidget(dialog)
    assert dialog.profile_combo.count() == 5
    assert "RTX 50" in dialog.profile_combo.currentText()
    assert "Blackwell" in dialog.details.toPlainText()
    assert "1.27 GB" in dialog.runtime_status.text()


def test_main_window_starts_without_libmpv(qtbot, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    window = MainWindow(paths)
    qtbot.addWidget(window)
    assert "当前：第一步仅生成原文 SRT" == window.workflow_label.text()
    assert window.translation_toggle.isChecked() is False
    toolbar_labels = [action.text() for action in window.toolbar.actions() if action.text()]
    assert toolbar_labels == [
        "添加媒体",
        "导入文件夹",
        "批量操作…",
        "开始识别",
        "翻译原文 SRT",
        "强制暂停",
        "导出字幕",
    ]
    menu_labels = [action.text() for action in window.menuBar().actions()]
    assert menu_labels == ["文件", "处理", "设置", "窗口", "帮助"]
    assert window.task_tree.columnCount() == 1
    assert window.task_dock.toggleViewAction().text() == "任务队列"
    assert window.side_dock.toggleViewAction().text() == "翻译上下文"
    assert not hasattr(window, "detail_log")
    window.close()


def test_dropping_media_creates_green_project_and_starts_recognition(qtbot, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    media = tmp_path / "节目.mp3"
    media.write_bytes(b"test-owned-media")
    window = MainWindow(paths)
    qtbot.addWidget(window)
    calls: list[bool] = []
    window.transcribe_media = lambda *, automatic=False: calls.append(automatic)  # type: ignore[method-assign]
    window._ingest_media(media)
    qtbot.waitUntil(lambda: bool(calls))
    assert calls == [True]
    assert window.project is not None
    assert window.project.path.parent == paths.data / "state"
    assert window.project.path.suffix == ".sqlite3"
    assert window.project.resolve_media() == media.resolve()
    window.close()


def test_folder_media_wait_for_manual_operation_then_process_in_order(
    qtbot, tmp_path: Path
) -> None:
    paths = _paths(tmp_path / "portable")
    paths.ensure()
    media_root = tmp_path / "节目文件夹"
    media_root.mkdir()
    media_files = [media_root / "第一话.mp3", media_root / "第二话.wav"]
    for media in media_files:
        media.write_bytes(b"test-owned-media")
    window = MainWindow(paths)
    qtbot.addWidget(window)
    processed: list[Path] = []

    def finish_immediately(media: Path) -> None:
        processed.append(media)
        window._finish_current_media("已完成")

    window._ingest_media = finish_immediately  # type: ignore[method-assign]
    window._enqueue_media_files(media_files, media_root)
    qtbot.wait(20)
    assert processed == []
    group = window.task_tree.topLevelItem(0)
    assert [window.task_status[media.resolve()] for media in media_files] == [
        "等待选择操作",
        "等待选择操作",
    ]
    first_item = window.task_items[media_files[0].resolve()]
    assert first_item.icon(0).isNull()
    assert window.task_tree.columnCount() == 1
    window._set_media_status(media_files[0], "正在识别", progress=50)
    assert not first_item.icon(0).isNull()
    assert window.task_progress[media_files[0].resolve()] == 50
    window._set_media_status(
        media_files[0], "识别完成", progress=100, show_progress=False
    )
    assert first_item.icon(0).isNull()
    window._queue_operations(media_files, "transcribe")
    qtbot.waitUntil(lambda: len(processed) == 2)
    assert processed == [media.resolve() for media in media_files]
    assert window.task_tree.topLevelItemCount() == 1
    assert group.childCount() == 2
    assert [window.task_status[media.resolve()] for media in media_files] == [
        "已完成",
        "已完成",
    ]
    assert group.checkState(0) == Qt.CheckState.Checked
    for index in range(2):
        child = group.child(index)
        assert child.checkState(0) == Qt.CheckState.Checked
        assert child.icon(0).isNull()
    window.close()


def test_gui_writes_source_srt_without_exposing_project_file(qtbot, tmp_path: Path) -> None:
    paths = _paths(tmp_path / "portable")
    paths.ensure()
    media = tmp_path / "节目.mp3"
    media.write_bytes(b"owned-media")
    state = paths.data / "state" / "internal.sqlite3"
    project = Project.create_state(state)
    project.set_media(media)
    project.add_segment(
        Segment(order_key=0, start_ms=1_000, end_ms=2_500, source_text="こんにちは")
    )
    window = MainWindow(paths)
    qtbot.addWidget(window)
    window._set_project(project)
    output = window._write_current_srt(ExportContent.SOURCE)
    assert output == (paths.data / "subtitles" / "原文" / "节目.srt").resolve()
    assert parse_srt(output)[0].source_text == "こんにちは"
    assert project.path.suffix == ".sqlite3"
    window.close()


def test_enabling_translation_waits_for_explicit_second_step(qtbot, tmp_path: Path) -> None:
    paths = _paths(tmp_path / "portable")
    paths.ensure()
    project = Project.create_state(paths.data / "state" / "internal.sqlite3")
    project.add_segment(
        Segment(order_key=0, start_ms=1_000, end_ms=2_500, source_text="こんにちは")
    )
    window = MainWindow(paths)
    qtbot.addWidget(window)
    window._set_project(project)
    calls: list[bool] = []
    window.translate_pending = lambda: calls.append(True)  # type: ignore[method-assign]
    window.translation_toggle.setChecked(True)
    qtbot.wait(20)
    assert calls == []
    assert project.get_settings().translation_enabled is True
    window.close()


def test_legacy_project_migration_keeps_backup_and_discards_duplicate_timeline(
    qtbot, tmp_path: Path
) -> None:
    paths = _paths(tmp_path / "portable")
    paths.ensure()
    media = tmp_path / "episode.mp3"
    media.write_bytes(b"owned-media")
    fingerprint = quick_file_fingerprint(media)
    legacy = paths.data / "projects" / f"episode-{fingerprint[:12]}.vstproj"
    with Project.create(legacy) as project:
        project.set_media(media)
        project.add_segment(
            Segment(order_key=0, start_ms=1_000, end_ms=2_000, source_text="first")
        )
        project.add_segment(
            Segment(order_key=1, start_ms=5_000, end_ms=6_000, source_text="end")
        )
        project.add_segment(
            Segment(order_key=2, start_ms=1_000, end_ms=2_000, source_text="duplicate")
        )
    window = MainWindow(paths)
    qtbot.addWidget(window)
    assert window._load_media_project(media)
    assert window.project is not None
    assert window.project.path.parent == paths.data / "state"
    assert window.project.list_segments() == []
    assert window.project.get_meta("source_srt_invalid") == "1"
    assert legacy.is_file()
    window.close()


def test_batch_dialog_supports_folder_and_individual_selection(qtbot, tmp_path: Path) -> None:
    root = tmp_path / "节目"
    media = [(root / "01.mp3", True), (root / "02.wav", True)]
    dialog = BatchOperationDialog([(root, media)])
    qtbot.addWidget(dialog)
    group = dialog.tree.topLevelItem(0)
    assert len(dialog.selected_paths()) == 2
    group.child(1).setCheckState(0, Qt.CheckState.Unchecked)
    assert dialog.selected_paths() == [media[0][0].resolve()]
    dialog._set_all(Qt.CheckState.Unchecked)
    assert dialog.selected_paths() == []


def test_export_uses_media_name_in_dedicated_srt_folder(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    paths = _paths(tmp_path / "portable")
    paths.ensure()
    media = tmp_path / "节目.mp3"
    media.write_bytes(b"test-owned-media")
    window = MainWindow(paths)
    qtbot.addWidget(window)
    assert window._load_media_project(media)
    assert window.project is not None
    window.project.add_segment(Segment(start_ms=0, end_ms=1000, source_text="字幕"))
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: None)
    window.export_dialog()
    output = paths.data / "subtitles" / "原文" / "节目.srt"
    assert output.is_file()
    assert "字幕" in output.read_text(encoding="utf-8-sig")
    window.close()

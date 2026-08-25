from pathlib import Path

from PySide6.QtCore import Qt

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
    assert dialog.table.rowCount() == 8
    assert "这不是转文字模型" in dialog.details.toPlainText()
    assert str(paths.models.resolve()) in dialog.status.text()
    assert str((paths.models / "silero-vad-v6").resolve()) in dialog.details.toPlainText()
    dialog.table.selectRow(3)
    assert "modelscope.cn → hf-mirror.com → huggingface.co" in dialog.details.toPlainText()


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
    assert "当前：仅识别并导出原文字幕" == window.workflow_label.text()
    assert window.translation_toggle.isChecked() is False
    toolbar_labels = [action.text() for action in window.toolbar.actions() if action.text()]
    assert toolbar_labels == [
        "添加媒体",
        "导入文件夹",
        "批量操作…",
        "开始识别",
        "导出字幕",
    ]
    menu_labels = [action.text() for action in window.menuBar().actions()]
    assert menu_labels == ["项目", "处理", "设置", "帮助"]
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
    assert window.project.path.parent == paths.data / "projects"
    assert window.project.resolve_media() == media.resolve()
    window.close()


def test_folder_media_are_grouped_and_processed_in_order(qtbot, tmp_path: Path) -> None:
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
    qtbot.waitUntil(lambda: len(processed) == 2)
    assert processed == [media.resolve() for media in media_files]
    assert window.task_tree.topLevelItemCount() == 1
    group = window.task_tree.topLevelItem(0)
    assert group.childCount() == 2
    assert [group.child(index).text(1) for index in range(2)] == ["已完成", "已完成"]
    assert group.checkState(0) == Qt.CheckState.Checked
    for index in range(2):
        child = group.child(index)
        assert child.checkState(0) == Qt.CheckState.Checked
        assert window.task_tree.itemWidget(child, 2) is not None
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

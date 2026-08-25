from pathlib import Path

from voice_subtitle_translator.gui.main_window import MainWindow
from voice_subtitle_translator.logging_utils import redact
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


def test_main_window_starts_without_libmpv(qtbot, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    window = MainWindow(paths)
    qtbot.addWidget(window)
    assert "当前：仅识别并导出原文字幕" == window.workflow_label.text()
    assert window.translation_toggle.isChecked() is False
    window.close()


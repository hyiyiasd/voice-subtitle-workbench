from __future__ import annotations

from html import escape

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from voice_subtitle_translator.gpu_runtime import (
    GPU_PROFILES,
    GPURuntimeManager,
    selected_profile,
)


class RuntimeDownloadThread(QThread):
    progress = Signal(int, int)
    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, manager: GPURuntimeManager, *, offline: bool, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.offline = offline

    def run(self) -> None:
        try:
            self.manager.install(
                offline=self.offline,
                on_progress=lambda done, total: self.progress.emit(done, total),
            )
            self.succeeded.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class GPUSettingsDialog(QDialog):
    def __init__(
        self,
        manager: GPURuntimeManager,
        *,
        profile_id: str,
        offline: bool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.manager = manager
        self.offline = offline
        self.download_thread: RuntimeDownloadThread | None = None
        self.setWindowTitle("GPU 推理设置")
        self.resize(720, 500)

        title = QLabel("GPU 推理档位")
        title.setStyleSheet("font-size:16px;font-weight:600")
        self.profile_combo = QComboBox()
        selected_index = 0
        for index, profile in enumerate(GPU_PROFILES):
            self.profile_combo.addItem(profile.name, profile.id)
            if profile.id == profile_id:
                selected_index = index
        self.profile_combo.setCurrentIndex(selected_index)
        self.profile_combo.currentIndexChanged.connect(self._show_profile)

        self.details = QTextBrowser()
        self.details.setMinimumHeight(180)
        self.details.setOpenExternalLinks(True)

        runtime_title = QLabel("CUDA 绿色运行库")
        runtime_title.setStyleSheet("font-size:15px;font-weight:600")
        self.runtime_status = QLabel()
        self.runtime_status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.hide()
        self.download_button = QPushButton("下载并安装绿色 CUDA 12.9（约 1.27 GB）")
        self.download_button.clicked.connect(self._download_runtime)
        self.download_button.setEnabled(not offline and not manager.is_installed())

        runtime_row = QHBoxLayout()
        runtime_row.addWidget(self.runtime_status, 1)
        runtime_row.addWidget(self.download_button)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self.profile_combo)
        layout.addWidget(self.details)
        layout.addWidget(runtime_title)
        layout.addLayout(runtime_row)
        source_hint = QLabel(
            "下载顺序：清华 PyPI 镜像 → 阿里云 PyPI 镜像 → "
            "官方 PyPI（自动回退并校验 SHA-256）"
        )
        source_hint.setWordWrap(True)
        source_hint.setStyleSheet("color:#666")
        layout.addWidget(source_hint)
        layout.addWidget(self.progress)
        layout.addWidget(buttons)
        self._show_profile()
        self._refresh_runtime_status()

    @property
    def selected_profile_id(self) -> str:
        return str(self.profile_combo.currentData())

    def _show_profile(self, _index: int = -1) -> None:
        profile = selected_profile(self.selected_profile_id)
        runtime = (
            "不需要 CUDA 运行库" if profile.device == "cpu" else "需要 CUDA 12.x、cuBLAS 和 cuDNN 9"
        )
        self.details.setHtml(
            f"<h3>{escape(profile.name)}</h3>"
            f"<p>{escape(profile.description)}</p>"
            f"<p><b>推荐：</b>{escape(profile.recommendation)}<br>"
            f"<b>计算类型：</b>{escape(profile.compute_type)}<br>"
            f"<b>运行要求：</b>{escape(runtime)}</p>"
        )

    def _refresh_runtime_status(self) -> None:
        prefix = "离线模式；" if self.offline else ""
        self.runtime_status.setText(prefix + self.manager.status_text())
        self.download_button.setEnabled(
            not self.offline
            and not self.manager.is_installed()
            and not (self.download_thread and self.download_thread.isRunning())
        )

    def _download_runtime(self) -> None:
        if self.download_thread and self.download_thread.isRunning():
            return
        answer = QMessageBox.question(
            self,
            "下载 NVIDIA GPU 运行库",
            "将从 NVIDIA 在 PyPI 发布的官方软件包下载约 1.27 GB，\n"
            "优先使用已验证的国内镜像，失败后自动回退官方源。\n"
            f"解压后保存到：\n{self.manager.bin_dir.resolve()}\n"
            "运行库使用 NVIDIA Proprietary Software 许可，是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.download_button.setEnabled(False)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.show()
        thread = RuntimeDownloadThread(self.manager, offline=self.offline, parent=self)
        thread.progress.connect(self._download_progress)
        thread.succeeded.connect(self._download_succeeded)
        thread.failed.connect(self._download_failed)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: setattr(self, "download_thread", None))
        self.download_thread = thread
        thread.start()

    def _download_progress(self, completed: int, total: int) -> None:
        value = round(completed / max(total, 1) * 1000)
        self.progress.setValue(min(value, 1000))
        self.progress.setFormat(f"{completed / 1024**3:.2f} GB / {total / 1024**3:.2f} GB（%p%）")

    def _download_succeeded(self) -> None:
        self.progress.hide()
        self._refresh_runtime_status()
        QMessageBox.information(
            self,
            "GPU 运行库",
            f"CUDA 12.9 绿色运行库安装完成。\n\n保存目录：\n{self.manager.bin_dir.resolve()}",
        )

    def _download_failed(self, message: str) -> None:
        self.progress.hide()
        self._refresh_runtime_status()
        QMessageBox.critical(
            self,
            "GPU 运行库下载失败",
            f"{message}\n\n{self.manager.manual_install_text()}",
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.download_thread and self.download_thread.isRunning():
            QMessageBox.information(self, "下载尚未完成", "请等待 GPU 运行库下载并校验完成。")
            event.ignore()
            return
        super().closeEvent(event)

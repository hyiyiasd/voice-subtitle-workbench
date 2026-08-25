from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from voice_subtitle_translator.model_manager import ModelManager


class ModelDownloadThread(QThread):
    progress = Signal(int, int)
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, manager: ModelManager, model_id: str, *, offline: bool, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.model_id = model_id
        self.offline = offline

    def run(self) -> None:
        try:
            self.manager.download(
                self.model_id,
                offline=self.offline,
                on_progress=lambda done, total: self.progress.emit(done, total),
            )
            self.succeeded.emit(self.model_id)
        except Exception as exc:
            self.failed.emit(str(exc))


class ModelManagerDialog(QDialog):
    def __init__(self, manager: ModelManager, *, offline: bool, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.offline = offline
        self.download_thread: ModelDownloadThread | None = None
        self.setWindowTitle("模型管理器")
        self.resize(900, 430)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["模型", "语言", "大小", "许可证", "预计显存", "状态", "说明"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.status = QLabel("离线模式：禁止下载" if offline else "模型保存于程序旁 data\\models")
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.download_button = QPushButton("下载所选")
        self.download_button.clicked.connect(self._download_selected)
        self.verify_button = QPushButton("校验所选")
        self.verify_button.clicked.connect(self._verify_selected)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addWidget(self.status)
        buttons.addWidget(self.progress_bar)
        buttons.addStretch(1)
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.verify_button)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        self._refresh()

    def _refresh(self) -> None:
        self.table.setRowCount(0)
        for model in self.manager.models.values():
            descriptor = model.descriptor
            row = self.table.rowCount()
            self.table.insertRow(row)
            status = "已安装" if self.manager.is_installed(descriptor.id) else "未安装"
            values = [
                descriptor.display_name,
                "/".join(descriptor.languages),
                _format_size(descriptor.size_bytes),
                descriptor.license,
                f"约 {descriptor.recommended_vram_mb} MB",
                status,
                model.note,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(256, descriptor.id)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()

    def _selected_model_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择模型", "请先选择一个模型。")
            return None
        return str(self.table.item(row, 0).data(256))

    def _download_selected(self) -> None:
        model_id = self._selected_model_id()
        if not model_id:
            return
        if self.download_thread and self.download_thread.isRunning():
            return
        self.download_button.setEnabled(False)
        self.status.setText(f"正在下载并校验：{model_id}")
        thread = ModelDownloadThread(self.manager, model_id, offline=self.offline, parent=self)
        thread.succeeded.connect(self._download_succeeded)
        thread.failed.connect(self._download_failed)
        thread.progress.connect(self._download_progress)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: setattr(self, "download_thread", None))
        self.download_thread = thread
        thread.start()

    def _download_succeeded(self, model_id: str) -> None:
        self.download_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status.setText(f"下载和校验完成：{model_id}")
        self._refresh()

    def _download_failed(self, message: str) -> None:
        self.download_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status.setText("下载失败")
        QMessageBox.critical(self, "模型下载失败", message)

    def _download_progress(self, completed: int, total: int) -> None:
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(min(completed, total))
        self.progress_bar.setFormat(f"{_format_size(completed)} / {_format_size(total)}")

    def _verify_selected(self) -> None:
        model_id = self._selected_model_id()
        if not model_id:
            return
        try:
            self.manager.verify(model_id)
            QMessageBox.information(self, "校验完成", f"{model_id} 校验通过。")
        except Exception as exc:
            QMessageBox.critical(self, "校验失败", str(exc))

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.download_thread and self.download_thread.isRunning():
            QMessageBox.information(self, "下载尚未完成", "请等待当前文件下载并校验完成。")
            event.ignore()
            return
        super().closeEvent(event)


def _format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for suffix in ("B", "MB", "GB"):
        if value < 1024 or suffix == "GB":
            return f"{value:.1f} {suffix}"
        value /= 1024
    return f"{size_bytes} B"

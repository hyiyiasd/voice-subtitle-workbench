from __future__ import annotations

from html import escape

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
    QTextBrowser,
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
        self.resize(1080, 650)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["模型", "语言", "大小", "许可证", "预计显存", "状态", "推荐场景"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.currentCellChanged.connect(self._show_details)

        details_title = QLabel("模型介绍")
        details_title.setStyleSheet("font-size: 15px; font-weight: 600")
        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(True)
        self.details.setMinimumHeight(150)

        mode = "离线模式：禁止下载\n" if offline else ""
        self.status = QLabel(f"{mode}模型总目录：{manager.paths.models.resolve()}")
        self.status.setWordWrap(True)
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
        layout.addWidget(details_title)
        layout.addWidget(self.details)
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
                _format_languages(descriptor.languages),
                _format_size(descriptor.size_bytes) if descriptor.size_bytes else "暂未公布",
                descriptor.license,
                (
                    f"约 {descriptor.recommended_vram_mb} MB"
                    if descriptor.recommended_vram_mb
                    else "无需独显"
                ),
                status,
                model.recommendation or model.note,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(256, descriptor.id)
                item.setToolTip(model.description or model.note)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        if self.table.rowCount():
            self.table.selectRow(0)
            self._show_details(0, 0, -1, -1)

    def _show_details(
        self, current_row: int, _current_column: int, _previous_row: int, _previous_column: int
    ) -> None:
        if current_row < 0 or not self.table.item(current_row, 0):
            self.details.clear()
            return
        model_id = str(self.table.item(current_row, 0).data(256))
        model = self.manager.models[model_id]
        descriptor = model.descriptor
        availability = "可自动下载" if model.downloadable else "暂不提供自动下载"
        if model.download_mirrors:
            mirror_hosts = " → ".join(_host(url) for url in model.download_mirrors)
            download_channel = f"{mirror_hosts} → huggingface.co（自动回退）"
        else:
            download_channel = "官方源"
        note = f"<p><b>注意：</b>{escape(model.note)}</p>" if model.note else ""
        source = escape(descriptor.source)
        install_path = escape(str(self.manager.model_path(model_id).resolve()))
        self.details.setHtml(
            f"<h3>{escape(descriptor.display_name)}</h3>"
            f"<p>{escape(model.description or '暂无详细介绍。')}</p>"
            f"<p><b>推荐场景：</b>{escape(model.recommendation or '未指定')}<br>"
            f"<b>语言：</b>{escape(_format_languages(descriptor.languages))}　"
            f"<b>运行时：</b>{escape(descriptor.runtime)}　"
            f"<b>下载：</b>{availability}<br>"
            f"<b>下载通道：</b>{download_channel}<br>"
            f"<b>安装目录：</b><code>{install_path}</code><br>"
            f'<b>来源：</b><a href="{source}">{source}</a></p>'
            f"<p>自动下载失败时，可按模型清单中的文件结构手动放入上述目录，然后点击“校验所选”。</p>"
            f"{note}"
        )

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
        self.status.setText(
            f"正在下载并校验：{model_id}\n"
            f"保存目录：{self.manager.model_path(model_id).resolve()}"
        )
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
        self.status.setText(
            f"下载和校验完成：{model_id}\n"
            f"保存目录：{self.manager.model_path(model_id).resolve()}"
        )
        self._refresh()

    def _download_failed(self, message: str) -> None:
        self.download_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        model_id = self._selected_model_id()
        target = (
            self.manager.model_path(model_id).resolve()
            if model_id
            else self.manager.paths.models.resolve()
        )
        self.status.setText(f"下载失败\n手动放置目录：{target}")
        QMessageBox.critical(
            self,
            "模型下载失败",
            f"{message}\n\n你可以自行下载模型文件并放到：\n{target}\n\n"
            "放置完成后点击“校验所选”。",
        )

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
            model_path = self.manager.model_path(model_id).resolve()
            QMessageBox.information(
                self,
                "校验完成",
                f"{model_id} 校验通过。\n\n模型目录：\n{model_path}",
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "校验失败",
                f"{exc}\n\n请检查模型目录：\n{self.manager.model_path(model_id).resolve()}",
            )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.download_thread and self.download_thread.isRunning():
            QMessageBox.information(self, "下载尚未完成", "请等待当前文件下载并校验完成。")
            event.ignore()
            return
        super().closeEvent(event)


def _format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or suffix == "TB":
            return f"{value:.1f} {suffix}"
        value /= 1024
    return f"{size_bytes} B"


def _format_languages(languages: tuple[str, ...]) -> str:
    names = {"multilingual": "多语言", "ja": "日语", "en": "英语"}
    return "/".join(names.get(language, language) for language in languages)


def _host(url: str) -> str:
    return url.split("/", 3)[2] if "://" in url else url

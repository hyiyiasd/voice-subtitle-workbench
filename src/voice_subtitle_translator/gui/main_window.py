from __future__ import annotations

import re
from collections import deque
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QTableView,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from voice_subtitle_translator import APP_NAME, AUTHOR, BILIBILI_URL, GITHUB_URL, __version__
from voice_subtitle_translator.credentials import CredentialStore
from voice_subtitle_translator.domain import ProjectSettings, Segment
from voice_subtitle_translator.model_manager import ModelManager
from voice_subtitle_translator.paths import AppPaths, bundled_resource
from voice_subtitle_translator.pipeline import PipelineCoordinator
from voice_subtitle_translator.project import Project, quick_file_fingerprint
from voice_subtitle_translator.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderConfig,
)
from voice_subtitle_translator.quality import apply_quality_flags
from voice_subtitle_translator.settings import SettingsStore
from voice_subtitle_translator.subtitles import (
    ExportContent,
    ExportFormat,
    TranslationUnavailableError,
    can_export,
    export_subtitles,
)
from voice_subtitle_translator.transcription import TranscriptionService

from .model_manager_dialog import ModelManagerDialog
from .player import MpvPlayerWidget
from .translation_settings_dialog import TranslationSettingsDialog
from .waveform import WaveformWidget

MEDIA_SUFFIXES = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".webm",
}
TASK_PATH_ROLE = int(Qt.ItemDataRole.UserRole)


class SegmentTableModel(QAbstractTableModel):
    HEADERS = ["锁定", "开始", "结束", "原文", "译文", "问题"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.project: Project | None = None
        self.segments: list[Segment] = []

    def set_project(self, project: Project | None) -> None:
        self.beginResetModel()
        self.project = project
        self.segments = [] if project is None else project.list_segments()
        self.endResetModel()

    def refresh(self) -> None:
        self.set_project(self.project)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008
        return 0 if parent.isValid() else len(self.segments)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        segment = self.segments[index.row()]
        column = index.column()
        if column == 0 and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if segment.human_locked else Qt.CheckState.Unchecked
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            values = [
                "",
                _format_milliseconds(segment.start_ms),
                _format_milliseconds(segment.end_ms),
                segment.source_text,
                segment.translated_text or "",
                "、".join(sorted(segment.quality_flags)),
            ]
            return values[column]
        if role == Qt.ItemDataRole.ToolTipRole and column == 4 and segment.translated_text:
            if not segment.has_valid_translation:
                return "原文已修改，此译文已过期。"
        return None

    def flags(self, index):
        value = super().flags(index)
        if not index.isValid():
            return value
        if index.column() == 0:
            return value | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable
        if index.column() in (1, 2, 3, 4):
            return value | Qt.ItemFlag.ItemIsEditable
        return value

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802
        if not self.project or not index.isValid():
            return False
        segment = self.segments[index.row()]
        try:
            if index.column() == 0 and role == Qt.ItemDataRole.CheckStateRole:
                locked = value == Qt.CheckState.Checked.value or value == Qt.CheckState.Checked
                self.project.set_human_locked(segment.id, locked)
            elif index.column() == 3 and role == Qt.ItemDataRole.EditRole:
                self.project.update_source_text(segment.id, str(value), lock=True)
            elif index.column() == 4 and role == Qt.ItemDataRole.EditRole:
                self.project.save_translation(segment.id, str(value))
            elif index.column() in (1, 2) and role == Qt.ItemDataRole.EditRole:
                milliseconds = _parse_timestamp(str(value))
                start = milliseconds if index.column() == 1 else segment.start_ms
                end = milliseconds if index.column() == 2 else segment.end_ms
                if start < 0 or end <= start:
                    return False
                with self.project.connection:
                    self.project.connection.execute(
                        "UPDATE segments SET start_ms=?, end_ms=?, "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (start, end, segment.id),
                    )
            else:
                return False
        except (ValueError, TypeError):
            return False
        self.refresh()
        return True


class TranslationThread(QThread):
    progress = Signal(int, int)
    succeeded = Signal(int, int, bool)
    failed = Signal(str)

    def __init__(
        self,
        *,
        project_path: Path,
        config: ProviderConfig,
        prompt: str,
        glossary: list[tuple[str, str]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.project_path = project_path
        self.config = config
        self.prompt = prompt
        self.glossary = glossary

    def run(self) -> None:
        provider = OpenAICompatibleProvider(self.config)
        try:
            with Project.open(self.project_path) as project:
                result = PipelineCoordinator(project).translate_pending(
                    provider,
                    prompt=self.prompt,
                    glossary=self.glossary,
                    on_batch_complete=lambda done, total: self.progress.emit(done, total),
                )
            self.succeeded.emit(result.completed, result.cached, result.stopped_by_switch)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            provider.close()


class TranscriptionThread(QThread):
    succeeded = Signal(str, int)
    failed = Signal(str)

    def __init__(
        self,
        *,
        project_path: Path,
        paths: AppPaths,
        model_id: str,
        device: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.project_path = project_path
        self.paths = paths
        self.model_id = model_id
        self.device = device

    def run(self) -> None:
        try:
            manager = ModelManager(self.paths, bundled_resource("models/manifest.json"))
            with Project.open(self.project_path) as project:
                task_id = TranscriptionService(
                    project,
                    paths=self.paths,
                    model_manager=manager,
                    ffmpeg_path=self.paths.root / "runtime" / "ffmpeg.exe",
                ).run(model_id=self.model_id, device=self.device)
                count = len(project.list_segments())
            self.succeeded.emit(task_id, count)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, paths: AppPaths) -> None:
        super().__init__()
        self.paths = paths
        self.settings_store = SettingsStore(paths)
        self.global_settings = self.settings_store.load()
        self.project: Project | None = None
        self.translation_thread: TranslationThread | None = None
        self.transcription_thread: TranscriptionThread | None = None
        self.pending_media: deque[Path] = deque()
        self.queued_media: set[Path] = set()
        self.current_media_path: Path | None = None
        self.task_groups: dict[Path, QTreeWidgetItem] = {}
        self.task_items: dict[Path, QTreeWidgetItem] = {}
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1280, 820)
        self.setAcceptDrops(True)
        self._build_ui()
        self._build_actions()
        self._set_project(None)

    def _build_ui(self) -> None:
        self.toolbar = QToolBar("主工具栏")
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)
        self.translation_toggle = QCheckBox("启用翻译")
        self.translation_toggle.toggled.connect(self._translation_toggled)
        self.workflow_label = QLabel()

        self.player = MpvPlayerWidget()
        self.player.initialize(self.paths.root / "runtime" / "libmpv-2.dll")
        self.waveform = WaveformWidget()
        media_panel = QWidget()
        media_layout = QVBoxLayout(media_panel)
        media_layout.setContentsMargins(0, 0, 0, 0)
        media_layout.addWidget(self.player)
        media_layout.addWidget(self.waveform)

        self.table_model = SegmentTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.table_model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.doubleClicked.connect(self._seek_selected)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 54)
        self.table.setColumnWidth(1, 95)
        self.table.setColumnWidth(2, 95)
        self.table.setColumnWidth(3, 360)
        self.table.setColumnWidth(4, 360)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(media_panel)
        splitter.addWidget(self.table)
        splitter.setStretchFactor(1, 2)

        footer = QLabel(
            f'作者：{AUTHOR}｜<a href="{BILIBILI_URL}">B站主页</a>｜'
            f'<a href="{GITHUB_URL}/releases">官方版本：GitHub Releases</a>'
        )
        footer.setOpenExternalLinks(False)
        footer.linkActivated.connect(lambda url: QDesktopServices.openUrl(QUrl(url)))
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.setStyleSheet("padding:6px;color:#666")

        central = QWidget()
        layout = QVBoxLayout(central)
        self.drop_hint = QLabel(
            "将 MP3、WAV、MP4 等音视频或整个文件夹拖到这里；启用翻译后会在识别完成后继续翻译"
        )
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setStyleSheet(
            "padding:12px;border:2px dashed #7a8aa0;border-radius:6px;font-size:15px;color:#405060"
        )
        layout.addWidget(self.drop_hint)
        layout.addWidget(splitter)
        layout.addWidget(footer)
        self.setCentralWidget(central)

        task_dock = QDockWidget("任务队列", self)
        self.task_tree = QTreeWidget()
        self.task_tree.setHeaderLabels(["文件夹 / 媒体", "状态"])
        self.task_tree.setColumnWidth(0, 260)
        self.task_tree.itemDoubleClicked.connect(self._open_task_media)
        task_dock.setWidget(self.task_tree)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, task_dock)

        side = QTabWidget()
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText("标题、背景、人物关系、语气和自由翻译指令")
        self.glossary_edit = QPlainTextEdit()
        self.glossary_edit.setPlaceholderText("每行：原词=译词")
        side.addTab(self.prompt_edit, "提示词")
        side.addTab(self.glossary_edit, "术语")
        side_dock = QDockWidget("翻译上下文", self)
        side_dock.setWidget(side)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, side_dock)

    def _build_actions(self) -> None:
        new_action = QAction("新建项目", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self.new_project)
        open_action = QAction("打开项目", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_project)
        media_action = QAction("打开媒体并转换", self)
        media_action.triggered.connect(self.add_media)
        folder_action = QAction("导入文件夹", self)
        folder_action.triggered.connect(self.add_media_folder)
        resume_queue_action = QAction("继续队列", self)
        resume_queue_action.triggered.connect(self.resume_media_queue)
        models_action = QAction("模型管理", self)
        models_action.triggered.connect(self.show_model_manager)
        transcribe_action = QAction("开始识别", self)
        transcribe_action.triggered.connect(lambda: self.transcribe_media(automatic=False))
        check_action = QAction("检查字幕", self)
        check_action.triggered.connect(self.check_quality)
        self.problems_action = QAction("仅看问题字幕", self)
        self.problems_action.setCheckable(True)
        self.problems_action.toggled.connect(self._filter_problem_rows)
        replace_action = QAction("搜索替换", self)
        replace_action.setShortcut(QKeySequence.StandardKey.Find)
        replace_action.triggered.connect(self.search_replace)
        translate_action = QAction("翻译未完成字幕", self)
        translate_action.triggered.connect(lambda: self.translate_pending())
        translation_settings_action = QAction("翻译服务设置", self)
        translation_settings_action.triggered.connect(self.show_translation_settings)
        export_action = QAction("导出字幕", self)
        export_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        export_action.triggered.connect(self.export_dialog)
        about_action = QAction("关于", self)
        about_action.triggered.connect(self.show_about)
        for action in (
            new_action,
            open_action,
            media_action,
            folder_action,
            resume_queue_action,
            models_action,
            transcribe_action,
            check_action,
            self.problems_action,
            replace_action,
            translation_settings_action,
            translate_action,
            export_action,
        ):
            self.toolbar.addAction(action)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(self.translation_toggle)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(self.workflow_label)
        self.menuBar().addAction(about_action)

    def _set_project(self, project: Project | None) -> None:
        if self.project and self.project is not project:
            self._save_side_context()
            self.project.close()
        self.project = project
        self.table_model.set_project(project)
        self.translation_toggle.blockSignals(True)
        self.translation_toggle.setChecked(
            self.global_settings.last_translation_enabled
            if project is None
            else project.get_settings().translation_enabled
        )
        self.translation_toggle.blockSignals(False)
        self._update_workflow_label()
        if project:
            self.prompt_edit.setPlainText(project.active_prompt())
            self.glossary_edit.setPlainText(
                "\n".join(f"{source}={target}" for source, target in project.glossary())
            )
            self.setWindowTitle(f"{APP_NAME} {__version__} — {project.path.name}")
            media = project.resolve_media()
            if media:
                self.player.load(media)
        else:
            self.prompt_edit.clear()
            self.glossary_edit.clear()
            self.setWindowTitle(f"{APP_NAME} {__version__}")

    def new_project(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "新建项目", "", "字幕项目 (*.vstproj)")
        if not path:
            return
        settings = ProjectSettings(
            translation_enabled=self.global_settings.last_translation_enabled
        )
        try:
            self._set_project(Project.create(path, settings))
        except Exception as exc:
            QMessageBox.critical(self, "无法新建项目", str(exc))

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "打开项目", "", "字幕项目 (*.vstproj)")
        if path:
            self._open_project_path(Path(path))

    def _open_project_path(self, path: Path) -> None:
        try:
            self._set_project(Project.open(path))
            self.global_settings.last_project = str(path)
            self.settings_store.save(self.global_settings)
        except Exception as exc:
            QMessageBox.critical(self, "无法打开项目", str(exc))

    def add_media(self) -> None:
        filters = (
            "音视频文件 (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus "
            "*.mp4 *.mkv *.mov *.avi *.webm);;所有文件 (*)"
        )
        path, _ = QFileDialog.getOpenFileName(self, "选择要转文字的音视频", "", filters)
        if path:
            media = Path(path).resolve()
            self._enqueue_media_files([media], media.parent)

    def add_media_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择包含音视频的文件夹")
        if not selected:
            return
        root = Path(selected).resolve()
        media_files = sorted(
            (
                path.resolve()
                for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
            ),
            key=lambda path: str(path).casefold(),
        )
        if not media_files:
            QMessageBox.information(self, "没有媒体", "该文件夹及其子文件夹中没有支持的音视频。")
            return
        self._enqueue_media_files(media_files, root)

    def _enqueue_media_files(self, media_files: list[Path], root: Path) -> None:
        root = root.resolve()
        group = self.task_groups.get(root)
        if group is None:
            group = QTreeWidgetItem([f"📁 {root.name or root}", ""])
            group.setToolTip(0, str(root))
            self.task_tree.addTopLevelItem(group)
            self.task_groups[root] = group
        added = 0
        for media in media_files:
            media = media.resolve()
            if media.suffix.lower() not in MEDIA_SUFFIXES or not media.is_file():
                continue
            item = self.task_items.get(media)
            if item is None:
                try:
                    label = str(media.relative_to(root))
                except ValueError:
                    label = media.name
                item = QTreeWidgetItem([label, "等待处理"])
                item.setData(0, TASK_PATH_ROLE, str(media))
                item.setToolTip(0, str(media))
                group.addChild(item)
                self.task_items[media] = item
            if media not in self.queued_media and media != self.current_media_path:
                self.pending_media.append(media)
                self.queued_media.add(media)
                item.setText(1, "等待处理")
                added += 1
        group.setText(1, f"{group.childCount()} 个文件")
        group.setExpanded(True)
        if added:
            self.statusBar().showMessage(f"已加入 {added} 个媒体文件", 5000)
        QTimer.singleShot(0, self._start_next_queued_media)

    def _start_next_queued_media(self) -> None:
        if self.current_media_path is not None:
            return
        if self.transcription_thread and self.transcription_thread.isRunning():
            return
        if self.translation_thread and self.translation_thread.isRunning():
            return
        while self.pending_media:
            media = self.pending_media.popleft()
            if not media.is_file():
                self.queued_media.discard(media)
                self._set_media_status(media, "文件不存在")
                continue
            self.current_media_path = media
            self._set_media_status(media, "正在导入")
            self._ingest_media(media)
            return
        self.statusBar().showMessage("任务队列已完成", 5000)

    def resume_media_queue(self) -> None:
        if self.current_media_path and not (
            self.transcription_thread and self.transcription_thread.isRunning()
        ):
            if (
                self.project
                and self.project.get_settings().translation_enabled
                and self.project.list_segments()
            ):
                self.translate_pending()
            else:
                self.transcribe_media(automatic=True)
            return
        self._start_next_queued_media()

    def _set_media_status(self, media: Path, status: str) -> None:
        if item := self.task_items.get(media.resolve()):
            item.setText(1, status)

    def _finish_current_media(self, status: str) -> None:
        media = self.current_media_path
        if media is None:
            return
        self._set_media_status(media, status)
        self.queued_media.discard(media)
        self.current_media_path = None
        QTimer.singleShot(0, self._start_next_queued_media)

    def _open_task_media(self, item: QTreeWidgetItem, _column: int) -> None:
        value = item.data(0, TASK_PATH_ROLE)
        if not value:
            item.setExpanded(not item.isExpanded())
            return
        if (self.transcription_thread and self.transcription_thread.isRunning()) or (
            self.translation_thread and self.translation_thread.isRunning()
        ):
            return
        self._ingest_media(Path(str(value)))

    def _ingest_media(self, media_path: Path) -> None:
        media_path = media_path.resolve()
        if media_path.suffix.lower() not in MEDIA_SUFFIXES:
            QMessageBox.information(
                self,
                "不支持的文件",
                "请拖入 MP3、WAV、M4A、FLAC、MP4、MKV、MOV 或 WebM 等音视频文件。",
            )
            self._finish_current_media("不支持的格式")
            return
        if self.transcription_thread and self.transcription_thread.isRunning():
            QMessageBox.information(self, "识别正在运行", "请等待当前媒体识别完成。")
            return
        if self.translation_thread and self.translation_thread.isRunning():
            QMessageBox.information(self, "翻译正在运行", "请等待当前媒体翻译完成。")
            return
        try:
            fingerprint = quick_file_fingerprint(media_path)
            safe_stem = re.sub(r"[^\w\-]+", "_", media_path.stem, flags=re.UNICODE).strip("_")
            safe_stem = safe_stem[:60] or "media"
            projects_dir = self.paths.data / "projects"
            projects_dir.mkdir(parents=True, exist_ok=True)
            project_path = projects_dir / f"{safe_stem}-{fingerprint[:12]}.vstproj"
            if project_path.exists():
                project = Project.open(project_path)
            else:
                project = Project.create(
                    project_path,
                    ProjectSettings(
                        translation_enabled=self.global_settings.last_translation_enabled
                    ),
                )
            project.set_media(media_path)
            self._set_project(project)
            self.global_settings.last_project = str(project_path)
            self.settings_store.save(self.global_settings)
            self.player.load(media_path)
        except Exception as exc:
            QMessageBox.critical(self, "无法导入媒体", str(exc))
            self._finish_current_media("导入失败")
            return
        existing = self.project.list_segments()
        if existing:
            self._set_media_status(media_path, f"已恢复 {len(existing)} 条字幕")
            self.statusBar().showMessage(
                f"已恢复该媒体的 {len(existing)} 条字幕；可继续校对或重新识别", 8000
            )
            if self.translation_toggle.isChecked() and any(
                not segment.has_valid_translation for segment in existing
            ):
                self._set_media_status(media_path, "等待翻译")
                QTimer.singleShot(0, self.translate_pending)
            else:
                self._finish_current_media("已完成")
        else:
            self._set_media_status(media_path, "等待识别")
            QTimer.singleShot(0, lambda: self.transcribe_media(automatic=True))

    def show_model_manager(self) -> None:
        offline = self.project.get_settings().offline if self.project else False
        dialog = ModelManagerDialog(
            ModelManager(self.paths, bundled_resource("models/manifest.json")),
            offline=offline,
            parent=self,
        )
        dialog.exec()

    def transcribe_media(self, *, automatic: bool = False) -> None:
        if not self._require_project():
            return
        if self.project.resolve_media() is None:
            QMessageBox.information(self, "没有媒体", "请先添加或重新定位音视频文件。")
            return
        if self.transcription_thread and self.transcription_thread.isRunning():
            QMessageBox.information(self, "识别正在运行", "请等待当前识别任务完成。")
            return
        manager = ModelManager(self.paths, bundled_resource("models/manifest.json"))
        installed = [
            model.descriptor.id
            for model in manager.models.values()
            if model.descriptor.id != "silero-vad-v6" and manager.is_installed(model.descriptor.id)
        ]
        if not manager.is_installed("silero-vad-v6") or not installed:
            QMessageBox.information(
                self,
                "模型尚未安装",
                "首次转换需要 Silero VAD 和至少一个语音识别模型。"
                "请在模型管理器中下载后关闭窗口，程序会继续检查。",
            )
            self.show_model_manager()
            manager = ModelManager(self.paths, bundled_resource("models/manifest.json"))
            installed = [
                model.descriptor.id
                for model in manager.models.values()
                if model.descriptor.id != "silero-vad-v6"
                and manager.is_installed(model.descriptor.id)
            ]
            if not manager.is_installed("silero-vad-v6") or not installed:
                self.statusBar().showMessage("尚未安装完整识别模型，媒体项目已保存", 8000)
                if self.current_media_path:
                    self._set_media_status(self.current_media_path, "等待安装模型")
                return
        settings = self.project.get_settings()
        if automatic:
            model_id = settings.asr_model if settings.asr_model in installed else installed[0]
            device = self.global_settings.asr_device
        else:
            model_id, ok = QInputDialog.getItem(
                self, "选择识别模型", "模型：", installed, editable=False
            )
            if not ok:
                return
            device_label, ok = QInputDialog.getItem(
                self,
                "选择运行设备",
                "设备（GPU 初始化失败时不会静默切换）：",
                ["CPU (int8)", "CUDA (int8_float16)"],
                1 if self.global_settings.asr_device == "cuda" else 0,
                editable=False,
            )
            if not ok:
                return
            device = "cuda" if device_label.startswith("CUDA") else "cpu"
            self.global_settings.asr_device = device
            self.settings_store.save(self.global_settings)
        settings.asr_model = model_id
        self.project.save_settings(settings)
        thread = TranscriptionThread(
            project_path=self.project.path,
            paths=self.paths,
            model_id=model_id,
            device=device,
            parent=self,
        )
        if self.current_media_path:
            self._set_media_status(self.current_media_path, f"识别中 · {model_id} / {device}")
        self.statusBar().showMessage("正在执行音频标准化、VAD 和识别……")
        thread.succeeded.connect(self._transcription_succeeded)
        thread.failed.connect(self._transcription_failed)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: setattr(self, "transcription_thread", None))
        self.transcription_thread = thread
        thread.start()

    def _transcription_succeeded(self, task_id: str, segment_count: int) -> None:
        if self.project:
            self.table_model.refresh()
            self._filter_problem_rows(self.problems_action.isChecked())
        self.statusBar().showMessage(
            f"识别完成：共 {segment_count} 条字幕（任务 {task_id[:8]}）", 8000
        )
        if segment_count and self.translation_toggle.isChecked():
            if self.current_media_path:
                self._set_media_status(self.current_media_path, "等待翻译")
            QTimer.singleShot(0, self.translate_pending)
        elif segment_count:
            self._finish_current_media(f"识别完成 · {segment_count} 条")
        else:
            self._finish_current_media("未检测到语音")

    def _transcription_failed(self, message: str) -> None:
        QMessageBox.critical(self, "识别失败", message)
        self._finish_current_media("识别失败")
        if self.project:
            self.table_model.refresh()

    def _translation_toggled(self, checked: bool) -> None:
        self.global_settings.last_translation_enabled = checked
        self.settings_store.save(self.global_settings)
        if self.project:
            PipelineCoordinator(self.project).set_translation_enabled(checked)
        self._update_workflow_label()
        if (
            checked
            and self.project
            and self.project.list_segments()
            and not (self.transcription_thread and self.transcription_thread.isRunning())
        ):
            QTimer.singleShot(0, self.translate_pending)

    def _update_workflow_label(self) -> None:
        if self.translation_toggle.isChecked():
            self.workflow_label.setText("当前：识别后翻译为简体中文")
        else:
            self.workflow_label.setText("当前：仅识别并导出原文字幕")

    def check_quality(self) -> None:
        if not self._require_project():
            return
        segments = self.project.list_segments()
        apply_quality_flags(segments, self.project.get_settings().subtitle)
        with self.project.connection:
            for segment in segments:
                import json

                self.project.connection.execute(
                    "UPDATE segments SET quality_flags_json=? WHERE id=?",
                    (json.dumps(sorted(segment.quality_flags), ensure_ascii=False), segment.id),
                )
        self.table_model.refresh()
        self._filter_problem_rows(self.problems_action.isChecked())
        problem_count = sum(bool(item.quality_flags) for item in segments)
        self.statusBar().showMessage(f"检查完成：{problem_count} 条字幕需要注意", 5000)

    def show_translation_settings(self) -> bool:
        provider = self.global_settings.translation_provider or "openai"
        credential_store = CredentialStore()
        has_saved_key = bool(credential_store.get(provider))
        dialog = TranslationSettingsDialog(
            provider=provider,
            base_url=self.global_settings.translation_base_url,
            model=self.global_settings.translation_model,
            has_saved_key=has_saved_key,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return False
        values = dialog.values()
        if not values.base_url or not values.model:
            QMessageBox.information(self, "配置不完整", "请填写 Base URL 和模型名称。")
            return False
        is_local = values.base_url.startswith(("http://127.0.0.1", "http://localhost"))
        if values.api_key:
            credential_store.set(values.provider, values.api_key)
        elif not is_local and not credential_store.get(values.provider):
            QMessageBox.information(self, "缺少 API Key", "远程翻译服务需要 API Key。")
            return False
        self.global_settings.translation_provider = values.provider
        self.global_settings.translation_base_url = values.base_url
        self.global_settings.translation_model = values.model
        self.global_settings.translation_structured_output = values.structured_output
        self.settings_store.save(self.global_settings)
        if self.project:
            settings = self.project.get_settings()
            settings.translation_provider = values.provider
            settings.translation_model = values.model
            self.project.save_settings(settings)
        self.statusBar().showMessage(f"已保存翻译服务：{values.provider} / {values.model}", 6000)
        return True

    def translate_pending(self) -> None:
        if not self._require_project():
            return
        if not self.project.get_settings().translation_enabled:
            QMessageBox.information(self, "未启用翻译", "请先开启顶部“启用翻译”开关。")
            return
        if self.translation_thread and self.translation_thread.isRunning():
            QMessageBox.information(self, "翻译正在运行", "请等待当前翻译批次完成。")
            return
        if (
            not self.global_settings.translation_base_url
            or not self.global_settings.translation_model
        ) and not self.show_translation_settings():
            self.statusBar().showMessage("识别已完成；尚未配置翻译服务", 8000)
            return
        settings = self.project.get_settings()
        provider_id = self.global_settings.translation_provider or "openai-compatible"
        base_url = self.global_settings.translation_base_url
        model = self.global_settings.translation_model
        is_local = base_url.startswith(("http://127.0.0.1", "http://localhost"))
        key = "" if is_local else (CredentialStore().get(provider_id) or "")
        if not is_local and not key:
            if not self.show_translation_settings():
                return
            provider_id = self.global_settings.translation_provider
            base_url = self.global_settings.translation_base_url
            model = self.global_settings.translation_model
            is_local = base_url.startswith(("http://127.0.0.1", "http://localhost"))
            key = "" if is_local else (CredentialStore().get(provider_id) or "")
            if not is_local and not key:
                return
        settings.translation_provider = provider_id
        settings.translation_model = model
        self.project.save_settings(settings)
        self._save_side_context()
        thread = TranslationThread(
            project_path=self.project.path,
            config=ProviderConfig(
                id=provider_id,
                base_url=base_url,
                api_key=key,
                structured_output=self.global_settings.translation_structured_output,
                offline=settings.offline,
            ),
            prompt=self.prompt_edit.toPlainText(),
            glossary=_parse_glossary(self.glossary_edit.toPlainText()),
            parent=self,
        )
        if self.current_media_path:
            self._set_media_status(self.current_media_path, f"翻译中 · {provider_id} / {model}")
        thread.progress.connect(
            lambda done, total: self.statusBar().showMessage(f"翻译批次 {done}/{total}")
        )
        thread.succeeded.connect(self._translation_succeeded)
        thread.failed.connect(self._translation_failed)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: setattr(self, "translation_thread", None))
        self.translation_thread = thread
        thread.start()

    def _translation_succeeded(self, completed: int, cached: int, stopped: bool) -> None:
        if self.project:
            self.table_model.refresh()
            self._filter_problem_rows(self.problems_action.isChecked())
        if stopped:
            self.statusBar().showMessage(
                f"翻译已暂停：完成 {completed} 条，缓存命中 {cached} 条", 7000
            )
            self._finish_current_media(f"翻译已暂停 · {completed} 条")
        else:
            self.statusBar().showMessage(f"翻译完成 {completed} 条，缓存命中 {cached} 条", 7000)
            self._finish_current_media(f"已完成 · 翻译 {completed} 条")

    def _translation_failed(self, message: str) -> None:
        QMessageBox.critical(self, "翻译失败", message)
        self._finish_current_media("翻译失败")
        if self.project:
            self.table_model.refresh()

    def search_replace(self) -> None:
        if not self._require_project():
            return
        source, ok = QInputDialog.getText(self, "搜索替换", "查找原文：")
        if not ok or not source:
            return
        replacement, ok = QInputDialog.getText(self, "搜索替换", "替换为：")
        if not ok:
            return
        changed = 0
        for segment in self.project.list_segments():
            if source in segment.source_text:
                self.project.update_source_text(
                    segment.id, segment.source_text.replace(source, replacement), lock=True
                )
                changed += 1
        self.table_model.refresh()
        self._filter_problem_rows(self.problems_action.isChecked())
        self.statusBar().showMessage(f"已替换 {changed} 条字幕", 5000)

    def _filter_problem_rows(self, enabled: bool) -> None:
        for row, segment in enumerate(self.table_model.segments):
            self.table.setRowHidden(row, enabled and not bool(segment.quality_flags))

    def export_dialog(self) -> None:
        if not self._require_project():
            return
        formats = "SRT (*.srt);;WebVTT (*.vtt);;ASS (*.ass);;文本 (*.txt);;JSON (*.json)"
        path, selected = QFileDialog.getSaveFileName(self, "导出字幕", "", formats)
        if not path:
            return
        format_map = {
            "SRT": ExportFormat.SRT,
            "WebVTT": ExportFormat.VTT,
            "ASS": ExportFormat.ASS,
            "文本": ExportFormat.TXT,
            "JSON": ExportFormat.JSON,
        }
        output_format = next(
            (value for key, value in format_map.items() if selected.startswith(key)), None
        )
        output_format = output_format or ExportFormat(Path(path).suffix.lstrip(".").lower())
        choices = ["原文"]
        allowed, reason = can_export(self.project.list_segments(), ExportContent.TRANSLATION)
        if allowed:
            choices.extend(["译文", "双语"])
        content_name, ok = QInputDialog.getItem(self, "导出内容", "内容：", choices, editable=False)
        if not ok:
            return
        content_map = {
            "原文": ExportContent.SOURCE,
            "译文": ExportContent.TRANSLATION,
            "双语": ExportContent.BILINGUAL,
        }
        try:
            export_subtitles(
                self.project.list_segments(),
                path,
                output_format=output_format,
                content=content_map[content_name],
            )
            self.statusBar().showMessage(f"已导出：{path}", 5000)
        except TranslationUnavailableError:
            QMessageBox.information(self, "无法导出译文", reason)
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def _seek_selected(self, index: QModelIndex) -> None:
        if index.isValid() and index.row() < len(self.table_model.segments):
            self.player.seek_ms(self.table_model.segments[index.row()].start_ms)

    def _require_project(self) -> bool:
        if self.project:
            return True
        QMessageBox.information(self, "没有项目", "请先新建或打开 .vstproj 项目。")
        return False

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            f"关于{APP_NAME}",
            f"{APP_NAME} {__version__}\n\n作者：{AUTHOR}\nB站：{BILIBILI_URL}\n"
            f"GitHub：{GITHUB_URL}\n官方版本：GitHub Releases\n许可证：MIT\n\n"
            "第三方组件许可详见 THIRD_PARTY_NOTICES.md。",
        )

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [Path(item.toLocalFile()) for item in event.mimeData().urls() if item.isLocalFile()]
        if not paths:
            return
        projects = [path for path in paths if path.suffix.lower() == ".vstproj"]
        if projects:
            self._open_project_path(projects[0])
            event.acceptProposedAction()
            return
        for path in paths:
            resolved = path.resolve()
            if resolved.is_dir():
                media_files = sorted(
                    (
                        item.resolve()
                        for item in resolved.rglob("*")
                        if item.is_file() and item.suffix.lower() in MEDIA_SUFFIXES
                    ),
                    key=lambda item: str(item).casefold(),
                )
                self._enqueue_media_files(media_files, resolved)
            elif resolved.suffix.lower() in MEDIA_SUFFIXES:
                self._enqueue_media_files([resolved], resolved.parent)
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.transcription_thread and self.transcription_thread.isRunning():
            QMessageBox.information(
                self,
                "识别任务尚未结束",
                "当前识别批次完成并保存前不能关闭程序。模型进程异常退出后，项目可恢复。",
            )
            event.ignore()
            return
        if self.translation_thread and self.translation_thread.isRunning():
            if self.project and self.project.get_settings().translation_enabled:
                PipelineCoordinator(self.project).set_translation_enabled(False)
                self.translation_toggle.blockSignals(True)
                self.translation_toggle.setChecked(False)
                self.translation_toggle.blockSignals(False)
            QMessageBox.information(
                self,
                "翻译批次尚未结束",
                "已停止调度新批次。请等待当前请求保存完成后再关闭程序。",
            )
            event.ignore()
            return
        if self.project:
            self._save_side_context()
            self.project.close()
            self.project = None
        super().closeEvent(event)

    def _save_side_context(self) -> None:
        if not self.project:
            return
        self.project.save_active_prompt(self.prompt_edit.toPlainText())
        self.project.save_glossary(_parse_glossary(self.glossary_edit.toPlainText()))


def _format_milliseconds(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _parse_timestamp(value: str) -> int:
    parts = value.replace(",", ".").split(":")
    if len(parts) != 3:
        raise ValueError(value)
    hours, minutes = int(parts[0]), int(parts[1])
    seconds = float(parts[2])
    return round((hours * 3600 + minutes * 60 + seconds) * 1000)


def _parse_glossary(value: str) -> list[tuple[str, str]]:
    result = []
    for line in value.splitlines():
        if "=" in line:
            source, target = line.split("=", 1)
            if source.strip() and target.strip():
                result.append((source.strip(), target.strip()))
    return result

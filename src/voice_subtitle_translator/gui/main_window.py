from __future__ import annotations

import re
import shutil
from collections import deque
from pathlib import Path
from threading import Event, Lock

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDockWidget,
    QFileDialog,
    QGroupBox,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
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
from voice_subtitle_translator.gpu_runtime import GPURuntimeManager, selected_profile
from voice_subtitle_translator.model_manager import ModelManager
from voice_subtitle_translator.paths import AppPaths, bundled_resource
from voice_subtitle_translator.pipeline import PipelineCoordinator, TranslationCancelledError
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
    InvalidSubtitleError,
    TranslationUnavailableError,
    can_export,
    export_subtitles,
    parse_srt,
)
from voice_subtitle_translator.transcription import (
    TranscriptionCancelledError,
    TranscriptionService,
)
from voice_subtitle_translator.worker_client import WorkerClient

from .batch_operation_dialog import BatchOperationDialog
from .gpu_settings_dialog import GPUSettingsDialog
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
SOURCE_SUBTITLE_SUFFIXES = {".srt"}
SUPPORTED_INPUT_SUFFIXES = MEDIA_SUFFIXES | SOURCE_SUBTITLE_SUFFIXES
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
    cancelled = Signal()

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
        self._stop_requested = Event()
        self._provider_lock = Lock()
        self._provider: OpenAICompatibleProvider | None = None

    def run(self) -> None:
        provider = OpenAICompatibleProvider(self.config)
        with self._provider_lock:
            self._provider = provider
        try:
            with Project.open(self.project_path) as project:
                result = PipelineCoordinator(project).translate_pending(
                    provider,
                    prompt=self.prompt,
                    glossary=self.glossary,
                    on_batch_complete=lambda done, total: self.progress.emit(done, total),
                    should_stop=self._stop_requested.is_set,
                )
            self.succeeded.emit(result.completed, result.cached, result.stopped_by_switch)
        except TranslationCancelledError:
            self.cancelled.emit()
        except Exception as exc:
            if self._stop_requested.is_set():
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))
        finally:
            try:
                provider.close()
            except Exception:
                pass
            with self._provider_lock:
                self._provider = None

    def force_stop(self) -> None:
        self._stop_requested.set()
        with self._provider_lock:
            provider = self._provider
        if provider:
            try:
                provider.close()
            except Exception:
                pass


class TranscriptionThread(QThread):
    progress = Signal(str, int, int)
    succeeded = Signal(str, int)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        *,
        project_path: Path,
        paths: AppPaths,
        model_id: str,
        device: str,
        compute_type: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.project_path = project_path
        self.paths = paths
        self.model_id = model_id
        self.device = device
        self.compute_type = compute_type
        self._stop_requested = Event()
        self._worker_lock = Lock()
        self._active_worker: WorkerClient | None = None

    def _worker_changed(self, worker: WorkerClient | None) -> None:
        with self._worker_lock:
            self._active_worker = worker

    def force_stop(self) -> None:
        self._stop_requested.set()
        with self._worker_lock:
            worker = self._active_worker
        if worker:
            worker.terminate()

    def run(self) -> None:
        try:
            manager = ModelManager(self.paths, bundled_resource("models/manifest.json"))
            with Project.open(self.project_path) as project:
                task_id = TranscriptionService(
                    project,
                    paths=self.paths,
                    model_manager=manager,
                    ffmpeg_path=self.paths.root / "runtime" / "ffmpeg.exe",
                ).run(
                    model_id=self.model_id,
                    device=self.device,
                    compute_type=self.compute_type,
                    on_progress=lambda stage, done, total: self.progress.emit(
                        stage, done, total
                    ),
                    should_stop=self._stop_requested.is_set,
                    on_worker_change=self._worker_changed,
                )
                count = len(project.list_segments())
            self.succeeded.emit(task_id, count)
        except TranscriptionCancelledError:
            self.cancelled.emit()
        except Exception as exc:
            if self._stop_requested.is_set():
                self.cancelled.emit()
            else:
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
        self.pending_actions: dict[Path, str] = {}
        self.queued_media: set[Path] = set()
        self.current_media_path: Path | None = None
        self.current_action = "auto"
        self.task_groups: dict[Path, QTreeWidgetItem] = {}
        self.task_items: dict[Path, QTreeWidgetItem] = {}
        self.task_progress: dict[Path, int] = {}
        self.task_status: dict[Path, str] = {}
        self.detail_media_path: Path | None = None
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
            "第一步：加入音视频并手动转文字，生成原文 SRT；"
            "第二步：选择原文 SRT，生成中文 SRT"
        )
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setStyleSheet(
            "padding:12px;border:2px dashed #7a8aa0;border-radius:6px;font-size:15px;color:#405060"
        )
        layout.addWidget(self.drop_hint)
        self.task_detail = QGroupBox("文件任务详情")
        self.task_detail.setVisible(False)
        detail_layout = QVBoxLayout(self.task_detail)
        self.detail_path_label = QLabel()
        self.detail_path_label.setWordWrap(True)
        self.detail_status_label = QLabel()
        self.detail_progress = QProgressBar()
        detail_layout.addWidget(self.detail_path_label)
        detail_layout.addWidget(self.detail_status_label)
        detail_layout.addWidget(self.detail_progress)
        layout.addWidget(self.task_detail)
        layout.addWidget(splitter)
        layout.addWidget(footer)
        self.setCentralWidget(central)

        self.task_dock = QDockWidget("任务队列", self)
        self.task_dock.setObjectName("task_queue_dock")
        self.task_tree = QTreeWidget()
        self.task_tree.setHeaderLabels(["文件夹 / 媒体"])
        self.task_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.task_tree.setIconSize(QSize(42, 10))
        self.task_tree.itemDoubleClicked.connect(self._open_task_media)
        self.task_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_tree.customContextMenuRequested.connect(self._show_task_context_menu)
        self.task_dock.setWidget(self.task_tree)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.task_dock)

        side = QTabWidget()
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText("标题、背景、人物关系、语气和自由翻译指令")
        self.glossary_edit = QPlainTextEdit()
        self.glossary_edit.setPlaceholderText("每行：原词=译词")
        side.addTab(self.prompt_edit, "提示词")
        side.addTab(self.glossary_edit, "术语")
        self.side_dock = QDockWidget("翻译上下文", self)
        self.side_dock.setObjectName("translation_context_dock")
        self.side_dock.setWidget(side)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.side_dock)

    def _build_actions(self) -> None:
        media_action = QAction("添加媒体", self)
        media_action.triggered.connect(self.add_media)
        folder_action = QAction("导入文件夹", self)
        folder_action.triggered.connect(self.add_media_folder)
        source_srt_action = QAction("翻译原文 SRT", self)
        source_srt_action.triggered.connect(self.add_source_srt)
        batch_action = QAction("批量操作…", self)
        batch_action.triggered.connect(self.show_batch_operations)
        resume_queue_action = QAction("继续队列", self)
        resume_queue_action.triggered.connect(self.resume_media_queue)
        models_action = QAction("模型管理", self)
        models_action.triggered.connect(self.show_model_manager)
        gpu_action = QAction("GPU 推理设置", self)
        gpu_action.triggered.connect(self.show_gpu_settings)
        transcribe_action = QAction("开始识别", self)
        transcribe_action.triggered.connect(self.start_selected_transcription)
        force_pause_action = QAction("强制暂停", self)
        force_pause_action.setToolTip("立即停止当前模型/API 请求并清空尚未开始的队列")
        force_pause_action.triggered.connect(self.force_pause_tasks)
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
        open_subtitles_action = QAction("打开字幕文件夹", self)
        open_subtitles_action.triggered.connect(self.open_subtitle_folder)
        about_action = QAction("关于", self)
        about_action.triggered.connect(self.show_about)
        for action in (
            media_action,
            folder_action,
            batch_action,
            transcribe_action,
            source_srt_action,
            force_pause_action,
            export_action,
        ):
            self.toolbar.addAction(action)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(self.translation_toggle)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(self.workflow_label)
        project_menu = self.menuBar().addMenu("文件")
        project_menu.addActions(
            (
                media_action,
                folder_action,
                source_srt_action,
                export_action,
                open_subtitles_action,
            )
        )

        process_menu = self.menuBar().addMenu("处理")
        process_menu.addActions(
            (
                batch_action,
                resume_queue_action,
                transcribe_action,
                translate_action,
                force_pause_action,
            )
        )
        process_menu.addSeparator()
        process_menu.addActions((check_action, self.problems_action, replace_action))

        settings_menu = self.menuBar().addMenu("设置")
        settings_menu.addActions((models_action, gpu_action, translation_settings_action))

        window_menu = self.menuBar().addMenu("窗口")
        task_dock_action = self.task_dock.toggleViewAction()
        task_dock_action.setText("任务队列")
        context_dock_action = self.side_dock.toggleViewAction()
        context_dock_action.setText("翻译上下文")
        window_menu.addActions((task_dock_action, context_dock_action))
        window_menu.addSeparator()
        reset_layout_action = QAction("恢复默认布局", self)
        reset_layout_action.triggered.connect(self.reset_window_layout)
        window_menu.addAction(reset_layout_action)

        help_menu = self.menuBar().addMenu("帮助")
        help_menu.addAction(about_action)

    def reset_window_layout(self) -> None:
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.task_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.side_dock)
        self.task_dock.show()
        self.side_dock.show()

    def start_selected_transcription(self) -> None:
        item = self.task_tree.currentItem()
        value = item.data(0, TASK_PATH_ROLE) if item else None
        if value:
            path = Path(str(value)).resolve()
            if path.suffix.lower() == ".srt":
                QMessageBox.information(
                    self, "这是字幕文件", "SRT 只能执行翻译，不能进行语音识别。"
                )
                return
            self._queue_operations([path], "transcribe")
            return
        self.transcribe_media(automatic=False)

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
            media = project.resolve_media()
            if media:
                self.setWindowTitle(f"{APP_NAME} {__version__} — {media.name}")
                self.player.load(media)
            elif source_srt := project.get_meta("source_srt"):
                self.setWindowTitle(f"{APP_NAME} {__version__} — {Path(source_srt).name}")
            else:
                self.setWindowTitle(f"{APP_NAME} {__version__}")
        else:
            self.prompt_edit.clear()
            self.glossary_edit.clear()
            self.setWindowTitle(f"{APP_NAME} {__version__}")

    def add_media(self) -> None:
        filters = (
            "音视频文件 (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus "
            "*.mp4 *.mkv *.mov *.avi *.webm);;所有文件 (*)"
        )
        path, _ = QFileDialog.getOpenFileName(self, "选择要转文字的音视频", "", filters)
        if path:
            media = Path(path).resolve()
            self._enqueue_media_files([media], media.parent)

    def add_source_srt(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择要翻译的原文 SRT",
            str((self.paths.data / "subtitles" / "原文").resolve()),
            "SRT 字幕 (*.srt)",
        )
        if not path:
            return
        source = Path(path).resolve()
        self._enqueue_media_files([source], source.parent)
        self._queue_operations([source], "translate")

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
            group = QTreeWidgetItem([f"📁 {root.name or root}"])
            group.setToolTip(0, str(root))
            group.setFlags(
                group.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsAutoTristate
            )
            self.task_tree.addTopLevelItem(group)
            self.task_groups[root] = group
        added = 0
        for media in media_files:
            media = media.resolve()
            if media.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES or not media.is_file():
                continue
            item = self.task_items.get(media)
            if item is None:
                try:
                    label = str(media.relative_to(root))
                except ValueError:
                    label = media.name
                item = QTreeWidgetItem([label])
                item.setData(0, TASK_PATH_ROLE, str(media))
                item.setToolTip(0, str(media))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked)
                group.addChild(item)
                self.task_items[media] = item
                self.task_progress[media] = 0
                self._set_media_status(media, "等待选择操作", progress=0)
                added += 1
        group.setText(0, f"📁 {root.name or root}（{group.childCount()} 个文件）")
        group.setExpanded(True)
        if added:
            self.statusBar().showMessage(
                f"已加入 {added} 个媒体文件；不会自动识别，请手动开始",
                8000,
            )

    def _start_next_queued_media(self) -> None:
        if self.current_media_path is not None:
            return
        if self.transcription_thread and self.transcription_thread.isRunning():
            return
        if self.translation_thread and self.translation_thread.isRunning():
            return
        while self.pending_media:
            media = self.pending_media.popleft()
            self.current_action = self.pending_actions.pop(media, "auto")
            if not media.is_file():
                self.queued_media.discard(media)
                self._set_media_status(media, "文件不存在")
                continue
            self.current_media_path = media
            self._set_media_status(media, "正在导入", progress=3)
            self._ingest_media(media)
            return
        self.statusBar().showMessage("任务队列已完成", 5000)

    def resume_media_queue(self) -> None:
        if self.current_media_path and not (
            self.transcription_thread and self.transcription_thread.isRunning()
        ):
            if self.current_action == "translate":
                self.translate_pending()
            elif self.current_action == "transcribe":
                self.transcribe_media(automatic=True)
            elif (
                self.project
                and self.project.get_settings().translation_enabled
                and self.project.list_segments()
            ):
                self.translate_pending()
            else:
                self.transcribe_media(automatic=True)
            return
        self._start_next_queued_media()

    def _set_media_status(
        self,
        media: Path,
        status: str,
        *,
        progress: int | None = None,
        show_progress: bool | None = None,
    ) -> None:
        media = media.resolve()
        self.task_status[media] = status
        if progress is not None:
            self.task_progress[media] = max(0, min(progress, 100))
        if show_progress is None:
            show_progress = progress is not None and 0 < progress < 100
        if item := self.task_items.get(media):
            icon = (
                self._progress_icon(self.task_progress.get(media, 0))
                if show_progress
                else QIcon()
            )
            item.setIcon(0, icon)
        if self.detail_media_path == media:
            self._refresh_task_details(media)

    @staticmethod
    def _progress_icon(value: int) -> QIcon:
        width, height = 40, 8
        pixmap = QPixmap(width, height)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setPen(QColor("#8fa4b8"))
        painter.setBrush(QColor("#e4ebf1"))
        painter.drawRoundedRect(0, 0, width - 1, height - 1, 3, 3)
        fill_width = max(2, round((width - 2) * max(0, min(value, 100)) / 100))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#1683df"))
        painter.drawRoundedRect(1, 1, fill_width, height - 2, 2, 2)
        painter.end()
        return QIcon(pixmap)

    def _finish_current_media(self, status: str) -> None:
        media = self.current_media_path
        if media is None:
            return
        completed = any(marker in status for marker in ("已完成", "识别完成", "翻译完成"))
        self._set_media_status(
            media,
            status,
            progress=100 if completed else None,
            show_progress=False,
        )
        self.queued_media.discard(media)
        self.current_media_path = None
        self.current_action = "auto"
        QTimer.singleShot(0, self._start_next_queued_media)

    def _open_task_media(self, item: QTreeWidgetItem, _column: int) -> None:
        value = item.data(0, TASK_PATH_ROLE)
        if not value:
            item.setExpanded(not item.isExpanded())
            return
        media = Path(str(value)).resolve()
        self._show_task_details(media)
        if (self.transcription_thread and self.transcription_thread.isRunning()) or (
            self.translation_thread and self.translation_thread.isRunning()
        ):
            return
        self._load_input_state(media)

    def _show_task_context_menu(self, position) -> None:
        item = self.task_tree.itemAt(position)
        if item is None:
            return
        value = item.data(0, TASK_PATH_ROLE)
        if not value:
            return
        media = Path(str(value)).resolve()
        menu = QMenu(self)
        transcribe = menu.addAction("转文字 / 重新识别")
        translate = menu.addAction("翻译原文 SRT")
        transcribe.setEnabled(media.suffix.lower() in MEDIA_SUFFIXES)
        menu.addSeparator()
        details = menu.addAction("查看任务详情")
        sovits = menu.addAction("SoVITS 改配音（暂未实现）")
        sovits.setEnabled(False)
        selected = menu.exec(self.task_tree.viewport().mapToGlobal(position))
        if selected == transcribe:
            self._queue_operations([media], "transcribe")
        elif selected == translate:
            self._queue_operations([media], "translate")
        elif selected == details:
            self._show_task_details(media)
            if not self._task_is_running():
                self._load_input_state(media)

    def show_batch_operations(self) -> None:
        if not self.task_items:
            QMessageBox.information(self, "没有媒体", "请先添加音视频文件或文件夹。")
            return
        groups: list[tuple[Path, list[tuple[Path, bool]]]] = []
        for root, group in self.task_groups.items():
            media_items: list[tuple[Path, bool]] = []
            for index in range(group.childCount()):
                child = group.child(index)
                value = child.data(0, TASK_PATH_ROLE)
                if value:
                    media_items.append(
                        (
                            Path(str(value)).resolve(),
                            child.checkState(0) == Qt.CheckState.Checked,
                        )
                    )
            groups.append((root, media_items))
        dialog = BatchOperationDialog(groups, parent=self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        selected = set(dialog.selected_paths())
        for media, item in self.task_items.items():
            item.setCheckState(
                0,
                Qt.CheckState.Checked if media in selected else Qt.CheckState.Unchecked,
            )
        ordered = sorted(selected, key=lambda path: str(path).casefold())
        self._queue_operations(ordered, dialog.selected_operation)

    def _queue_operations(self, media_files: list[Path], operation: str) -> None:
        if operation == "sovits":
            QMessageBox.information(self, "暂未实现", "SoVITS 改配音将在后续版本提供。")
            return
        added = 0
        for media in media_files:
            media = media.resolve()
            if media not in self.task_items or media in self.queued_media:
                continue
            if operation == "transcribe" and media.suffix.lower() == ".srt":
                self._set_media_status(media, "SRT 只能翻译")
                continue
            if media == self.current_media_path:
                continue
            self.pending_media.append(media)
            self.pending_actions[media] = operation
            self.queued_media.add(media)
            label = "等待转文字" if operation == "transcribe" else "等待翻译"
            self._set_media_status(media, label, progress=0)
            added += 1
        if not added:
            QMessageBox.information(self, "没有加入任务", "所选文件已在处理队列中。")
            return
        self.statusBar().showMessage(f"已加入 {added} 个批量任务", 5000)
        QTimer.singleShot(0, self._start_next_queued_media)

    def _task_is_running(self) -> bool:
        return bool(
            (self.transcription_thread and self.transcription_thread.isRunning())
            or (self.translation_thread and self.translation_thread.isRunning())
        )

    def force_pause_tasks(self) -> None:
        if not self._task_is_running() and not self.pending_media:
            QMessageBox.information(self, "没有运行中的任务", "当前没有需要暂停的识别或翻译任务。")
            return
        self._request_force_pause(wait=False)
        self.statusBar().showMessage("正在强制暂停并释放模型/API 请求……")
        QTimer.singleShot(2500, self._finish_force_pause)

    def _request_force_pause(self, *, wait: bool) -> None:
        for media in tuple(self.pending_media):
            self._set_media_status(media, "已取消排队", show_progress=False)
            self.queued_media.discard(media)
        self.pending_media.clear()
        self.pending_actions.clear()
        if self.transcription_thread and self.transcription_thread.isRunning():
            self.transcription_thread.force_stop()
        if self.translation_thread and self.translation_thread.isRunning():
            self.translation_thread.force_stop()
        if wait:
            for thread in (self.transcription_thread, self.translation_thread):
                if thread and thread.isRunning() and not thread.wait(2000):
                    thread.terminate()
                    thread.wait(500)

    def _finish_force_pause(self) -> None:
        for thread in (self.transcription_thread, self.translation_thread):
            if thread and thread.isRunning():
                thread.terminate()
                thread.wait(300)
        if self.current_media_path:
            self._finish_current_media("已强制暂停")
        self.statusBar().showMessage("任务已强制暂停；已保存的识别/翻译批次不会丢失", 8000)

    def _show_task_details(self, media: Path) -> None:
        self.detail_media_path = media.resolve()
        self.task_detail.setVisible(True)
        self._refresh_task_details(self.detail_media_path)

    def _refresh_task_details(self, media: Path) -> None:
        media = media.resolve()
        self.detail_path_label.setText(f"文件：{media}")
        status = self.task_status.get(media, "未加入任务队列")
        self.detail_status_label.setText(f"当前阶段：{status}")
        self.detail_progress.setRange(0, 100)
        self.detail_progress.setValue(self.task_progress.get(media, 0))
        self.detail_progress.setFormat("%p%")

    def _ingest_media(self, media_path: Path) -> None:
        media_path = media_path.resolve()
        if media_path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
            QMessageBox.information(
                self,
                "不支持的文件",
                "请拖入支持的音视频或原文 SRT 文件。",
            )
            self._finish_current_media("不支持的格式")
            return
        if self.transcription_thread and self.transcription_thread.isRunning():
            QMessageBox.information(self, "识别正在运行", "请等待当前媒体识别完成。")
            return
        if self.translation_thread and self.translation_thread.isRunning():
            QMessageBox.information(self, "翻译正在运行", "请等待当前媒体翻译完成。")
            return
        if not self._load_input_state(media_path):
            self._finish_current_media("导入失败")
            return
        existing = self.project.list_segments()
        if self.current_action == "transcribe":
            self._set_media_status(media_path, "等待转文字", progress=0)
            QTimer.singleShot(0, lambda: self.transcribe_media(automatic=True))
            return
        if self.current_action == "translate":
            if not existing:
                self._finish_current_media("无法翻译 · 尚无原文字幕")
                return
            settings = self.project.get_settings()
            settings.translation_enabled = True
            self.project.save_settings(settings)
            self.translation_toggle.blockSignals(True)
            self.translation_toggle.setChecked(True)
            self.translation_toggle.blockSignals(False)
            self._update_workflow_label()
            self._set_media_status(media_path, "等待翻译", progress=0)
            QTimer.singleShot(0, self.translate_pending)
            return
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
            self._set_media_status(media_path, "等待识别", progress=0)
            QTimer.singleShot(0, lambda: self.transcribe_media(automatic=True))

    def _load_input_state(self, path: Path) -> bool:
        if path.suffix.lower() == ".srt":
            return self._load_source_srt(path)
        return self._load_media_project(path)

    def _internal_state_path(self, source: Path) -> Path:
        fingerprint = quick_file_fingerprint(source)
        safe_stem = re.sub(r"[^\w\-]+", "_", source.stem, flags=re.UNICODE).strip("_")
        safe_stem = safe_stem[:60] or "subtitle"
        state_dir = self.paths.data / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / f"{safe_stem}-{fingerprint[:12]}.sqlite3"

    def _load_media_project(self, media_path: Path) -> bool:
        media_path = media_path.resolve()
        if not media_path.is_file() or media_path.suffix.lower() not in MEDIA_SUFFIXES:
            QMessageBox.information(self, "媒体不可用", f"找不到或不支持该文件：\n{media_path}")
            return False
        migration_removed = 0
        try:
            fingerprint = quick_file_fingerprint(media_path)
            safe_stem = re.sub(r"[^\w\-]+", "_", media_path.stem, flags=re.UNICODE).strip("_")
            safe_stem = safe_stem[:60] or "media"
            state_path = self._internal_state_path(media_path)
            legacy_path = (
                self.paths.data / "projects" / f"{safe_stem}-{fingerprint[:12]}.vstproj"
            )
            migrated_legacy = not state_path.exists() and legacy_path.exists()
            if migrated_legacy:
                shutil.copy2(legacy_path, state_path)
            if state_path.exists():
                project = Project.open(state_path)
            else:
                project = Project.create_state(
                    state_path,
                    ProjectSettings(
                        translation_enabled=self.global_settings.last_translation_enabled
                    ),
                )
            if migrated_legacy and project.has_timeline_regressions():
                migration_removed = project.discard_unlocked_segments()
                project.set_meta(
                    "migration_note",
                    f"discarded_duplicate_segments:{migration_removed}",
                )
                project.set_meta("source_srt_invalid", "1")
            project.set_media(media_path)
            project.set_meta("state_kind", "media")
            self._set_project(project)
            self.global_settings.last_project = str(state_path)
            self.settings_store.save(self.global_settings)
            self.player.load(media_path)
            self._show_task_details(media_path)
            if migration_removed:
                self.statusBar().showMessage(
                    f"检测到旧时间轴重复，已在内部新状态中隔离 {migration_removed} 条；"
                    "请重新执行第一步识别",
                    12000,
                )
            return True
        except Exception as exc:
            QMessageBox.critical(self, "无法导入媒体", str(exc))
            return False

    def _load_source_srt(self, source_path: Path) -> bool:
        source_path = source_path.resolve()
        if not source_path.is_file() or source_path.suffix.lower() != ".srt":
            QMessageBox.information(self, "字幕不可用", f"找不到 SRT 文件：\n{source_path}")
            return False
        try:
            state_path = self._internal_state_path(source_path)
            if state_path.exists():
                project = Project.open(state_path)
            else:
                project = Project.create_state(
                    state_path,
                    ProjectSettings(translation_enabled=True),
                )
                project.replace_all_segments(
                    parse_srt(source_path),
                    reason="srt_import",
                )
            project.set_meta("state_kind", "source_srt")
            project.set_meta("source_srt", str(source_path))
            settings = project.get_settings()
            settings.translation_enabled = True
            project.save_settings(settings)
            self._set_project(project)
            self.translation_toggle.blockSignals(True)
            self.translation_toggle.setChecked(True)
            self.translation_toggle.blockSignals(False)
            self._update_workflow_label()
            self.global_settings.last_project = str(state_path)
            self.settings_store.save(self.global_settings)
            self._show_task_details(source_path)
            return True
        except (InvalidSubtitleError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "无法读取原文 SRT", str(exc))
            return False

    def show_model_manager(self) -> None:
        offline = self.project.get_settings().offline if self.project else False
        dialog = ModelManagerDialog(
            ModelManager(self.paths, bundled_resource("models/manifest.json")),
            offline=offline,
            parent=self,
        )
        dialog.exec()

    def show_gpu_settings(self) -> bool:
        offline = self.project.get_settings().offline if self.project else False
        dialog = GPUSettingsDialog(
            GPURuntimeManager(self.paths),
            profile_id=self.global_settings.asr_profile,
            offline=offline,
            parent=self,
        )
        if not dialog.exec():
            return False
        profile = selected_profile(dialog.selected_profile_id)
        self.global_settings.asr_profile = profile.id
        self.global_settings.asr_device = profile.device
        self.global_settings.asr_compute_type = profile.compute_type
        self.settings_store.save(self.global_settings)
        self.statusBar().showMessage(f"已选择 GPU 推理档位：{profile.name}", 6000)
        return True

    def transcribe_media(self, *, automatic: bool = False) -> None:
        if not self._require_project():
            return
        project_media = self.project.resolve_media()
        if project_media is None:
            QMessageBox.information(self, "没有媒体", "请先添加或重新定位音视频文件。")
            return
        if self.current_media_path is None and project_media.resolve() in self.task_items:
            self.current_media_path = project_media.resolve()
            self.current_action = "auto"
            self.queued_media.add(self.current_media_path)
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
        else:
            model_id, ok = QInputDialog.getItem(
                self, "选择识别模型", "模型：", installed, editable=False
            )
            if not ok:
                return
            if not self.show_gpu_settings():
                return
        profile = selected_profile(self.global_settings.asr_profile)
        device = profile.device
        compute_type = profile.compute_type
        if device == "cuda" and not GPURuntimeManager(self.paths).is_installed():
            QMessageBox.information(
                self,
                "GPU 运行库未安装",
                "当前档位需要 CUDA 12.x、cuBLAS 和 cuDNN 9。\n"
                "请在“GPU 推理设置”中下载绿色运行库；"
                "程序不会自动改用 CPU。",
            )
            if not self.show_gpu_settings():
                return
            profile = selected_profile(self.global_settings.asr_profile)
            device = profile.device
            compute_type = profile.compute_type
            if device == "cuda" and not GPURuntimeManager(self.paths).is_installed():
                if self.current_media_path:
                    self._set_media_status(self.current_media_path, "等待安装 GPU 运行库")
                return
        settings.asr_model = model_id
        self.project.save_settings(settings)
        thread = TranscriptionThread(
            project_path=self.project.path,
            paths=self.paths,
            model_id=model_id,
            device=device,
            compute_type=compute_type,
            parent=self,
        )
        if self.current_media_path:
            self._set_media_status(
                self.current_media_path,
                f"识别中 · {model_id} / {device} / {compute_type}",
                progress=1,
            )
        self.statusBar().showMessage("正在执行音频标准化、VAD 和识别……")
        thread.succeeded.connect(self._transcription_succeeded)
        thread.failed.connect(self._transcription_failed)
        thread.cancelled.connect(self._task_cancelled)
        thread.progress.connect(self._transcription_progress)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: setattr(self, "transcription_thread", None))
        self.transcription_thread = thread
        thread.start()

    def _transcription_progress(self, stage: str, completed: int, total: int) -> None:
        if not self.current_media_path:
            return
        percent = round(completed / max(total, 1) * 100)
        self._set_media_status(self.current_media_path, stage, progress=percent)
        self.statusBar().showMessage(stage)

    def _transcription_succeeded(self, task_id: str, segment_count: int) -> None:
        if self.project:
            self.table_model.refresh()
            self._filter_problem_rows(self.problems_action.isChecked())
        if segment_count:
            try:
                path = self._write_current_srt(ExportContent.SOURCE)
            except Exception as exc:
                QMessageBox.critical(self, "原文 SRT 保存失败", str(exc))
                self._finish_current_media("识别完成 · SRT 保存失败")
                return
            self.statusBar().showMessage(
                f"第一步完成：{segment_count} 条字幕，原文 SRT 已保存到 {path}",
                10000,
            )
            QMessageBox.information(
                self,
                "原文 SRT 已生成",
                f"第一步已经完成：\n{path}\n\n"
                "下一步请点击“翻译原文 SRT”生成中文 SRT。",
            )
            self._finish_current_media(f"原文 SRT 已生成 · {segment_count} 条")
        else:
            self._finish_current_media("未检测到语音")

    def _transcription_failed(self, message: str) -> None:
        if any(
            marker in message.lower()
            for marker in ("cublas64_12.dll", "cudnn", "cuda driver", "cuda_error")
        ):
            message = (
                "GPU 运行库未安装完整或与当前显卡不兼容。\n\n"
                f"原始错误：{message}\n\n"
                "请打开“GPU 推理设置”，为 RTX 50 系选择推荐档位并"
                "安装 CUDA 12.9 绿色运行库。程序不会自动切换到 CPU。"
            )
        QMessageBox.critical(self, "识别失败", message)
        self._finish_current_media("识别失败")
        if self.project:
            self.table_model.refresh()

    def _task_cancelled(self) -> None:
        if self.project:
            self.table_model.refresh()
        self._finish_current_media("已强制暂停")
        self.statusBar().showMessage("任务已强制暂停；已完成批次已经保存", 8000)

    def _translation_toggled(self, checked: bool) -> None:
        self.global_settings.last_translation_enabled = checked
        self.settings_store.save(self.global_settings)
        if self.project:
            PipelineCoordinator(self.project).set_translation_enabled(checked)
        self._update_workflow_label()
        self.statusBar().showMessage(
            "已启用翻译；请点击“翻译原文 SRT”开始第二步"
            if checked
            else "已关闭翻译；不会发起新的翻译请求",
            6000,
        )

    def _update_workflow_label(self) -> None:
        if self.translation_toggle.isChecked():
            self.workflow_label.setText("当前：分两步生成原文 SRT 和中文 SRT")
        else:
            self.workflow_label.setText("当前：第一步仅生成原文 SRT")

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
        try:
            source_srt = self._source_srt_path()
            if source_srt is None or not source_srt.is_file():
                QMessageBox.information(
                    self,
                    "没有原文 SRT",
                    "请先完成语音识别生成原文 SRT，或点击“翻译原文 SRT”选择文件。",
                )
                return
            self.project.replace_all_segments(
                parse_srt(source_srt, language=self.project.get_settings().source_language),
                reason="srt_translation_source",
            )
            self.project.set_meta("source_srt", str(source_srt))
            self.table_model.refresh()
        except (InvalidSubtitleError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "原文 SRT 无法读取", str(exc))
            return
        if (
            not self.global_settings.translation_base_url
            or not self.global_settings.translation_model
        ) and not self.show_translation_settings():
            self.statusBar().showMessage("识别已完成；尚未配置翻译服务", 8000)
            if self.current_media_path:
                self._finish_current_media("等待配置翻译服务")
            return
        settings = self.project.get_settings()
        provider_id = self.global_settings.translation_provider or "openai-compatible"
        base_url = self.global_settings.translation_base_url
        model = self.global_settings.translation_model
        is_local = base_url.startswith(("http://127.0.0.1", "http://localhost"))
        key = "" if is_local else (CredentialStore().get(provider_id) or "")
        if not is_local and not key:
            if not self.show_translation_settings():
                if self.current_media_path:
                    self._finish_current_media("等待配置翻译服务")
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
            self._set_media_status(
                self.current_media_path,
                f"翻译中 · {provider_id} / {model}",
                progress=0,
                show_progress=True,
            )
        thread.progress.connect(self._translation_progress)
        thread.progress.connect(
            lambda done, total: self.statusBar().showMessage(f"翻译批次 {done}/{total}")
        )
        thread.succeeded.connect(self._translation_succeeded)
        thread.failed.connect(self._translation_failed)
        thread.cancelled.connect(self._task_cancelled)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: setattr(self, "translation_thread", None))
        self.translation_thread = thread
        thread.start()

    def _translation_progress(self, completed: int, total: int) -> None:
        if not self.current_media_path:
            return
        percent = round(completed / max(total, 1) * 100)
        self._set_media_status(
            self.current_media_path,
            f"正在翻译字幕 {completed}/{total}",
            progress=percent,
        )

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
            try:
                path = self._write_current_srt(ExportContent.TRANSLATION)
            except Exception as exc:
                QMessageBox.critical(self, "中文 SRT 保存失败", str(exc))
                self._finish_current_media("翻译完成 · SRT 保存失败")
                return
            self.statusBar().showMessage(
                f"第二步完成：中文 SRT 已保存到 {path}（新翻译 {completed}，缓存 {cached}）",
                10000,
            )
            QMessageBox.information(self, "中文 SRT 已生成", f"第二步已经完成：\n{path}")
            self._finish_current_media(f"中文 SRT 已生成 · {completed + cached} 条")

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

    def _source_srt_path(self) -> Path | None:
        if not self.project:
            return None
        if self.project.get_meta("source_srt_invalid") == "1":
            return None
        if value := self.project.get_meta("source_srt"):
            return Path(value).resolve()
        media = self.project.resolve_media()
        if media:
            return (self.paths.data / "subtitles" / "原文" / f"{media.stem}.srt").resolve()
        return None

    def _write_current_srt(self, content: ExportContent) -> Path:
        if not self.project:
            raise RuntimeError("当前没有可用字幕。")
        source_srt = self._source_srt_path()
        media = self.project.resolve_media()
        if source_srt:
            stem = source_srt.stem
        elif media:
            stem = media.stem
        else:
            raise RuntimeError("无法确定字幕文件名。")
        directory = {
            ExportContent.SOURCE: "原文",
            ExportContent.TRANSLATION: "中文",
            ExportContent.BILINGUAL: "双语",
        }[content]
        path = self.paths.data / "subtitles" / directory / f"{stem}.srt"
        export_subtitles(
            self.project.list_segments(),
            path,
            output_format=ExportFormat.SRT,
            content=content,
        )
        if content is ExportContent.SOURCE:
            self.project.set_meta("source_srt", str(path.resolve()))
            self.project.set_meta("source_srt_invalid", "0")
        return path.resolve()

    def export_dialog(self) -> None:
        if not self._require_project():
            return
        source_srt = self._source_srt_path()
        media = self.project.resolve_media()
        source_name = source_srt or media
        if source_name is None:
            QMessageBox.information(self, "没有字幕", "请先识别媒体或选择原文 SRT。")
            return
        choices = ["原文"]
        allowed, reason = can_export(self.project.list_segments(), ExportContent.TRANSLATION)
        if allowed:
            choices.extend(["中文", "双语"])
        if len(choices) == 1:
            content_name = "原文"
        else:
            content_name, ok = QInputDialog.getItem(
                self, "导出内容", "内容：", choices, editable=False
            )
            if not ok:
                return
        content_map = {
            "原文": ExportContent.SOURCE,
            "中文": ExportContent.TRANSLATION,
            "双语": ExportContent.BILINGUAL,
        }
        output_directory = self.paths.data / "subtitles" / content_name
        output_directory.mkdir(parents=True, exist_ok=True)
        path = output_directory / f"{source_name.stem}.srt"
        if path.exists():
            answer = QMessageBox.question(
                self,
                "覆盖已有字幕",
                f"字幕文件已经存在：\n{path}\n\n是否覆盖？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            path = self._write_current_srt(content_map[content_name])
            self.statusBar().showMessage(f"已导出：{path}", 5000)
            QMessageBox.information(
                self,
                "导出完成",
                f"已按原文件名导出：\n{path}\n\n"
                "可通过“文件 → 打开字幕文件夹”查看。",
            )
        except TranslationUnavailableError:
            QMessageBox.information(self, "无法导出译文", reason)
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def open_subtitle_folder(self) -> None:
        root = self.paths.data / "subtitles"
        for name in ("原文", "中文", "双语"):
            (root / name).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root.resolve())))

    def _seek_selected(self, index: QModelIndex) -> None:
        if index.isValid() and index.row() < len(self.table_model.segments):
            self.player.seek_ms(self.table_model.segments[index.row()].start_ms)

    def _require_project(self) -> bool:
        if self.project:
            return True
        QMessageBox.information(self, "没有字幕", "请先加入音视频，或选择一份原文 SRT。")
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
            elif resolved.suffix.lower() in SUPPORTED_INPUT_SUFFIXES:
                self._enqueue_media_files([resolved], resolved.parent)
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._task_is_running():
            answer = QMessageBox.question(
                self,
                "任务仍在运行",
                "识别或翻译仍在运行。是否强制暂停任务并退出？\n\n"
                "已经完成并写入项目的批次会保留，当前未完成批次将中断。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._request_force_pause(wait=True)
            if self.current_media_path:
                media = self.current_media_path
                self._set_media_status(media, "已强制暂停", show_progress=False)
                self.queued_media.discard(media)
                self.current_media_path = None
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

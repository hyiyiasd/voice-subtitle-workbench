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
    QGroupBox,
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
    QTextBrowser,
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
TASK_PATH_ROLE = int(Qt.ItemDataRole.UserRole)


class SegmentTableModel(QAbstractTableModel):
    HEADERS = ["é”å®š", "å¼€å§‹", "ç»“æŸ", "åŸæ–‡", "è¯‘æ–‡", "é—®é¢˜"]

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
                "ã€".join(sorted(segment.quality_flags)),
            ]
            return values[column]
        if role == Qt.ItemDataRole.ToolTipRole and column == 4 and segment.translated_text:
            if not segment.has_valid_translation:
                return "åŸæ–‡å·²ä¿®æ”¹ï¼Œæ­¤è¯‘æ–‡å·²è¿‡æœŸã€‚"
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
    progress = Signal(str, int, int)
    succeeded = Signal(str, int)
    failed = Signal(str)

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
                )
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
        self.pending_actions: dict[Path, str] = {}
        self.queued_media: set[Path] = set()
        self.current_media_path: Path | None = None
        self.current_action = "auto"
        self.task_groups: dict[Path, QTreeWidgetItem] = {}
        self.task_items: dict[Path, QTreeWidgetItem] = {}
        self.task_progress: dict[Path, QProgressBar] = {}
        self.task_history: dict[Path, list[str]] = {}
        self.detail_media_path: Path | None = None
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1280, 820)
        self.setAcceptDrops(True)
        self._build_ui()
        self._build_actions()
        self._set_project(None)

    def _build_ui(self) -> None:
        self.toolbar = QToolBar("ä¸»å·¥å…·æ ")
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)
        self.translation_toggle = QCheckBox("å¯ç”¨ç¿»è¯‘")
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
            f'ä½œè€…ï¼š{AUTHOR}ï½œ<a href="{BILIBILI_URL}">Bç«™ä¸»é¡µ</a>ï½œ'
            f'<a href="{GITHUB_URL}/releases">å®˜æ–¹ç‰ˆæœ¬ï¼šGitHub Releases</a>'
        )
        footer.setOpenExternalLinks(False)
        footer.linkActivated.connect(lambda url: QDesktopServices.openUrl(QUrl(url)))
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.setStyleSheet("padding:6px;color:#666")

        central = QWidget()
        layout = QVBoxLayout(central)
        self.drop_hint = QLabel(
            "å°† MP3ã€WAVã€MP4 ç­‰éŸ³è§†é¢‘æˆ–æ•´ä¸ªæ–‡ä»¶å¤¹æ‹–åˆ°è¿™é‡Œï¼›å¯ç”¨ç¿»è¯‘åä¼šåœ¨è¯†åˆ«å®Œæˆåç»§ç»­ç¿»è¯‘"
        )
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setStyleSheet(
            "padding:12px;border:2px dashed #7a8aa0;border-radius:6px;font-size:15px;color:#405060"
        )
        layout.addWidget(self.drop_hint)
        self.task_detail = QGroupBox("æ–‡ä»¶ä»»åŠ¡è¯¦æƒ…")
        self.task_detail.setVisible(False)
        detail_layout = QVBoxLayout(self.task_detail)
        self.detail_path_label = QLabel()
        self.detail_path_label.setWordWrap(True)
        self.detail_status_label = QLabel()
        self.detail_progress = QProgressBar()
        self.detail_log = QTextBrowser()
        self.detail_log.setMaximumHeight(110)
        detail_layout.addWidget(self.detail_path_label)
        detail_layout.addWidget(self.detail_status_label)
        detail_layout.addWidget(self.detail_progress)
        detail_layout.addWidget(self.detail_log)
        layout.addWidget(self.task_detail)
        layout.addWidget(splitter)
        layout.addWidget(footer)
        self.setCentralWidget(central)

        task_dock = QDockWidget("ä»»åŠ¡é˜Ÿåˆ—", self)
        self.task_tree = QTreeWidget()
        self.task_tree.setHeaderLabels(["æ–‡ä»¶å¤¹ / åª’ä½“", "å½“å‰æ“ä½œ", "è¿›åº¦"])
        self.task_tree.setColumnWidth(0, 230)
        self.task_tree.setColumnWidth(1, 180)
        self.task_tree.setColumnWidth(2, 120)
        self.task_tree.itemDoubleClicked.connect(self._open_task_media)
        self.task_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_tree.customContextMenuRequested.connect(self._show_task_context_menu)
        task_dock.setWidget(self.task_tree)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, task_dock)

        side = QTabWidget()
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText("æ ‡é¢˜ã€èƒŒæ™¯ã€äººç‰©å…³ç³»ã€è¯­æ°”å’Œè‡ªç”±ç¿»è¯‘æŒ‡ä»¤")
        self.glossary_edit = QPlainTextEdit()
        self.glossary_edit.setPlaceholderText("æ¯è¡Œï¼šåŸè¯=è¯‘è¯")
        side.addTab(self.prompt_edit, "æç¤ºè¯")
        side.addTab(self.glossary_edit, "æœ¯è¯­")
        side_dock = QDockWidget("ç¿»è¯‘ä¸Šä¸‹æ–‡", self)
        side_dock.setWidget(side)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, side_dock)

    def _build_actions(self) -> None:
        new_action = QAction("æ–°å»ºé¡¹ç›®", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self.new_project)
        open_action = QAction("æ‰“å¼€é¡¹ç›®", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_project)
        media_action = QAction("æ·»åŠ åª’ä½“", self)
        media_action.triggered.connect(self.add_media)
        folder_action = ÷¾=¶‰ËkºwµçeÁÑ¥½¹}Ñ¡É•……¹Í•±˜¹ÑÉ…¹ÍÉ¥ÁÑ¥½¹}Ñ¡É•…¹¥ÍIÕ¹¹¥¹œ ¤¤(€€€€€€€€¤è(€€€€€€€€€€€EQ¥µ•È¹Í¥¹±•M¡½Ğ À°Í•±˜¹ÑÉ…¹Í±…Ñ•}Á•¹‘¥¹œ¤((€€€‘•˜}ÕÁ‘…Ñ•}İ½É­™±½İ}±…‰•°¡Í•±˜¤€´ø9½¹”è(€€€€€€€¥˜Í•±˜¹ÑÉ…¹Í±…Ñ¥½¹}Ñ½±”¹¥Í¡•­• ¤è(€€€€€€€€€€€Í•±˜¹İ½É­™±½İ}±…‰•°¹Í•ÑQ•áĞ ‹–öO–&7¾òk¢¾–"¯–B;şï¢¾G’âëº’öO’â·šZˆ¤(€€€€€€€•±Í”è(€€€€€€€€€€€Í•±˜¹İ½É­™±½İ}±…‰•°¹Í•ÑQ•áĞ ‹–öO–&7¾òk’î¢¾–"¯–æÛ–¾ó–ë–:šZ–¶_–æTˆ¤((€€€‘•˜¡•­}ÅÕ…±¥Ñä¡Í•±˜¤€´ø9½¹”è(€€€€€€€¥˜¹½ĞÍ•±˜¹}É•ÅÕ¥É•}ÁÉ½©•Ğ ¤è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€Í•µ•¹ÑÌ€ôÍ•±˜¹ÁÉ½©•Ğ¹±¥ÍÑ}Í•µ•¹ÑÌ ¤(€€€€€€€…ÁÁ±å}ÅÕ…±¥Ñå}™±…Ì¡Í•µ•¹ÑÌ°Í•±˜¹ÁÉ½©•Ğ¹•Ñ}Í•ÑÑ¥¹Ì ¤¹ÍÕ‰Ñ¥Ñ±”¤(€€€€€€€İ¥Ñ Í•±˜¹ÁÉ½©•Ğ¹½¹¹•Ñ¥½¸è(€€€€€€€€€€€™½ÈÍ•µ•¹Ğ¥¸Í•µ•¹ÑÌè(€€€€€€€€€€€€€€€¥µÁ½ÉĞ©Í½¸((€€€€€€€€€€€€€€€Í•±˜¹ÁÉ½©•Ğ¹½¹¹•Ñ¥½¸¹•á•ÕÑ” (€€€€€€€€€€€€€€€€€€€€‰UAQÍ•µ•¹ÑÌMPÅÕ…±¥Ñå}™±…Í}©Í½¸ôü]!I¥ôüˆ°(€€€€€€€€€€€€€€€€€€€€¡©Í½¸¹‘ÕµÁÌ¡Í½ÉÑ•¡Í•µ•¹Ğ¹ÅÕ…±¥Ñå}™±…Ì¤°•¹ÍÕÉ•}…Í¥¤õ…±Í”¤°Í•µ•¹Ğ¹¥¤°(€€€€€€€€€€€€€€€€¤(€€€€€€€Í•±˜¹Ñ…‰±•}µ½‘•°¹É•™É•Í  ¤(€€€€€€€Í•±˜¹}™¥±Ñ•É}ÁÉ½‰±•µ}É½İÌ¡Í•±˜¹ÁÉ½‰±•µÍ}…Ñ¥½¸¹¥Í¡•­• ¤¤(€€€€€€€ÁÉ½‰±•µ}½Õ¹Ğ€ôÍÕ´¡‰½½°¡¥Ñ•´¹ÅÕ…±¥Ñå}™±…Ì¤™½È¥Ñ•´¥¸Í•µ•¹ÑÌ¤(€€€€€€€Í•±˜¹ÍÑ…ÑÕÍ	…È ¤¹Í¡½İ5•ÍÍ…”¡˜‹šš~—–º3š"C¾òiíÁÉ½‰±•µ}½Õ¹Ñôƒšv‡–¶_–æW¦r¢ššÎ£š<ˆ°€ÔÀÀÀ¤((€€€‘•˜Í¡½İ}ÑÉ…¹Í±…Ñ¥½¹}Í•ÑÑ¥¹Ì¡Í•±˜¤€´ø‰½½°è(€€€€€€€ÁÉ½Ù¥‘•È€ôÍ•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}ÁÉ½Ù¥‘•È½È€‰½Á•¹…¤ˆ(€€€€€€€É•‘•¹Ñ¥…±}ÍÑ½É”€ôÉ•‘•¹Ñ¥…±MÑ½É” ¤(€€€€€€€¡…Í}Í…Ù•‘}­•ä€ô‰½½°¡É•‘•¹Ñ¥…±}ÍÑ½É”¹•Ğ¡ÁÉ½Ù¥‘•È¤¤(€€€€€€€‘¥…±½œ€ôQÉ…¹Í±…Ñ¥½¹M•ÑÑ¥¹Í¥…±½œ (€€€€€€€€€€€ÁÉ½Ù¥‘•ÈõÁÉ½Ù¥‘•È°(€€€€€€€€€€€‰…Í•}ÕÉ°õÍ•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}‰…Í•}ÕÉ°°(€€€€€€€€€€€µ½‘•°õÍ•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}µ½‘•°°(€€€€€€€€€€€¡…Í}Í…Ù•‘}­•äõ¡…Í}Í…Ù•‘}­•ä°(€€€€€€€€€€€Á…É•¹ĞõÍ•±˜°(€€€€€€€€¤(€€€€€€€¥˜‘¥…±½œ¹•á•Œ ¤€„ô‘¥…±½œ¹¥…±½½‘”¹•ÁÑ•è(€€€€€€€€€€€É•ÑÕÉ¸…±Í”(€€€€€€€Ù…±Õ•Ì€ô‘¥…±½œ¹Ù…±Õ•Ì ¤(€€€€€€€¥˜¹½ĞÙ…±Õ•Ì¹‰…Í•}ÕÉ°½È¹½ĞÙ…±Õ•Ì¹µ½‘•°è(€€€€€€€€€€€E5•ÍÍ…•	½à¹¥¹™½Éµ…Ñ¥½¸¡Í•±˜°€‹¦7ö»’â7–º3šVĞˆ°€‹¢¾ß–†¯–d	…Í”UI0ƒ–J3š¢‡–z/–B7Ãˆ¤(€€€€€€€€€€€É•ÑÕÉ¸…±Í”(€€€€€€€¥Í}±½…°€ôÙ…±Õ•Ì¹‰…Í•}ÕÉ°¹ÍÑ…ÉÑÍİ¥Ñ   ‰¡ÑÑÀè¼¼ÄÈÜ¸À¸À¸Äˆ°€‰¡ÑÑÀè¼½±½…±¡½ÍĞˆ¤¤(€€€€€€€¥˜Ù…±Õ•Ì¹…Á¥}­•äè(€€€€€€€€€€€É•‘•¹Ñ¥…±}ÍÑ½É”¹Í•Ğ¡Ù…±Õ•Ì¹ÁÉ½Ù¥‘•È°Ù…±Õ•Ì¹…Á¥}­•ä¤(€€€€€€€•±¥˜¹½Ğ¥Í}±½…°…¹¹½ĞÉ•‘•¹Ñ¥…±}ÍÑ½É”¹•Ğ¡Ù…±Õ•Ì¹ÁÉ½Ù¥‘•È¤è(€€€€€€€€€€€E5•ÍÍ…•	½à¹¥¹™½Éµ…Ñ¥½¸¡Í•±˜°€‹òë–ÂDA$-•äˆ°€‹¢şs¢/şï¢¾Gšr7–*‡¦r¢šA$-•çˆ¤(€€€€€€€€€€€É•ÑÕÉ¸…±Í”(€€€€€€€Í•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}ÁÉ½Ù¥‘•È€ôÙ…±Õ•Ì¹ÁÉ½Ù¥‘•È(€€€€€€€Í•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}‰…Í•}ÕÉ°€ôÙ…±Õ•Ì¹‰…Í•}ÕÉ°(€€€€€€€Í•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}µ½‘•°€ôÙ…±Õ•Ì¹µ½‘•°(€€€€€€€Í•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}ÍÑÉÕÑÕÉ•‘}½ÕÑÁÕĞ€ôÙ…±Õ•Ì¹ÍÑÉÕÑÕÉ•‘}½ÕÑÁÕĞ(€€€€€€€Í•±˜¹Í•ÑÑ¥¹Í}ÍÑ½É”¹Í…Ù”¡Í•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¤(€€€€€€€¥˜Í•±˜¹ÁÉ½©•Ğè(€€€€€€€€€€€Í•ÑÑ¥¹Ì€ôÍ•±˜¹ÁÉ½©•Ğ¹•Ñ}Í•ÑÑ¥¹Ì ¤(€€€€€€€€€€€Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}ÁÉ½Ù¥‘•È€ôÙ…±Õ•Ì¹ÁÉ½Ù¥‘•È(€€€€€€€€€€€Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}µ½‘•°€ôÙ…±Õ•Ì¹µ½‘•°(€€€€€€€€€€€Í•±˜¹ÁÉ½©•Ğ¹Í…Ù•}Í•ÑÑ¥¹Ì¡Í•ÑÑ¥¹Ì¤(€€€€€€€Í•±˜¹ÍÑ…ÑÕÍ	…È ¤¹Í¡½İ5•ÍÍ…”¡˜‹–ŞË’şw–¶cşï¢¾Gšr7–*‡¾òiíÙ…±Õ•Ì¹ÁÉ½Ù¥‘•Éô€¼íÙ…±Õ•Ì¹µ½‘•±ôˆ°€ØÀÀÀ¤(€€€€€€€É•ÑÕÉ¸QÉÕ”((€€€‘•˜ÑÉ…¹Í±…Ñ•}Á•¹‘¥¹œ¡Í•±˜¤€´ø9½¹”è(€€€€€€€¥˜¹½ĞÍ•±˜¹}É•ÅÕ¥É•}ÁÉ½©•Ğ ¤è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€¥˜¹½ĞÍ•±˜¹ÁÉ½©•Ğ¹•Ñ}Í•ÑÑ¥¹Ì ¤¹ÑÉ…¹Í±…Ñ¥½¹}•¹…‰±•è(€€€€€€€€€€€E5•ÍÍ…•	½à¹¥¹™½Éµ…Ñ¥½¸¡Í•±˜°€‹šr«–B¿R£şï¢¾Dˆ°€‹¢¾ß–#–ò–B¿¦†Û¦£Šs–B¿R£şï¢¾GŠw–ò–Ïˆ¤(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€¥˜Í•±˜¹ÑÉ…¹Í±…Ñ¥½¹}Ñ¡É•……¹Í•±˜¹ÑÉ…¹Í±…Ñ¥½¹}Ñ¡É•…¹¥ÍIÕ¹¹¥¹œ ¤è(€€€€€€€€€€€E5•ÍÍ…•	½à¹¥¹™½Éµ…Ñ¥½¸¡Í•±˜°€‹şï¢¾Gš¶–r£¢şC¢†0ˆ°€‹¢¾ß¶'–ú–öO–&7şï¢¾Gš&çš²‡–º3š"Cˆ¤(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€¥˜€ (€€€€€€€€€€€¹½ĞÍ•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}‰…Í•}ÕÉ°(€€€€€€€€€€€½È¹½ĞÍ•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}µ½‘•°(€€€€€€€€¤…¹¹½ĞÍ•±˜¹Í¡½İ}ÑÉ…¹Í±…Ñ¥½¹}Í•ÑÑ¥¹Ì ¤è(€€€€€€€€€€€Í•±˜¹ÍÑ…ÑÕÍ	…È ¤¹Í¡½İ5•ÍÍ…” ‹¢¾–"¯–ŞË–º3š"C¾òo–Âkšr«¦7ö»şï¢¾Gšr7–*„ˆ°€àÀÀÀ¤(€€€€€€€€€€€¥˜Í•±˜¹ÕÉÉ•¹Ñ}µ•‘¥…}Á…Ñ è(€€€€€€€€€€€€€€€Í•±˜¹}™¥¹¥Í¡}ÕÉÉ•¹Ñ}µ•‘¥„ ‹¶'–ú¦7ö»şï¢¾Gšr7–*„ˆ¤(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€Í•ÑÑ¥¹Ì€ôÍ•±˜¹ÁÉ½©•Ğ¹•Ñ}Í•ÑÑ¥¹Ì ¤(€€€€€€€ÁÉ½Ù¥‘•É}¥€ôÍ•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}ÁÉ½Ù¥‘•È½È€‰½Á•¹…¤µ½µÁ…Ñ¥‰±”ˆ(€€€€€€€‰…Í•}ÕÉ°€ôÍ•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}‰…Í•}ÕÉ°(€€€€€€€µ½‘•°€ôÍ•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}µ½‘•°(€€€€€€€¥Í}±½…°€ô‰…Í•}ÕÉ°¹ÍÑ…ÉÑÍİ¥Ñ   ‰¡ÑÑÀè¼¼ÄÈÜ¸À¸À¸Äˆ°€‰¡ÑÑÀè¼½±½…±¡½ÍĞˆ¤¤(€€€€€€€­•ä€ô€ˆˆ¥˜¥Í}±½…°•±Í”€¡É•‘•¹Ñ¥…±MÑ½É” ¤¹•Ğ¡ÁÉ½Ù¥‘•É}¥¤½È€ˆˆ¤(€€€€€€€¥˜¹½Ğ¥Í}±½…°…¹¹½Ğ­•äè(€€€€€€€€€€€¥˜¹½ĞÍ•±˜¹Í¡½İ}ÑÉ…¹Í±…Ñ¥½¹}Í•ÑÑ¥¹Ì ¤è(€€€€€€€€€€€€€€€¥˜Í•±˜¹ÕÉÉ•¹Ñ}µ•‘¥…}Á…Ñ è(€€€€€€€€€€€€€€€€€€€Í•±˜¹}™¥¹¥Í¡}ÕÉÉ•¹Ñ}µ•‘¥„ ‹¶'–ú¦7ö»şï¢¾Gšr7–*„ˆ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€€€€€ÁÉ½Ù¥‘•É}¥€ôÍ•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}ÁÉ½Ù¥‘•È(€€€€€€€€€€€‰…Í•}ÕÉ°€ôÍ•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}‰…Í•}ÕÉ°(€€€€€€€€€€€µ½‘•°€ôÍ•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}µ½‘•°(€€€€€€€€€€€¥Í}±½…°€ô‰…Í•}ÕÉ°¹ÍÑ…ÉÑÍİ¥Ñ   ‰¡ÑÑÀè¼¼ÄÈÜ¸À¸À¸Äˆ°€‰¡ÑÑÀè¼½±½…±¡½ÍĞˆ¤¤(€€€€€€€€€€€­•ä€ô€ˆˆ¥˜¥Í}±½…°•±Í”€¡É•‘•¹Ñ¥…±MÑ½É” ¤¹•Ğ¡ÁÉ½Ù¥‘•É}¥¤½È€ˆˆ¤(€€€€€€€€€€€¥˜¹½Ğ¥Í}±½…°…¹¹½Ğ­•äè(€€€€€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}ÁÉ½Ù¥‘•È€ôÁÉ½Ù¥‘•É}¥(€€€€€€€Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}µ½‘•°€ôµ½‘•°(€€€€€€€Í•±˜¹ÁÉ½©•Ğ¹Í…Ù•}Í•ÑÑ¥¹Ì¡Í•ÑÑ¥¹Ì¤(€€€€€€€Í•±˜¹}Í…Ù•}Í¥‘•}½¹Ñ•áĞ ¤(€€€€€€€Ñ¡É•…€ôQÉ…¹Í±…Ñ¥½¹Q¡É•… (€€€€€€€€€€€ÁÉ½©•Ñ}Á…Ñ õÍ•±˜¹ÁÉ½©•Ğ¹Á…Ñ °(€€€€€€€€€€€½¹™¥œõAÉ½Ù¥‘•É½¹™¥œ (€€€€€€€€€€€€€€€¥õÁÉ½Ù¥‘•É}¥°(€€€€€€€€€€€€€€€‰…Í•}ÕÉ°õ‰…Í•}ÕÉ°°(€€€€€€€€€€€€€€€…Á¥}­•äõ­•ä°(€€€€€€€€€€€€€€€ÍÑÉÕÑÕÉ•‘}½ÕÑÁÕĞõÍ•±˜¹±½‰…±}Í•ÑÑ¥¹Ì¹ÑÉ…¹Í±…Ñ¥½¹}ÍÑÉÕÑÕÉ•‘}½ÕÑÁÕĞ°(€€€€€€€€€€€€€€€½™™±¥¹”õÍ•ÑÑ¥¹Ì¹½™™±¥¹”°(€€€€€€€€€€€€¤°(€€€€€€€€€€€ÁÉ½µÁĞõÍ•±˜¹ÁÉ½µÁÑ}•‘¥Ğ¹Ñ½A±…¥¹Q•áĞ ¤°(€€€€€€€€€€€±½ÍÍ…Éäõ}Á…ÉÍ•}±½ÍÍ…Éä¡Í•±˜¹±½ÍÍ…Éå}•‘¥Ğ¹Ñ½A±…¥¹Q•áĞ ¤¤°(€€€€€€€€€€€Á…É•¹ĞõÍ•±˜°(€€€€€€€€¤(€€€€€€€¥˜Í•±˜¹ÕÉÉ•¹Ñ}µ•‘¥…}Á…Ñ è(€€€€€€€€€€€Í•±˜¹}Í•Ñ}µ•‘¥…}ÍÑ…ÑÕÌ (€€€€€€€€€€€€€€€Í•±˜¹ÕÉÉ•¹Ñ}µ•‘¥…}Á…Ñ °(€€€€€€€€€€€€€€€˜‹şï¢¾G’â´ƒ
ÜíÁÉ½Ù¥‘•É}¥‘ô€¼íµ½‘•±ôˆ°(€€€€€€€€€€€€€€€ÁÉ½É•ÍÌôÀ°(€€€€€€€€€€€€¤(€€€€€€€Ñ¡É•…¹ÁÉ½É•ÍÌ¹½¹¹•Ğ¡Í•±˜¹}ÑÉ…¹Í±…Ñ¥½¹}ÁÉ½É•ÍÌ¤(€€€€€€€Ñ¡É•…¹ÁÉ½É•ÍÌ¹½¹¹•Ğ (€€€€€€€€€€€±…µ‰‘„‘½¹”°Ñ½Ñ…°èÍ•±˜¹ÍÑ…ÑÕÍ	…È ¤¹Í¡½İ5•ÍÍ…”¡˜‹şï¢¾Gš&çš²„í‘½¹•ô½íÑ½Ñ…±ôˆ¤(€€€€€€€€¤(€€€€€€€Ñ¡É•…¹ÍÕ••‘•¹½¹¹•Ğ¡Í•±˜¹}ÑÉ…¹Í±…Ñ¥½¹}ÍÕ••‘•¤(€€€€€€€Ñ¡É•…¹™…¥±•¹½¹¹•Ğ¡Í•±˜¹}ÑÉ…¹Í±…Ñ¥½¹}™…¥±•¤(€€€€€€€Ñ¡É•…¹™¥¹¥Í¡•¹½¹¹•Ğ¡Ñ¡É•…¹‘•±•Ñ•1…Ñ•È¤(€€€€€€€Ñ¡É•…¹™¥¹¥Í¡•¹½¹¹•Ğ¡±…µ‰‘„èÍ•Ñ…ÑÑÈ¡Í•±˜°€‰ÑÉ…¹Í±…Ñ¥½¹}Ñ¡É•…ˆ°9½¹”¤¤(€€€€€€€Í•±˜¹ÑÉ…¹Í±…Ñ¥½¹}Ñ¡É•…€ôÑ¡É•…(€€€€€€€Ñ¡É•…¹ÍÑ…ÉĞ ¤((€€€‘•˜}ÑÉ…¹Í±…Ñ¥½¹}ÁÉ½É•ÍÌ¡Í•±˜°½µÁ±•Ñ•è¥¹Ğ°Ñ½Ñ…°è¥¹Ğ¤€´ø9½¹”è(€€€€€€€¥˜¹½ĞÍ•±˜¹ÕÉÉ•¹Ñ}µ•‘¥…}Á…Ñ è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€Á•É•¹Ğ€ôÉ½Õ¹¡½µÁ±•Ñ•€¼µ…à¡Ñ½Ñ…°°€Ä¤€¨€ÄÀÀ¤(€€€€€€€Í•±˜¹}Í•Ñ}µ•‘¥…}ÍÑ…ÑÕÌ (€€€€€€€€€€€Í•±˜¹ÕÉÉ•¹Ñ}µ•‘¥…}Á…Ñ °(€€€€€€€€€€€˜‹š¶–r£şï¢¾G–¶_–æTí½µÁ±•Ñ•‘ô½íÑ½Ñ…±ôˆ°(€€€€€€€€€€€ÁÉ½É•ÍÌõÁ•É•¹Ğ°(€€€€€€€€¤((€€€‘•˜}ÑÉ…¹Í±…Ñ¥½¹}ÍÕ••‘•¡Í•±˜°½µÁ±•Ñ•è¥¹Ğ°…¡•è¥¹Ğ°ÍÑ½ÁÁ•è‰½½°¤€´ø9½¹”è(€€€€€€€¥˜Í•±˜¹ÁÉ½©•Ğè(€€€€€€€€€€€Í•±˜¹Ñ…‰±•}µ½‘•°¹É•™É•Í  ¤(€€€€€€€€€€€Í•±˜¹}™¥±Ñ•É}ÁÉ½‰±•µ}É½İÌ¡Í•±˜¹ÁÉ½‰±•µÍ}…Ñ¥½¸¹¥Í¡•­• ¤¤(€€€€€€€¥˜ÍÑ½ÁÁ•è(€€€€€€€€€€€Í•±˜¹ÍÑ…ÑÕÍ	…È ¤¹Í¡½İ5•ÍÍ…” (€€€€€€€€€€€€€€€˜‹şï¢¾G–ŞËšj–s¾òk–º3š"@í½µÁ±•Ñ•‘ôƒšv‡¾ò3òO–¶c–F÷’â´í…¡•‘ôƒšv„ˆ°€ÜÀÀÀ(€€€€€€€€€€€€¤(€€€€€€€€€€€Í•±˜¹}™¥¹¥Í¡}ÕÉÉ•¹Ñ}µ•‘¥„¡˜‹şï¢¾G–ŞËšj–pƒ
Üí½µÁ±•Ñ•‘ôƒšv„ˆ¤(€€€€€€€•±Í”è(€€€€€€€€€€€Í•±˜¹ÍÑ…ÑÕÍ	…È ¤¹Í¡½İ5•ÍÍ…”¡˜‹şï¢¾G–º3š"@í½µÁ±•Ñ•‘ôƒšv‡¾ò3òO–¶c–F÷’â´í…¡•‘ôƒšv„ˆ°€ÜÀÀÀ¤(€€€€€€€€€€€Í•±˜¹}™¥¹¥Í¡}ÕÉÉ•¹Ñ}µ•‘¥„¡˜‹–ŞË–º3š"@ƒ
Üƒşï¢¾Dí½µÁ±•Ñ•‘ôƒšv„ˆ¤((€€€‘•˜}ÑÉ…¹Í±…Ñ¥½¹}™…¥±•¡Í•±˜°µ•ÍÍ…”èÍÑÈ¤€´ø9½¹”è(€€€€€€€E5•ÍÍ…•	½à¹É¥Ñ¥…°¡Í•±˜°€‹şï¢¾G–’Ç¢Ò”ˆ°µ•ÍÍ…”¤(€€€€€€€Í•±˜¹}™¥¹¥Í¡}ÕÉÉ•¹Ñ}µ•‘¥„ ‹şï¢¾G–’Ç¢Ò”ˆ¤(€€€€€€€¥˜Í•±˜¹ÁÉ½©•Ğè(€€€€€€€€€€€Í•±˜¹Ñ…‰±•}µ½‘•°¹É•™É•Í  ¤((€€€‘•˜Í•…É¡}É•Á±…”¡Í•±˜¤€´ø9½¹”è(€€€€€€€¥˜¹½ĞÍ•±˜¹}É•ÅÕ¥É•}ÁÉ½©•Ğ ¤è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€Í½ÕÉ”°½¬€ôE%¹ÁÕÑ¥…±½œ¹•ÑQ•áĞ¡Í•±˜°€‹šBsÒ‹šnÿš6ˆˆ°€‹š~—š&û–:šZ¾òhˆ¤(€€€€€€€¥˜¹½Ğ½¬½È¹½ĞÍ½ÕÉ”è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€É•Á±…•µ•¹Ğ°½¬€ôE%¹ÁÕÑ¥…±½œ¹•ÑQ•áĞ¡Í•±˜°€‹šBsÒ‹šnÿš6ˆˆ°€‹šnÿš6‹’âë¾òhˆ¤(€€€€€€€¥˜¹½Ğ½¬è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€¡…¹•€ô€À(€€€€€€€™½ÈÍ•µ•¹Ğ¥¸Í•±˜¹ÁÉ½©•Ğ¹±¥ÍÑ}Í•µ•¹ÑÌ ¤è(€€€€€€€€€€€¥˜Í½ÕÉ”¥¸Í•µ•¹Ğ¹Í½ÕÉ•}Ñ•áĞè(€€€€€€€€€€€€€€€Í•±˜¹ÁÉ½©•Ğ¹ÕÁ‘…Ñ•}Í½ÕÉ•}Ñ•áĞ (€€€€€€€€€€€€€€€€€€€Í•µ•¹Ğ¹¥°Í•µ•¹Ğ¹Í½ÕÉ•}Ñ•áĞ¹É•Á±…”¡Í½ÕÉ”°É•Á±…•µ•¹Ğ¤°±½¬õQÉÕ”(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€¡…¹•€¬ô€Ä(€€€€€€€Í•±˜¹Ñ…‰±•}µ½‘•°¹É•™É•Í  ¤(€€€€€€€Í•±˜¹}™¥±Ñ•É}ÁÉ½‰±•µ}É½İÌ¡Í•±˜¹ÁÉ½‰±•µÍ}…Ñ¥½¸¹¥Í¡•­• ¤¤(€€€€€€€Í•±˜¹ÍÑ…ÑÕÍ	…È ¤¹Í¡½İ5•ÍÍ…”¡˜‹–ŞËšnÿš6ˆí¡…¹•‘ôƒšv‡–¶_–æTˆ°€ÔÀÀÀ¤((€€€‘•˜}™¥±Ñ•É}ÁÉ½‰±•µ}É½İÌ¡Í•±˜°•¹…‰±•è‰½½°¤€´ø9½¹”è(€€€€€€€™½ÈÉ½Ü°Í•µ•¹Ğ¥¸•¹Õµ•É…Ñ”¡Í•±˜¹Ñ…‰±•}µ½‘•°¹Í•µ•¹ÑÌ¤è(€€€€€€€€€€€Í•±˜¹Ñ…‰±”¹Í•ÑI½İ!¥‘‘•¸¡É½Ü°•¹…‰±•…¹¹½Ğ‰½½°¡Í•µ•¹Ğ¹ÅÕ…±¥Ñå}™±…Ì¤¤((€€€‘•˜•áÁ½ÉÑ}‘¥…±½œ¡Í•±˜¤€´ø9½¹”è(€€€€€€€¥˜¹½ĞÍ•±˜¹}É•ÅÕ¥É•}ÁÉ½©•Ğ ¤è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€™½Éµ…ÑÌ€ô€‰MIP€ ¨¹ÍÉĞ¤ìí]•‰YQP€ ¨¹ÙÑĞ¤ìíML€ ¨¹…ÍÌ¤ìïšZšr°€ ¨¹ÑáĞ¤ìí)M=8€ ¨¹©Í½¸¤ˆ(€€€€€€€Á…Ñ °Í•±•Ñ•€ôE¥±•¥…±½œ¹•ÑM…Ù•¥±•9…µ”¡Í•±˜°€‹–¾ó–ë–¶_–æTˆ°€ˆˆ°™½Éµ…ÑÌ¤(€€€€€€€¥˜¹½ĞÁ…Ñ è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€™½Éµ…Ñ}µ…À€ôì(€€€€€€€€€€€€‰MIPˆèáÁ½ÉÑ½Éµ…Ğ¹MIP°(€€€€€€€€€€€€‰]•‰YQPˆèáÁ½ÉÑ½Éµ…Ğ¹YQP°(€€€€€€€€€€€€‰MLˆèáÁ½ÉÑ½Éµ…Ğ¹ML°(€€€€€€€€€€€€‹šZšr°ˆèáÁ½ÉÑ½Éµ…Ğ¹QaP°(€€€€€€€€€€€€‰)M=8ˆèáÁ½ÉÑ½Éµ…Ğ¹)M=8°(€€€€€€€ô(€€€€€€€½ÕÑÁÕÑ}™½Éµ…Ğ€ô¹•áĞ (€€€€€€€€€€€€¡Ù…±Õ”™½È­•ä°Ù…±Õ”¥¸™½Éµ…Ñ}µ…À¹¥Ñ•µÌ ¤¥˜Í•±•Ñ•¹ÍÑ…ÉÑÍİ¥Ñ ¡­•ä¤¤°9½¹”(€€€€€€€€¤(€€€€€€€½ÕÑÁÕÑ}™½Éµ…Ğ€ô½ÕÑÁÕÑ}™½Éµ…Ğ½ÈáÁ½ÉÑ½Éµ…Ğ¡A…Ñ ¡Á…Ñ ¤¹ÍÕ™™¥à¹±ÍÑÉ¥À ˆ¸ˆ¤¹±½İ•È ¤¤(€€€€€€€¡½¥•Ì€ôl‹–:šZ‰t(€€€€€€€…±±½İ•°É•…Í½¸€ô…¹}•áÁ½ÉĞ¡Í•±˜¹ÁÉ½©•Ğ¹±¥ÍÑ}Í•µ•¹ÑÌ ¤°áÁ½ÉÑ½¹Ñ•¹Ğ¹QI9M1Q%=8¤(€€€€€€€¥˜…±±½İ•è(€€€€€€€€€€€¡½¥•Ì¹•áÑ•¹¡l‹¢¾GšZˆ°€‹–>3¢¾´‰t¤(€€€€€€€½¹Ñ•¹Ñ}¹…µ”°½¬€ôE%¹ÁÕÑ¥…±½œ¹•Ñ%Ñ•´¡Í•±˜°€‹–¾ó–ë––ºäˆ°€‹––ºç¾òhˆ°¡½¥•Ì°•‘¥Ñ…‰±”õ…±Í”¤(€€€€€€€¥˜¹½Ğ½¬è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€½¹Ñ•¹Ñ}µ…À€ôì(€€€€€€€€€€€€‹–:šZˆèáÁ½ÉÑ½¹Ñ•¹Ğ¹M=UI°(€€€€€€€€€€€€‹¢¾GšZˆèáÁ½ÉÑ½¹Ñ•¹Ğ¹QI9M1Q%=8°(€€€€€€€€€€€€‹–>3¢¾´ˆèáÁ½ÉÑ½¹Ñ•¹Ğ¹	%1%9U0°(€€€€€€€ô(€€€€€€€ÑÉäè(€€€€€€€€€€€•áÁ½ÉÑ}ÍÕ‰Ñ¥Ñ±•Ì (€€€€€€€€€€€€€€€Í•±˜¹ÁÉ½©•Ğ¹±¥ÍÑ}Í•µ•¹ÑÌ ¤°(€€€€€€€€€€€€€€€Á…Ñ °(€€€€€€€€€€€€€€€½ÕÑÁÕÑ}™½Éµ…Ğõ½ÕÑÁÕÑ}™½Éµ…Ğ°(€€€€€€€€€€€€€€€½¹Ñ•¹Ğõ½¹Ñ•¹Ñ}µ…Ám½¹Ñ•¹Ñ}¹…µ•t°(€€€€€€€€€€€€¤(€€€€€€€€€€€Í•±˜¹ÍÑ…ÑÕÍ	…È ¤¹Í¡½İ5•ÍÍ…”¡˜‹–ŞË–¾ó–ë¾òiíÁ…Ñ¡ôˆ°€ÔÀÀÀ¤(€€€€€€€•á•ÁĞQÉ…¹Í±…Ñ¥½¹U¹…Ù…¥±…‰±•ÉÉ½Èè(€€€€€€€€€€€E5•ÍÍ…•	½à¹¥¹™½Éµ…Ñ¥½¸¡Í•±˜°€‹š^ƒšÎW–¾ó–ë¢¾GšZˆ°É•…Í½¸¤(€€€€€€€•á•ÁĞá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€€€€€E5•ÍÍ…•	½à¹É¥Ñ¥…°¡Í•±˜°€‹–¾ó–ë–’Ç¢Ò”ˆ°ÍÑÈ¡•áŒ¤¤((€€€‘•˜}Í••­}Í•±•Ñ•¡Í•±˜°¥¹‘•àèE5½‘•±%¹‘•à¤€´ø9½¹”è(€€€€€€€¥˜¥¹‘•à¹¥ÍY…±¥ ¤…¹¥¹‘•à¹É½Ü ¤€ğ±•¸¡Í•±˜¹Ñ…‰±•}µ½‘•°¹Í•µ•¹ÑÌ¤è(€€€€€€€€€€€Í•±˜¹Á±…å•È¹Í••­}µÌ¡Í•±˜¹Ñ…‰±•}µ½‘•°¹Í•µ•¹ÑÍm¥¹‘•à¹É½Ü ¥t¹ÍÑ…ÉÑ}µÌ¤((€€€‘•˜}É•ÅÕ¥É•}ÁÉ½©•Ğ¡Í•±˜¤€´ø‰½½°è(€€€€€€€¥˜Í•±˜¹ÁÉ½©•Ğè(€€€€€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€€€€€E5•ÍÍ…•	½à¹¥¹™½Éµ…Ñ¥½¸¡Í•±˜°€‹šÊ‡šr'¦†çn¸ˆ°€‹¢¾ß–#šZÃ–îëš"[š&O–ò €¹ÙÍÑÁÉ½¨ƒ¦†çn»ˆ¤(€€€€€€€É•ÑÕÉ¸…±Í”((€€€‘•˜Í¡½İ}…‰½ÕĞ¡Í•±˜¤€´ø9½¹”è(€€€€€€€E5•ÍÍ…•	½à¹…‰½ÕĞ (€€€€€€€€€€€Í•±˜°(€€€€€€€€€€€˜‹–Ï’ê9íAA}95ôˆ°(€€€€€€€€€€€˜‰íAA}95ôí}}Ù•ÉÍ¥½¹}}õq¹q»’ös¢¾òiíUQ!=Iõq¹®g¾òií	%1%	%1%}UI1õq¸ˆ(€€€€€€€€€€€˜‰¥Ñ!Õ‹¾òií%Q!U	}UI1õq»–ºcšZç&#šr³¾òi¥Ñ!ÕˆI•±•…Í•Íq»¢ºã–>¿¢¾¾òi5%Qq¹q¸ˆ(€€€€€€€€€€€€‹²³’â'šZçî’îÛ¢ºã–>¿¢¾›¢Q!%I}AIQe}9=Q%L¹µ“ˆ°(€€€€€€€€¤((€€€‘•˜‘É…¹Ñ•ÉÙ•¹Ğ¡Í•±˜°•Ù•¹Ğ¤€´ø9½¹”è€€Œ¹½Å„è8àÀÈ(€€€€€€€¥˜•Ù•¹Ğ¹µ¥µ•…Ñ„ ¤¹¡…ÍUÉ±Ì ¤è(€€€€€€€€€€€•Ù•¹Ğ¹…•ÁÑAÉ½Á½Í•‘Ñ¥½¸ ¤((€€€‘•˜‘É½ÁÙ•¹Ğ¡Í•±˜°•Ù•¹Ğ¤€´ø9½¹”è€€Œ¹½Å„è8àÀÈ(€€€€€€€Á…Ñ¡Ì€ômA…Ñ ¡¥Ñ•´¹Ñ½1½…±¥±” ¤¤™½È¥Ñ•´¥¸•Ù•¹Ğ¹µ¥µ•…Ñ„ ¤¹ÕÉ±Ì ¤¥˜¥Ñ•´¹¥Í1½…±¥±” ¥t(€€€€€€€¥˜¹½ĞÁ…Ñ¡Ìè(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€ÁÉ½©•ÑÌ€ômÁ…Ñ ™½ÈÁ…Ñ ¥¸Á…Ñ¡Ì¥˜Á…Ñ ¹ÍÕ™™¥à¹±½İ•È ¤€ôô€ˆ¹ÙÍÑÁÉ½¨‰t(€€€€€€€¥˜ÁÉ½©•ÑÌè(€€€€€€€€€€€Í•±˜¹}½Á•¹}ÁÉ½©•Ñ}Á…Ñ ¡ÁÉ½©•ÑÍlÁt¤(€€€€€€€€€€€•Ù•¹Ğ¹…•ÁÑAÉ½Á½Í•‘Ñ¥½¸ ¤(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€™½ÈÁ…Ñ ¥¸Á…Ñ¡Ìè(€€€€€€€€€€€É•Í½±Ù•€ôÁ…Ñ ¹É•Í½±Ù” ¤(€€€€€€€€€€€¥˜É•Í½±Ù•¹¥Í}‘¥È ¤è(€€€€€€€€€€€€€€€µ•‘¥…}™¥±•Ì€ôÍ½ÉÑ• (€€€€€€€€€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€€€€€€€€¥Ñ•´¹É•Í½±Ù” ¤(€€€€€€€€€€€€€€€€€€€€€€€™½È¥Ñ•´¥¸É•Í½±Ù•¹É±½ˆ ˆ¨ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜¥Ñ•´¹¥Í}™¥±” ¤…¹¥Ñ•´¹ÍÕ™™¥à¹±½İ•È ¤¥¸5%}MU%aL(€€€€€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€€€€€­•äõ±…µ‰‘„¥Ñ•´èÍÑÈ¡¥Ñ•´¤¹…Í•™½± ¤°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€Í•±˜¹}•¹ÅÕ•Õ•}µ•‘¥…}™¥±•Ì¡µ•‘¥…}™¥±•Ì°É•Í½±Ù•¤(€€€€€€€€€€€•±¥˜É•Í½±Ù•¹ÍÕ™™¥à¹±½İ•È ¤¥¸5%}MU%aLè(€€€€€€€€€€€€€€€Í•±˜¹}•¹ÅÕ•Õ•}µ•‘¥…}™¥±•Ì¡mÉ•Í½±Ù•‘t°É•Í½±Ù•¹Á…É•¹Ğ¤(€€€€€€€•Ù•¹Ğ¹…•ÁÑAÉ½Á½Í•‘Ñ¥½¸ ¤((€€€‘•˜±½Í•Ù•¹Ğ¡Í•±˜°•Ù•¹Ğ¤€´ø9½¹”è€€Œ¹½Å„è8àÀÈ(€€€€€€€¥˜Í•±˜¹ÑÉ…¹ÍÉ¥ÁÑ¥½¹}Ñ¡É•……¹Í•±˜¹ÑÉ…¹ÍÉ¥ÁÑ¥½¹}Ñ¡É•…¹¥ÍIÕ¹¹¥¹œ ¤è(€€€€€€€€€€€E5•ÍÍ…•	½à¹¥¹™½Éµ…Ñ¥½¸ (€€€€€€€€€€€€€€€Í•±˜°(€€€€€€€€€€€€€€€€‹¢¾–"¯’îï–*‡–Âkšr«îOšv|ˆ°(€€€€€€€€€€€€€€€€‹–öO–&7¢¾–"¯š&çš²‡–º3š"C–æÛ’şw–¶c–&7’â7¢÷–Ï¦^·¢/–ê?š¢‡–z/¢şo¢/–ò–âã¦–ë–B;¾ò3¦†çn»–>¿š‹–’7ˆ°(€€€€€€€€€€€€¤(€€€€€€€€€€€•Ù•¹Ğ¹¥¹½É” ¤(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€¥˜Í•±˜¹ÑÉ…¹Í±…Ñ¥½¹}Ñ¡É•……¹Í•±˜¹ÑÉ…¹Í±…Ñ¥½¹}Ñ¡É•…¹¥ÍIÕ¹¹¥¹œ ¤è(€€€€€€€€€€€¥˜Í•±˜¹ÁÉ½©•Ğ…¹Í•±˜¹ÁÉ½©•Ğ¹•Ñ}Í•ÑÑ¥¹Ì ¤¹ÑÉ…¹Í±…Ñ¥½¹}•¹…‰±•è(€€€€€€€€€€€€€€€A¥Á•±¥¹•½½É‘¥¹…Ñ½È¡Í•±˜¹ÁÉ½©•Ğ¤¹Í•Ñ}ÑÉ…¹Í±…Ñ¥½¹}•¹…‰±•¡…±Í”¤(€€€€€€€€€€€€€€€Í•±˜¹ÑÉ…¹Í±…Ñ¥½¹}Ñ½±”¹‰±½­M¥¹…±Ì¡QÉÕ”¤(€€€€€€€€€€€€€€€Í•±˜¹ÑÉ…¹Í±…Ñ¥½¹}Ñ½±”¹Í•Ñ¡•­•¡…±Í”¤(€€€€€€€€€€€€€€€Í•±˜¹ÑÉ…¹Í±…Ñ¥½¹}Ñ½±”¹‰±½­M¥¹…±Ì¡…±Í”¤(€€€€€€€€€€€E5•ÍÍ…•	½à¹¥¹™½Éµ…Ñ¥½¸ (€€€€€€€€€€€€€€€Í•±˜°(€€€€€€€€€€€€€€€€‹şï¢¾Gš&çš²‡–Âkšr«îOšv|ˆ°(€€€€€€€€€€€€€€€€‹–ŞË–sš¶‹¢Â–ê›šZÃš&çš²‡¢¾ß¶'–ú–öO–&7¢¾ßšÆ’şw–¶c–º3š"C–B;–7–Ï¦^·¢/–ê?ˆ°(€€€€€€€€€€€€¤(€€€€€€€€€€€•Ù•¹Ğ¹¥¹½É” ¤(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€¥˜Í•±˜¹ÁÉ½©•Ğè(€€€€€€€€€€€Í•±˜¹}Í…Ù•}Í¥‘•}½¹Ñ•áĞ ¤(€€€€€€€€€€€Í•±˜¹ÁÉ½©•Ğ¹±½Í” ¤(€€€€€€€€€€€Í•±˜¹ÁÉ½©•Ğ€ô9½¹”(€€€€€€€ÍÕÁ•È ¤¹±½Í•Ù•¹Ğ¡•Ù•¹Ğ¤((€€€‘•˜}Í…Ù•}Í¥‘•}½¹Ñ•áĞ¡Í•±˜¤€´ø9½¹”è(€€€€€€€¥˜¹½ĞÍ•±˜¹ÁÉ½©•Ğè(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€Í•±˜¹ÁÉ½©•Ğ¹Í…Ù•}…Ñ¥Ù•}ÁÉ½µÁĞ¡Í•±˜¹ÁÉ½µÁÑ}•‘¥Ğ¹Ñ½A±…¥¹Q•áĞ ¤¤(€€€€€€€Í•±˜¹ÁÉ½©•Ğ¹Í…Ù•}±½ÍÍ…Éä¡}Á…ÉÍ•}±½ÍÍ…Éä¡Í•±˜¹±½ÍÍ…Éå}•‘¥Ğ¹Ñ½A±…¥¹Q•áĞ ¤¤¤(()‘•˜}™½Éµ…Ñ}µ¥±±¥Í•½¹‘Ì¡Ù…±Õ”è¥¹Ğ¤€´øÍÑÈè(€€€¡½ÕÉÌ°É•µ…¥¹‘•È€ô‘¥Ùµ½¡Ù…±Õ”°€Í|ØÀÁ|ÀÀÀ¤(€€€µ¥¹ÕÑ•Ì°É•µ…¥¹‘•È€ô‘¥Ùµ½¡É•µ…¥¹‘•È°€ØÁ|ÀÀÀ¤(€€€Í•½¹‘Ì°µ¥±±¥Í•½¹‘Ì€ô‘¥Ùµ½¡É•µ…¥¹‘•È°€Å|ÀÀÀ¤(€€€É•ÑÕÉ¸˜‰í¡½ÕÉÌèÀÉ‘ôéíµ¥¹ÕÑ•ÌèÀÉ‘ôéíÍ•½¹‘ÌèÀÉ‘ô¹íµ¥±±¥Í•½¹‘ÌèÀÍ‘ôˆ(()‘•˜}Á…ÉÍ•}Ñ¥µ•ÍÑ…µÀ¡Ù…±Õ”èÍÑÈ¤€´ø¥¹Ğè(€€€Á…ÉÑÌ€ôÙ…±Õ”¹É•Á±…” ˆ°ˆ°€ˆ¸ˆ¤¹ÍÁ±¥Ğ ˆèˆ¤(€€€¥˜±•¸¡Á…ÉÑÌ¤€„ô€Ìè(€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È¡Ù…±Õ”¤(€€€¡½ÕÉÌ°µ¥¹ÕÑ•Ì€ô¥¹Ğ¡Á…ÉÑÍlÁt¤°¥¹Ğ¡Á…ÉÑÍlÅt¤(€€€Í•½¹‘Ì€ô™±½…Ğ¡Á…ÉÑÍlÉt¤(€€€É•ÑÕÉ¸É½Õ¹ ¡¡½ÕÉÌ€¨€ÌØÀÀ€¬µ¥¹ÕÑ•Ì€¨€ØÀ€¬Í•½¹‘Ì¤€¨€ÄÀÀÀ¤(()‘•˜}Á…ÉÍ•}±½ÍÍ…Éä¡Ù…±Õ”èÍÑÈ¤€´ø±¥ÍÑmÑÕÁ±•mÍÑÈ°ÍÑÉutè(€€€É•ÍÕ±Ğ€ômt(€€€™½È±¥¹”¥¸Ù…±Õ”¹ÍÁ±¥Ñ±¥¹•Ì ¤è(€€€€€€€¥˜€ˆôˆ¥¸±¥¹”è(€€€€€€€€€€€Í½ÕÉ”°Ñ…É•Ğ€ô±¥¹”¹ÍÁ±¥Ğ ˆôˆ°€Ä¤(€€€€€€€€€€€¥˜Í½ÕÉ”¹ÍÑÉ¥À ¤…¹Ñ…É•Ğ¹ÍÑÉ¥À ¤è(€€€€€€€€€€€€€€€É•ÍÕ±Ğ¹…ÁÁ•¹ ¡Í½ÕÉ”¹ÍÑÉ¥À ¤°Ñ…É•Ğ¹ÍÑÉ¥À ¤¤¤(€€€É•ÑÕÉ¸É•ÍÕ±Ğ(
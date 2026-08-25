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
            "å°† MP3ã€WAVã€MP4 ç­‰éŸ³è§†é¢‘æˆ–æ•´ä¸ªæ–‡ä»¶å¤¹æ‹–åˆ°è¿™é‡Œï¼›"
            "åŠ å…¥ä»»åŠ¡åè¯·æ‰‹åŠ¨é€‰æ‹©è½¬æ–‡å­—æˆ–æ‰¹é‡æ“ä½œ"
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
        detail_layout.addWidget(self.detail_path_label)
        detail_layout.addWidget(self.detail_status_label)
        detail_layout.addWidget(self.detail_progress)
        layout.addWidget(self.task_detail)
        layout.addWidget(splitter)
        layout.addWidget(footer)
        self.setCentralWidget(central)

        self.task_dock = QDockWidget("ä»»åŠ¡é˜Ÿåˆ—", self)
        self.task_dock.setObjectName("task_queue_dock")
        self.task_tree = QTreeWidget()
        self.task_tree.setHeaderLabels(["æ–‡ä»¶å¤¹ / åª’ä½“", "å½“å‰æ“ä½œ", "è¿›åº¦"])
        self.task_tree.setColumnWidth(0, 230)
        self.task_tree.setColumnWidth(1, 180)
        self.task_tree.setColumnWidth(2, 120)
        self.task_tree.itemDoubleClicked.connect(self._open_task_media)
        self.task_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_tree.customContextMenuRequested.connect(self._show_task_context_menu)
        self.task_dock.setWidget(self.task_tree)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.task_dock)

        side = QTabWidget()
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText("æ ‡é¢˜ã€èƒŒæ™¯ã€äººç‰©å…³ç³»ã€è¯­æ°”å’Œè‡ªç”±ç¿»è¯‘æŒ‡ä»¤")
        self.glossary_edit = QPlainTextEdit()
        self.glossary_edit.setPlaceholderText("æ¯è¡Œï¼šåŸè¯=è¯‘è¯")
        side.addTab(self.prompt_edit, "æç¤ºè¯")
        side.addTab(self.glossary_edit, "æœ¯è¯­")
        self.side_dock = QDockWidget("ç¿»è¯‘ä¸Šä¸‹æ–‡", self)
        self.side_dock.setObjectName("translation_context_dock")
        self.side_dock.setWidget(side)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.side_dock)

    def _build_actions(self) -> None:
        new_action = QAction("æ–°å»ºé¡¹ç›®", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self.new_project)
        open_action = QAction("æ‰“å¼€é¡¹ç›®", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_project)
        media_action = QAction("æ·»åŠ åª’ä½“", self)
        media_action.triggered.connect(self.add_media)
        folder_action = QAction("å¯¼å…¥æ–‡ä»¶å¤¹", self)
   ×M:ÖÚ$z{-®éÜj×&—F–öå÷F‡&VBæB6VÆbçG&ç67&—F–öå÷F‡&VBæ—5'Vææ–ær‚’¢“ ¢F–ÖW"ç6–ævÆU6†÷BƒÂ6VÆbçG&ç6ÆFU÷VæF–ær ¢FVb÷WFFU÷v÷&¶fÆ÷uöÆ&VÂ‡6VÆb’ÓâæöæS ¢–b6VÆbçG&ç6ÆF–öå÷FövvÆRæ—46†V6¶VB‚“ ¢6VÆbçv÷&¶fÆ÷uöÆ&VÂç6WEFW‡B‚.[Ù>X˜ŞûÉ®ŠønXŠ¾Yî{û¾ŠùK‹®zèKÙ>KŠŞihr"¢VÇ6S ¢6VÆbçv÷&¶fÆ÷uöÆ&VÂç6WEFW‡B‚.[Ù>X˜ŞûÉ®K¸^ŠønXŠ¾[›nZûÎX{®Xéşih~ZÙ~[™R" ¢FVb6†V6µ÷VÆ—G’‡6VÆb’ÓâæöæS ¢–bæ÷B6VÆbå÷&WV—&U÷&ö¦V7B‚“ ¢&WGW&à¢6VvÖVçG2Ò6VÆbç&ö¦V7BæÆ—7E÷6VvÖVçG2‚¢Ç•÷VÆ—G•öfÆw2‡6VvÖVçG2Â6VÆbç&ö¦V7BævWE÷6WGF–æw2‚’ç7V'F—FÆR¢v—F‚6VÆbç&ö¦V7Bæ6öææV7F–öã ¢f÷"6VvÖVçB–â6VvÖVçG3 ¢–×÷'B§6öà ¢6VÆbç&ö¦V7Bæ6öææV7F–öâæW†V7WFR€¢%UDDR6VvÖVçG24UBVÆ—G•öfÆw5ö§6öãÓòt„U$R–CÓò"À¢†§6öâæGV×2‡6÷'FVB‡6VvÖVçBçVÆ—G•öfÆw2’ÂVç7W&Uö66–“ÔfÇ6R’Â6VvÖVçBæ–B’À¢¢6VÆbçF&ÆUöÖöFVÂç&Vg&W6‚‚¢6VÆbåöf–ÇFW%÷&ö&ÆVÕ÷&÷w2‡6VÆbç&ö&ÆV×5ö7F–öâæ—46†V6¶VB‚’¢&ö&ÆVÕö6÷VçBÒ7VÒ†&ööÂ†—FVÒçVÆ—G•öfÆw2’f÷"—FVÒ–â6VvÖVçG2¢6VÆbç7FGW4&"‚’ç6†÷tÖW76vR†b.j8iú^ZèÎh‰ûÉ§·&ö&ÆVÕö6÷VçGÒiÚZÙ~[™^™ÈŠhk:hHò"ÂS ¢FVb6†÷u÷G&ç6ÆF–öå÷6WGF–æw2‡6VÆb’Óâ&ööÃ ¢&÷f–FW"Ò6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öå÷&÷f–FW"÷"&÷Væ’ ¢7&VFVçF–Å÷7F÷&RÒ7&VFVçF–Å7F÷&R‚¢†5÷6fVEö¶W’Ò&ööÂ†7&VFVçF–Å÷7F÷&RævWB‡&÷f–FW"’¢F–ÆörÒG&ç6ÆF–öå6WGF–æw4F–Æör€¢&÷f–FW#×&÷f–FW"À¢&6U÷W&Ã×6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öåö&6U÷W&ÂÀ¢ÖöFVÃ×6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öåöÖöFVÂÀ¢†5÷6fVEö¶W“Ö†5÷6fVEö¶W’À¢&VçC×6VÆbÀ¢¢–bF–ÆöræW†V2‚’ÒF–ÆöräF–Æöt6öFRä66WFVC ¢&WGW&âfÇ6P¢fÇVW2ÒF–ÆörçfÇVW2‚¢–bæ÷BfÇVW2æ&6U÷W&Â÷"æ÷BfÇVW2æÖöFVÃ ¢ÖW76vT&÷‚æ–æf÷&ÖF–öâ‡6VÆbÂ.˜XŞ{ÚîKˆŞZèÎi[B"Â.Šû~Z¾Xi’&6RU$ÂY(ÎjŠYè¾YŞz{8""¢&WGW&âfÇ6P¢—5öÆö6ÂÒfÇVW2æ&6U÷W&Âç7F'G7v—F‚‚‚&‡GG¢òó#rããã"Â&‡GG¢òöÆö6Æ†÷7B"’¢–bfÇVW2æ•ö¶W“ ¢7&VFVçF–Å÷7F÷&Rç6WB‡fÇVW2ç&÷f–FW"ÂfÇVW2æ•ö¶W’¢VÆ–bæ÷B—5öÆö6ÂæBæ÷B7&VFVçF–Å÷7F÷&RævWB‡fÇVW2ç&÷f–FW"“ ¢ÖW76vT&÷‚æ–æf÷&ÖF–öâ‡6VÆbÂ.{Ë®[	’¶W’"Â.‹ùÎzˆ¾{û¾ŠùiÈŞXª™ÈŠh’¶W8""¢&WGW&âfÇ6P¢6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öå÷&÷f–FW"ÒfÇVW2ç&÷f–FW ¢6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öåö&6U÷W&ÂÒfÇVW2æ&6U÷W&À¢6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öåöÖöFVÂÒfÇVW2æÖöFVÀ¢6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öå÷7G'V7GW&VEö÷WGWBÒfÇVW2ç7G'V7GW&VEö÷WGW@¢6VÆbç6WGF–æw5÷7F÷&Rç6fR‡6VÆbævÆö&Å÷6WGF–æw2¢–b6VÆbç&ö¦V7C ¢6WGF–æw2Ò6VÆbç&ö¦V7BævWE÷6WGF–æw2‚¢6WGF–æw2çG&ç6ÆF–öå÷&÷f–FW"ÒfÇVW2ç&÷f–FW ¢6WGF–æw2çG&ç6ÆF–öåöÖöFVÂÒfÇVW2æÖöFVÀ¢6VÆbç&ö¦V7Bç6fU÷6WGF–æw2‡6WGF–æw2¢6VÆbç7FGW4&"‚’ç6†÷tÖW76vR†b.[{.KùŞZÙ{û¾ŠùiÈŞXªûÉ§·fÇVW2ç&÷f–FW'Òò·fÇVW2æÖöFVÇÒ"Âc¢&WGW&âG'VP ¢FVbG&ç6ÆFU÷VæF–ær‡6VÆb’ÓâæöæS ¢–bæ÷B6VÆbå÷&WV—&U÷&ö¦V7B‚“ ¢&WGW&à¢–bæ÷B6VÆbç&ö¦V7BævWE÷6WGF–æw2‚’çG&ç6ÆF–öåöVæ&ÆVC ¢ÖW76vT&÷‚æ–æf÷&ÖF–öâ‡6VÆbÂ.iÊ®Y
şyJ{û¾Šù"Â.Šû~XX[ÈY
şšn˜:(	ÎY
şyJ{û¾Šù(	Ş[ÈX[>8""¢&WGW&à¢–b6VÆbçG&ç6ÆF–öå÷F‡&VBæB6VÆbçG&ç6ÆF–öå÷F‡&VBæ—5'Vææ–ær‚“ ¢ÖW76vT&÷‚æ–æf÷&ÖF–öâ‡6VÆbÂ.{û¾ŠùjÚ>YÊ‹ùŠÂ"Â.Šû~zØ[è^[Ù>X˜Ş{û¾Šùh›jÊZèÎh‰8""¢&WGW&à¢–b€¢æ÷B6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öåö&6U÷W&À¢÷"æ÷B6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öåöÖöFVÀ¢’æBæ÷B6VÆbç6†÷u÷G&ç6ÆF–öå÷6WGF–æw2‚“ ¢6VÆbç7FGW4&"‚’ç6†÷tÖW76vR‚.ŠønXŠ¾[{.ZèÎh‰ûÉ¾[	®iÊ®˜XŞ{Úî{û¾ŠùiÈŞXª"Âƒ¢–b6VÆbæ7W'&VçEöÖVF–÷Fƒ ¢6VÆbåöf–æ—6…ö7W'&VçEöÖVF–‚.zØ[è^˜XŞ{Úî{û¾ŠùiÈŞXª"¢&WGW&à¢6WGF–æw2Ò6VÆbç&ö¦V7BævWE÷6WGF–æw2‚¢&÷f–FW%ö–BÒ6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öå÷&÷f–FW"÷"&÷Væ’Ö6ö×F–&ÆR ¢&6U÷W&ÂÒ6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öåö&6U÷W&À¢ÖöFVÂÒ6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öåöÖöFVÀ¢—5öÆö6ÂÒ&6U÷W&Âç7F'G7v—F‚‚‚&‡GG¢òó#rããã"Â&‡GG¢òöÆö6Æ†÷7B"’¢¶W’Ò""–b—5öÆö6ÂVÇ6R„7&VFVçF–Å7F÷&R‚’ævWB‡&÷f–FW%ö–B’÷"""¢–bæ÷B—5öÆö6ÂæBæ÷B¶W“ ¢–bæ÷B6VÆbç6†÷u÷G&ç6ÆF–öå÷6WGF–æw2‚“ ¢–b6VÆbæ7W'&VçEöÖVF–÷Fƒ ¢6VÆbåöf–æ—6…ö7W'&VçEöÖVF–‚.zØ[è^˜XŞ{Úî{û¾ŠùiÈŞXª"¢&WGW&à¢&÷f–FW%ö–BÒ6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öå÷&÷f–FW ¢&6U÷W&ÂÒ6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öåö&6U÷W&À¢ÖöFVÂÒ6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öåöÖöFVÀ¢—5öÆö6ÂÒ&6U÷W&Âç7F'G7v—F‚‚‚&‡GG¢òó#rããã"Â&‡GG¢òöÆö6Æ†÷7B"’¢¶W’Ò""–b—5öÆö6ÂVÇ6R„7&VFVçF–Å7F÷&R‚’ævWB‡&÷f–FW%ö–B’÷"""¢–bæ÷B—5öÆö6ÂæBæ÷B¶W“ ¢&WGW&à¢6WGF–æw2çG&ç6ÆF–öå÷&÷f–FW"Ò&÷f–FW%ö–@¢6WGF–æw2çG&ç6ÆF–öåöÖöFVÂÒÖöFVÀ¢6VÆbç&ö¦V7Bç6fU÷6WGF–æw2‡6WGF–æw2¢6VÆbå÷6fU÷6–FUö6öçFW‡B‚¢F‡&VBÒG&ç6ÆF–öåF‡&VB€¢&ö¦V7E÷Fƒ×6VÆbç&ö¦V7BçF‚À¢6öæf–sÕ&÷f–FW$6öæf–r€¢–C×&÷f–FW%ö–BÀ¢&6U÷W&ÃÖ&6U÷W&ÂÀ¢•ö¶W“Ö¶W’À¢7G'V7GW&VEö÷WGWC×6VÆbævÆö&Å÷6WGF–æw2çG&ç6ÆF–öå÷7G'V7GW&VEö÷WGWBÀ¢öffÆ–æS×6WGF–æw2æöffÆ–æRÀ¢’À¢&ö×C×6VÆbç&ö×EöVF—BçFõÆ–åFW‡B‚’À¢vÆ÷76'“Õ÷'6UövÆ÷76'’‡6VÆbævÆ÷76'•öVF—BçFõÆ–åFW‡B‚’’À¢&VçC×6VÆbÀ¢¢–b6VÆbæ7W'&VçEöÖVF–÷Fƒ ¢6VÆbå÷6WEöÖVF–÷7FGW2€¢6VÆbæ7W'&VçEöÖVF–÷F‚À¢b.{û¾ŠùKŠÒ+r·&÷f–FW%ö–GÒò¶ÖöFVÇÒ"À¢&öw&W73ÓÀ¢¢F‡&VBç&öw&W72æ6öææV7B‡6VÆbå÷G&ç6ÆF–öå÷&öw&W72¢F‡&VBç&öw&W72æ6öææV7B€¢ÆÖ&FFöæRÂF÷FÃ¢6VÆbç7FGW4&"‚’ç6†÷tÖW76vR†b.{û¾Šùh›jÊ¶FöæWÒ÷·F÷FÇÒ"¢¢F‡&VBç7V66VVFVBæ6öææV7B‡6VÆbå÷G&ç6ÆF–öå÷7V66VVFVB¢F‡&VBæf–ÆVBæ6öææV7B‡6VÆbå÷G&ç6ÆF–öåöf–ÆVB¢F‡&VBæf–æ—6†VBæ6öææV7B‡F‡&VBæFVÆWFTÆFW"¢F‡&VBæf–æ—6†VBæ6öææV7B†ÆÖ&F¢6WFGG"‡6VÆbÂ'G&ç6ÆF–öå÷F‡&VB"ÂæöæR’¢6VÆbçG&ç6ÆF–öå÷F‡&VBÒF‡&V@¢F‡&VBç7F'B‚ ¢FVb÷G&ç6ÆF–öå÷&öw&W72‡6VÆbÂ6ö×ÆWFVC¢–çBÂF÷FÃ¢–çB’ÓâæöæS ¢–bæ÷B6VÆbæ7W'&VçEöÖVF–÷Fƒ ¢&WGW&à¢W&6VçBÒ&÷VæB†6ö×ÆWFVBòÖ‚‡F÷FÂÂ’¢¢6VÆbå÷6WEöÖVF–÷7FGW2€¢6VÆbæ7W'&VçEöÖVF–÷F‚À¢b.jÚ>YÊ{û¾ŠùZÙ~[™R¶6ö×ÆWFVGÒ÷·F÷FÇÒ"À¢&öw&W73×W&6VçBÀ¢ ¢FVb÷G&ç6ÆF–öå÷7V66VVFVB‡6VÆbÂ6ö×ÆWFVC¢–çBÂ66†VC¢–çBÂ7F÷VC¢&ööÂ’ÓâæöæS ¢–b6VÆbç&ö¦V7C ¢6VÆbçF&ÆUöÖöFVÂç&Vg&W6‚‚¢6VÆbåöf–ÇFW%÷&ö&ÆVÕ÷&÷w2‡6VÆbç&ö&ÆV×5ö7F–öâæ—46†V6¶VB‚’¢–b7F÷VC ¢6VÆbç7FGW4&"‚’ç6†÷tÖW76vR€¢b.{û¾Šù[{.i¨.XÎûÉ®ZèÎh‰¶6ö×ÆWFVGÒiÚûÈÎ{É>ZÙYŞKŠÒ¶66†VGÒiÚ"Âs ¢¢6VÆbåöf–æ—6…ö7W'&VçEöÖVF–†b.{û¾Šù[{.i¨.XÂ+r¶6ö×ÆWFVGÒiÚ"¢VÇ6S ¢6VÆbç7FGW4&"‚’ç6†÷tÖW76vR†b.{û¾ŠùZèÎh‰¶6ö×ÆWFVGÒiÚûÈÎ{É>ZÙYŞKŠÒ¶66†VGÒiÚ"Âs¢6VÆbåöf–æ—6…ö7W'&VçEöÖVF–†b.[{.ZèÎh‰+r{û¾Šù¶6ö×ÆWFVGÒiÚ" ¢FVb÷G&ç6ÆF–öåöf–ÆVB‡6VÆbÂÖW76vS¢7G"’ÓâæöæS ¢ÖW76vT&÷‚æ7&—F–6Â‡6VÆbÂ.{û¾ŠùZK‹JR"ÂÖW76vR¢6VÆbåöf–æ—6…ö7W'&VçEöÖVF–‚.{û¾ŠùZK‹JR"¢–b6VÆbç&ö¦V7C ¢6VÆbçF&ÆUöÖöFVÂç&Vg&W6‚‚ ¢FVb6V&6…÷&WÆ6R‡6VÆb’ÓâæöæS ¢–bæ÷B6VÆbå÷&WV—&U÷&ö¦V7B‚“ ¢&WGW&à¢6÷W&6RÂö²Ò–çWDF–ÆörævWEFW‡B‡6VÆbÂ.i	Î{J.i»şhÚ""Â.iú^h›îXéşih~ûÉ¢"¢–bæ÷Bö²÷"æ÷B6÷W&6S ¢&WGW&à¢&WÆ6VÖVçBÂö²Ò–çWDF–ÆörævWEFW‡B‡6VÆbÂ.i	Î{J.i»şhÚ""Â.i»şhÚ.K‹®ûÉ¢"¢–bæ÷Bö³ ¢&WGW&à¢6†ævVBÒ ¢f÷"6VvÖVçB–â6VÆbç&ö¦V7BæÆ—7E÷6VvÖVçG2‚“ ¢–b6÷W&6R–â6VvÖVçBç6÷W&6U÷FW‡C ¢6VÆbç&ö¦V7BçWFFU÷6÷W&6U÷FW‡B€¢6VvÖVçBæ–BÂ6VvÖVçBç6÷W&6U÷FW‡Bç&WÆ6R‡6÷W&6RÂ&WÆ6VÖVçB’ÂÆö6³ÕG'VP¢¢6†ævVB³Ò¢6VÆbçF&ÆUöÖöFVÂç&Vg&W6‚‚¢6VÆbåöf–ÇFW%÷&ö&ÆVÕ÷&÷w2‡6VÆbç&ö&ÆV×5ö7F–öâæ—46†V6¶VB‚’¢6VÆbç7FGW4&"‚’ç6†÷tÖW76vR†b.[{.i»şhÚ"¶6†ævVGÒiÚZÙ~[™R"ÂS ¢FVböf–ÇFW%÷&ö&ÆVÕ÷&÷w2‡6VÆbÂVæ&ÆVC¢&ööÂ’ÓâæöæS ¢f÷"&÷rÂ6VvÖVçB–âVçVÖW&FR‡6VÆbçF&ÆUöÖöFVÂç6VvÖVçG2“ ¢6VÆbçF&ÆRç6WE&÷t†–FFVâ‡&÷rÂVæ&ÆVBæBæ÷B&ööÂ‡6VvÖVçBçVÆ—G•öfÆw2’ ¢FVbW‡÷'EöF–Æör‡6VÆb’ÓâæöæS ¢–bæ÷B6VÆbå÷&WV—&U÷&ö¦V7B‚“ ¢&WGW&à¢f÷&ÖG2Ò%5%B‚¢ç7'B“³µvV%eEB‚¢çgGB“³´52‚¢æ72“³¾ih~iÊÂ‚¢çG‡B“³´¥4ôâ‚¢æ§6öâ’ ¢F‚Â6VÆV7FVBÒf–ÆTF–ÆörævWE6fTf–ÆTæÖR‡6VÆbÂ.ZûÎX{®ZÙ~[™R"Â""Âf÷&ÖG2¢–bæ÷BFƒ ¢&WGW&à¢f÷&ÖEöÖÒ°¢%5%B#¢W‡÷'Df÷&ÖBå5%BÀ¢%vV%eEB#¢W‡÷'Df÷&ÖBåeEBÀ¢$52#¢W‡÷'Df÷&ÖBä52À¢.ih~iÊÂ#¢W‡÷'Df÷&ÖBåE…BÀ¢$¥4ôâ#¢W‡÷'Df÷&ÖBä¥4ôâÀ¢Ğ¢÷WGWEöf÷&ÖBÒæW‡B€¢‡fÇVRf÷"¶W’ÂfÇVR–âf÷&ÖEöÖæ—FV×2‚’–b6VÆV7FVBç7F'G7v—F‚†¶W’’’ÂæöæP¢¢÷WGWEöf÷&ÖBÒ÷WGWEöf÷&ÖB÷"W‡÷'Df÷&ÖB…F‚‡F‚’ç7Vff—‚æÇ7G&—‚"â"’æÆ÷vW"‚’¢6†ö–6W2Ò².Xéşihr%Ğ¢ÆÆ÷vVBÂ&V6öâÒ6åöW‡÷'B‡6VÆbç&ö¦V7BæÆ—7E÷6VvÖVçG2‚’ÂW‡÷'D6öçFVçBåE$å4ÄD”ôâ¢–bÆÆ÷vVC ¢6†ö–6W2æW‡FVæB…².Šùihr"Â.XøÎŠúÒ%Ò¢6öçFVçEöæÖRÂö²Ò–çWDF–ÆörævWD—FVÒ‡6VÆbÂ.ZûÎX{®Xh^Zë’"Â.Xh^ZëûÉ¢"Â6†ö–6W2ÂVF—F&ÆSÔfÇ6R¢–bæ÷Bö³ ¢&WGW&à¢6öçFVçEöÖÒ°¢.Xéşihr#¢W‡÷'D6öçFVçBå4õU$4RÀ¢.Šùihr#¢W‡÷'D6öçFVçBåE$å4ÄD”ôâÀ¢.XøÎŠúÒ#¢W‡÷'D6öçFVçBä$”Ä”äuTÂÀ¢Ğ¢G'“ ¢W‡÷'E÷7V'F—FÆW2€¢6VÆbç&ö¦V7BæÆ—7E÷6VvÖVçG2‚’À¢F‚À¢÷WGWEöf÷&ÖCÖ÷WGWEöf÷&ÖBÀ¢6öçFVçCÖ6öçFVçEöÖ¶6öçFVçEöæÖUÒÀ¢¢6VÆbç7FGW4&"‚’ç6†÷tÖW76vR†b.[{.ZûÎX{®ûÉ§·F‡Ò"ÂS¢W†6WBG&ç6ÆF–öåVæf–Æ&ÆTW'&÷# ¢ÖW76vT&÷‚æ–æf÷&ÖF–öâ‡6VÆbÂ.izk9^ZûÎX{®Šùihr"Â&V6öâ¢W†6WBW†6WF–öâ2W†3 ¢ÖW76vT&÷‚æ7&—F–6Â‡6VÆbÂ.ZûÎX{®ZK‹JR"Â7G"†W†2’ ¢FVb÷6VVµ÷6VÆV7FVB‡6VÆbÂ–æFWƒ¢ÖöFVÄ–æFW‚’ÓâæöæS ¢–b–æFW‚æ—5fÆ–B‚’æB–æFW‚ç&÷r‚’ÂÆVâ‡6VÆbçF&ÆUöÖöFVÂç6VvÖVçG2“ ¢6VÆbçÆ–W"ç6VVµö×2‡6VÆbçF&ÆUöÖöFVÂç6VvÖVçG5¶–æFW‚ç&÷r‚•Òç7F'Eö×2 ¢FVb÷&WV—&U÷&ö¦V7B‡6VÆb’Óâ&ööÃ ¢–b6VÆbç&ö¦V7C ¢&WGW&âG'VP¢ÖW76vT&÷‚æ–æf÷&ÖF–öâ‡6VÆbÂ.k*iÈšyºâ"Â.Šû~XXik[»®h‰nh™>[Èçg7G&ö¢šyºî8""¢&WGW&âfÇ6P ¢FVb6†÷uö&÷WB‡6VÆb’ÓâæöæS ¢ÖW76vT&÷‚æ&÷WB€¢6VÆbÀ¢b.X[>K¨ç´ôäÔWÒ"À¢b'´ôäÔWÒµõ÷fW'6–öåõ÷ÕÆåÆîKÙÎˆ^ûÉ§´UD„õ'ÕÆä.z¹ûÉ§´$”Ä”$”Ä•õU$ÇÕÆâ ¢b$v—D‡V.ûÉ§´t•D…T%õU$ÇÕÆîZéikx˜iÊÎûÉ¤v—D‡V"&VÆV6W5ÆîŠëXúşŠøûÉ¤Ô•EÆåÆâ ¢.zÊÎKˆik{¸NK»nŠëXúşŠúnŠxD„•$Eõ%E•ôäõD”4U2æÖN8""À¢ ¢FVbG&tVçFW$WfVçB‡6VÆbÂWfVçB’ÓâæöæS¢2æ÷¢ãƒ ¢–bWfVçBæÖ–ÖTFF‚’æ†5W&Ç2‚“ ¢WfVçBæ66WE&÷÷6VD7F–öâ‚ ¢FVbG&÷WfVçB‡6VÆbÂWfVçB’ÓâæöæS¢2æ÷¢ãƒ ¢F‡2ÒµF‚†—FVÒçFôÆö6Äf–ÆR‚’’f÷"—FVÒ–âWfVçBæÖ–ÖTFF‚’çW&Ç2‚’–b—FVÒæ—4Æö6Äf–ÆR‚•Ğ¢–bæ÷BF‡3 ¢&WGW&à¢&ö¦V7G2Ò·F‚f÷"F‚–âF‡2–bF‚ç7Vff—‚æÆ÷vW"‚’ÓÒ"çg7G&ö¢%Ğ¢–b&ö¦V7G3 ¢6VÆbåö÷Vå÷&ö¦V7E÷F‚‡&ö¦V7G5³Ò¢WfVçBæ66WE&÷÷6VD7F–öâ‚¢&WGW&à¢f÷"F‚–âF‡3 ¢&W6öÇfVBÒF‚ç&W6öÇfR‚¢–b&W6öÇfVBæ—5öF—"‚“ ¢ÖVF–öf–ÆW2Ò6÷'FVB€¢€¢—FVÒç&W6öÇfR‚¢f÷"—FVÒ–â&W6öÇfVBç&vÆö"‚"¢"¢–b—FVÒæ—5öf–ÆR‚’æB—FVÒç7Vff—‚æÆ÷vW"‚’–âÔTD”õ5Tdd•„U0¢’À¢¶W“ÖÆÖ&F—FVÓ¢7G"†—FVÒ’æ66VföÆB‚’À¢¢6VÆbåöVçVWVUöÖVF–öf–ÆW2†ÖVF–öf–ÆW2Â&W6öÇfVB¢VÆ–b&W6öÇfVBç7Vff—‚æÆ÷vW"‚’–âÔTD”õ5Tdd•„U3 ¢6VÆbåöVçVWVUöÖVF–öf–ÆW2…·&W6öÇfVEÒÂ&W6öÇfVBç&VçB¢WfVçBæ66WE&÷÷6VD7F–öâ‚ ¢FVb6Æ÷6TWfVçB‡6VÆbÂWfVçB’ÓâæöæS¢2æ÷¢ãƒ ¢–b6VÆbçG&ç67&—F–öå÷F‡&VBæB6VÆbçG&ç67&—F–öå÷F‡&VBæ—5'Vææ–ær‚“ ¢ÖW76vT&÷‚æ–æf÷&ÖF–öâ€¢6VÆbÀ¢.ŠønXŠ¾K»¾Xª[	®iÊ®{¹>iÙò"À¢.[Ù>X˜ŞŠønXŠ¾h›jÊZèÎh‰[›nKùŞZÙX˜ŞKˆŞˆ;ŞX[>™zŞzˆ¾[¨ş8.jŠYè¾‹ù¾zˆ¾[È.[‹˜X{®YîûÈÎšyºîXúşh.ZHŞ8""À¢¢WfVçBæ–væ÷&R‚¢&WGW&à¢–b6VÆbçG&ç6ÆF–öå÷F‡&VBæB6VÆbçG&ç6ÆF–öå÷F‡&VBæ—5'Vææ–ær‚“ ¢–b6VÆbç&ö¦V7BæB6VÆbç&ö¦V7BævWE÷6WGF–æw2‚’çG&ç6ÆF–öåöVæ&ÆVC ¢—VÆ–æT6ö÷&F–æF÷"‡6VÆbç&ö¦V7B’ç6WE÷G&ç6ÆF–öåöVæ&ÆVB„fÇ6R¢6VÆbçG&ç6ÆF–öå÷FövvÆRæ&Æö6µ6–væÇ2…G'VR¢6VÆbçG&ç6ÆF–öå÷FövvÆRç6WD6†V6¶VB„fÇ6R¢6VÆbçG&ç6ÆF–öå÷FövvÆRæ&Æö6µ6–væÇ2„fÇ6R¢ÖW76vT&÷‚æ–æf÷&ÖF–öâ€¢6VÆbÀ¢.{û¾Šùh›jÊ[	®iÊ®{¹>iÙò"À¢.[{.XÎjÚ.‹>[ªnikh›jÊ8.Šû~zØ[è^[Ù>X˜ŞŠû~k.KùŞZÙZèÎh‰YîXhŞX[>™zŞzˆ¾[¨ş8""À¢¢WfVçBæ–væ÷&R‚¢&WGW&à¢–b6VÆbç&ö¦V7C ¢6VÆbå÷6fU÷6–FUö6öçFW‡B‚¢6VÆbç&ö¦V7Bæ6Æ÷6R‚¢6VÆbç&ö¦V7BÒæöæP¢7WW"‚’æ6Æ÷6TWfVçB†WfVçB ¢FVb÷6fU÷6–FUö6öçFW‡B‡6VÆb’ÓâæöæS ¢–bæ÷B6VÆbç&ö¦V7C ¢&WGW&à¢6VÆbç&ö¦V7Bç6fUö7F—fU÷&ö×B‡6VÆbç&ö×EöVF—BçFõÆ–åFW‡B‚’¢6VÆbç&ö¦V7Bç6fUövÆ÷76'’…÷'6UövÆ÷76'’‡6VÆbævÆ÷76'•öVF—BçFõÆ–åFW‡B‚’’  ¦FVböf÷&ÖEöÖ–ÆÆ—6V6öæG2‡fÇVS¢–çB’Óâ7G# ¢†÷W'2Â&VÖ–æFW"ÒF—fÖöB‡fÇVRÂ5ócó¢Ö–çWFW2Â&VÖ–æFW"ÒF—fÖöB‡&VÖ–æFW"Âcó¢6V6öæG2ÂÖ–ÆÆ—6V6öæG2ÒF—fÖöB‡&VÖ–æFW"Âó¢&WGW&âb'¶†÷W'3£&GÓ§¶Ö–çWFW3£&GÓ§·6V6öæG3£&GÒç¶Ö–ÆÆ—6V6öæG3£6GÒ   ¦FVb÷'6U÷F–ÖW7F×‡fÇVS¢7G"’Óâ–çC ¢'G2ÒfÇVRç&WÆ6R‚"Â"Â"â"’ç7Æ—B‚#¢"¢–bÆVâ‡'G2’Ò3 ¢&—6RfÇVTW'&÷"‡fÇVR¢†÷W'2ÂÖ–çWFW2Ò–çB‡'G5³Ò’Â–çB‡'G5³Ò¢6V6öæG2ÒfÆöB‡'G5³%Ò¢&WGW&â&÷VæB‚††÷W'2¢3c²Ö–çWFW2¢c²6V6öæG2’¢  ¦FVb÷'6UövÆ÷76'’‡fÇVS¢7G"’ÓâÆ—7E·GWÆU·7G"Â7G%ÕÓ ¢&W7VÇBÒµĞ¢f÷"Æ–æR–âfÇVRç7Æ—FÆ–æW2‚“ ¢–b#Ò"–âÆ–æS ¢6÷W&6RÂF&vWBÒÆ–æRç7Æ—B‚#Ò"Â¢–b6÷W&6Rç7G&—‚’æBF&vWBç7G&—‚“ ¢&W7VÇBæVæB‚‡6÷W&6Rç7G&—‚’ÂF&vWBç7G&—‚’’¢&WGW&â&W7VÇ@
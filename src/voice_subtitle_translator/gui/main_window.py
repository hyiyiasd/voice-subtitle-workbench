from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QTableView,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from voice_subtitle_translator import APP_NAME, AUTHOR, BILIBILI_URL, GITHUB_URL, __version__
from voice_subtitle_translator.credentials import CredentialStore
from voice_subtitle_translator.domain import ProjectSettings, Segment
from voice_subtitle_translator.model_manager import ModelManager
from voice_subtitle_translator.paths import AppPaths, bundled_resource
from voice_subtitle_translator.pipeline import PipelineCoordinator
from voice_subtitle_translator.project import Project
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
from .waveform import WaveformWidget


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
        layout.addWidget(splitter)
        layout.addWidget(footer)
        self.setCentralWidget(central)

        task_dock = QDockWidget("ä»»åŠ¡é˜Ÿåˆ—", self)
        self.task_list = QListWidget()
        task_dock.setWidget(self.task_list)
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
        models_action = QAction("æ¨¡å‹ç®¡ç†", self)
        models_action.triggered.connect(self.show_model_manager)
        transcribe_action = QAction("å¼€å§‹è¯†åˆ«", self)
        transcribe_action.triggered.connect(self.transcribe_media)
        check_action = QAction("æ£€æŸ¥å­—å¹•", self)
        check_action.triggered.connect(self.check_quality)
        self.problems_action = QAction("ä»…çœ‹é—®é¢˜å­—å¹•", self)
        self.problems_action.setCheckable(True)
        self.problems_action.toggled.connect(self._filter_problem_rows)
        replace_action = QAction("æœç´¢æ›¿æ¢", self)
        replace_action.setShortcut(QKeySequence.StandardKey.Find)
        replace_action.triggered.connect(self.search_replace)
        translate_action = QAction("ç¿»è¯‘æœªå®Œæˆå­—å¹•", self)
        translate_action.triggered.connect(self.translate_pending)
        export_action = QAction("å¯¼å‡ºå­—å¹•", self)
        export_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        export_action.triggered.connect(self.export_dialog)
        about_action = QAction("å…³äº", self)
        about_action.triggered.connect(self.show_about)
        for action in (
            new_action,
            open_action,
            media_action,
            models_action,
            transcribe_action,
            check_action,
            self.problems_action,
            replace_action,
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
            self.setWindowTitle(f"{APP_NAME} {__version__} â€” {project.path.name}")
            media = project.resolve_media()
            if media:
                self.player.load(media)
        else:
            self.prompt_edit.clear()
            self.glossary_edit.clear()
            self.setWindowTitle(f"{APP_NAME} {__version__}")

    def new_project(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "æ–°å»ºé¡¹ç›®", "", "å­—å¹•é¡¹ç›® (*.vstproj)")
        if not path:
            return
        settings = Projó{h‘éì¶»§q«^t¹¥è9¬åy¥¬9nîºhnyæëˆ‹İŠ^ÊJB‚ˆYˆÜ[—Ü›Ú™Xİ
Ù[ŠHOˆ›Û™N‚ˆ]ÈHQš[QX[ÙË™Ù]Ü[‘š[S˜[YJÙ[‹¹¢dùo :hnyæëˆ‹ˆ‹¹keùnezhnyæëˆ

‹œİ›ÚŠHŠBˆYˆ]‚ˆÙ[‹—ÛÜ[—Ü›Ú™XİÜ]
]
]
JB‚ˆYˆÛÜ[—Ü›Ú™XİÜ]
Ù[‹]ˆ]
HOˆ›Û™N‚ˆN‚ˆÙ[‹—ÜÙ]Ü›Ú™Xİ
›Ú™Xİ›Ü[Š]
JBˆÙ[‹™ÛØ˜[ÜÙ][™ÜË›\İÜ›Ú™XİHİŠ]
BˆÙ[‹œÙ][™Ü×ÜİÜ™KœØ]™JÙ[‹™ÛØ˜[ÜÙ][™ÜÊBˆ^Ù\^Ù\[Ûˆ\È^Î‚ˆSY\ÜØYÙP›Ş˜Üš]XØ[
Ù[‹¹¥è9¬åy¢dùo :hnyæëˆ‹İŠ^ÊJB‚ˆYˆYÛYYXJÙ[ŠHOˆ›Û™N‚ˆYˆ›İÙ[‹—Ü™\]Z\™WÜ›Ú™Xİ

N‚ˆ™]\›‚ˆ]ÈHQš[QX[ÙË™Ù]Ü[‘š[S˜[YJÙ[‹º`"y¢êzgìú)áºh¤HŠBˆYˆ]‚ˆÙ[‹œ›Ú™XİœÙ]ÛYYXJ]
BˆÙ[‹œ^Y\‹›ØY
]
]
JB‚ˆYˆÚİ×Û[Ù[ÛX[˜YÙ\ŠÙ[ŠHOˆ›Û™N‚ˆÙ™›[™HHÙ[‹œ›Ú™Xİ™Ù]ÜÙ][™ÜÊ
K›Ù™›[™HYˆÙ[‹œ›Ú™Xİ[ÙH˜[ÙBˆX[ÙÈH[Ù[X[˜YÙ\‘X[ÙÊˆ[Ù[X[˜YÙ\ŠÙ[‹œ]Ë[™YÜ™\Ûİ\˜ÙJ›[Ù[ËÛX[šY™\İšœÛÛˆŠJKˆÙ™›[™O[Ù™›[™Kˆ\™[\Ù[‹ˆ
BˆX[ÙË™^XÊ
B‚ˆYˆ˜[œØÜšX™WÛYYXJÙ[ŠHOˆ›Û™N‚ˆYˆ›İÙ[‹—Ü™\]Z\™WÜ›Ú™Xİ

N‚ˆ™]\›‚ˆYˆÙ[‹œ›Ú™Xİœ™\ÛÛ™WÛYYXJ
H\È›Û™N‚ˆSY\ÜØYÙP›Şš[™›Ü›X][ÛŠÙ[‹¹¬¨y§"yj¤¹/dÈ‹º+íùab9­îùb¨9¢%ºaãy¥¬9k¦¹/czgìú)áºh¤y¥¡ù.í¸à ˆŠBˆ™]\›‚ˆYˆÙ[‹˜[œØÜš\[Û—İ™XY[™Ù[‹˜[œØÜš\[Û—İ™XYš\Ô[›š[™Ê
N‚ˆSY\ÜØYÙP›Şš[™›Ü›X][ÛŠÙ[‹º+á¹b*ù«hùg*:/ä:(c‹º+íùëbyo¡yodùbcz+á¹b*ù.îùb¨yk£9¢$8à ˆŠBˆ™]\›‚ˆX[˜YÙ\ˆH[Ù[X[˜YÙ\ŠÙ[‹œ]Ë[™YÜ™\Ûİ\˜ÙJ›[Ù[ËÛX[šY™\İšœÛÛˆŠJBˆ[œİ[YHÂˆ[Ù[™\ØÜš\Ü‹šYˆ›Üˆ[Ù[[ˆX[˜YÙ\‹›[Ù[Ë˜[Y\Ê
BˆYˆ[Ù[™\ØÜš\Ü‹šYOHœÚ[\›Ë]˜Y]ˆˆ[™X[˜YÙ\‹š\×Ú[œİ[Y
[Ù[™\ØÜš\Ü‹šY
BˆBˆYˆ›İX[˜YÙ\‹š\×Ú[œİ[Y
œÚ[\›Ë]˜Y]ˆŠHÜˆ›İ[œİ[Y‚ˆSY\ÜØYÙP›Şš[™›Ü›X][ÛŠˆÙ[‹ˆ¹ª(yg¢ùl&¹§*¹k¢z(áH‹ˆº+á¹b*úg :) HÚ[\›ÈQ9d£:!ìùl$y. 9.*ˆTÔˆ9ª(yg¢ûï#:+íùab9¢dùo 8 '9ª(yg¢ùë¨yä!¸ 'y."ú/oxà ˆ‹ˆ
Bˆ™]\›‚ˆ[Ù[ÚYÚÈHR[œ]X[ÙË™Ù]][JˆÙ[‹º`"y¢êz+á¹b*ùª(yg¢È‹¹ª(yg¢ûï&ˆ‹[œİ[YY]X›OQ˜[ÙBˆ
BˆYˆ›İÚÎ‚ˆ™]\›‚ˆ]šXÙWÛX™[ÚÈHR[œ]X[ÙË™Ù]][JˆÙ[‹ˆº`"y¢êz/ä:(c:+¯¹i!È‹ˆº+¯¹i!ûï"ÔH9b'yiâùc%¹i,z-)y¥í¹.#y/&ºgfznæ9b!ù£h»ï"{ï&ˆ‹ˆÈÔH
[
H‹ÕQH
[Ù›Ø]MŠH—KˆY]X›OQ˜[ÙKˆ
BˆYˆ›İÚÎ‚ˆ™]\›‚ˆ]šXÙHH˜İYHˆYˆ]šXÙWÛX™[œİ\İÚ]
ÕQHŠH[ÙH˜ÜH‚ˆÙ][™ÜÈHÙ[‹œ›Ú™Xİ™Ù]ÜÙ][™ÜÊ
BˆÙ][™ÜË˜\Ü—Û[Ù[H[Ù[ÚYˆÙ[‹œ›Ú™XİœØ]™WÜÙ][™ÜÊÙ][™ÜÊBˆ™XYH˜[œØÜš\[Û•™XY
ˆ›Ú™XİÜ]\Ù[‹œ›Ú™Xİœ]ˆ]Ï\Ù[‹œ]Ëˆ[Ù[ÚY[[Ù[ÚYˆ]šXÙOY]šXÙKˆ\™[\Ù[‹ˆ
BˆÙ[‹\Ú×Û\İ˜Y][Jˆº+á¹b*ûï&Û[Ù[ÚYHÈÙ]šXÙ_HŠBˆÙ[‹œİ]\Ğ˜\Š
KœÚİÓY\ÜØYÙJ¹«hùg*9¢iú(c:gìúh¤y¨!ùaá¹c%¸à UQ9d£:+á¹b*ø )¸ )ˆŠBˆ™XYœİXØÙYYY˜ÛÛ›™Xİ
Ù[‹—İ˜[œØÜš\[Û—ÜİXØÙYYY
Bˆ™XY™˜Z[Y˜ÛÛ›™Xİ
Ù[‹—İ˜[œØÜš\[Û—Ù˜Z[Y
Bˆ™XY™š[š\ÚY˜ÛÛ›™Xİ
™XY™[]S]\ŠBˆ™XY™š[š\ÚY˜ÛÛ›™Xİ
[X™NˆÙ]]ŠÙ[‹˜[œØÜš\[Û—İ™XY‹›Û™JJBˆÙ[‹˜[œØÜš\[Û—İ™XYH™XYˆ™XYœİ\

B‚ˆYˆİ˜[œØÜš\[Û—ÜİXØÙYYY
Ù[‹\Ú×ÚYˆİ‹ÙYÛY[ØÛİ[ˆ[
HOˆ›Û™N‚ˆYˆÙ[‹œ›Ú™Xİ‚ˆÙ[‹X›WÛ[Ù[œ™Yœ™\Ú

BˆÙ[‹—Ùš[\—Ü›Ø›[WÜ›İÜÊÙ[‹œ›Ø›[\×ØXİ[Û‹š\ĞÚXÚÙY

JBˆÙ[‹œİ]\Ğ˜\Š
KœÚİÓY\ÜØYÙJˆˆº+á¹b*ùk£9¢$;ï&¹alHÜÙYÛY[ØÛİ[H9§hykeùne{ï"9.îùb¨Hİ\Ú×ÚYÎ_{ï"H‹ˆ
B‚ˆYˆİ˜[œØÜš\[Û—Ù˜Z[Y
Ù[‹Y\ÜØYÙNˆİŠHOˆ›Û™N‚ˆSY\ÜØYÙP›Ş˜Üš]XØ[
Ù[‹º+á¹b*ùi,z-)H‹Y\ÜØYÙJBˆYˆÙ[‹œ›Ú™Xİ‚ˆÙ[‹X›WÛ[Ù[œ™Yœ™\Ú

B‚ˆYˆİ˜[œÛ][Û—İÙÙÛY
Ù[‹ÚXÚÙYˆ›ÛÛ
HOˆ›Û™N‚ˆÙ[‹™ÛØ˜[ÜÙ][™ÜË›\İİ˜[œÛ][Û—Ù[˜X›YHÚXÚÙYˆÙ[‹œÙ][™Ü×ÜİÜ™KœØ]™JÙ[‹™ÛØ˜[ÜÙ][™ÜÊBˆYˆÙ[‹œ›Ú™Xİ‚ˆ\[[™PÛÛÜ™[˜]ÜŠÙ[‹œ›Ú™Xİ
KœÙ]İ˜[œÛ][Û—Ù[˜X›Y
ÚXÚÙY
BˆÙ[‹—İ\]WİÛÜšÙ›İ×ÛX™[

B‚ˆYˆİ\]WİÛÜšÙ›İ×ÛX™[
Ù[ŠHOˆ›Û™N‚ˆYˆÙ[‹˜[œÛ][Û—İÙÙÛKš\ĞÚXÚÙY

N‚ˆÙ[‹ÛÜšÙ›İ×ÛX™[œÙ]^
¹odùbc{ï&º+á¹b*ùd#¹ïîú+äy..¹ë 9/dù.+y¥¡ÈŠBˆ[ÙN‚ˆÙ[‹ÛÜšÙ›İ×ÛX™[œÙ]^
¹odùbc{ï&¹.áz+á¹b*ùnm¹kï9aî¹c§ù¥¡ùkeùneHŠB‚ˆYˆÚXÚ×Ü]X[]JÙ[ŠHOˆ›Û™N‚ˆYˆ›İÙ[‹—Ü™\]Z\™WÜ›Ú™Xİ

N‚ˆ™]\›‚ˆÙYÛY[ÈHÙ[‹œ›Ú™Xİ›\İÜÙYÛY[Ê
Bˆ\WÜ]X[]WÙ›YÜÊÙYÛY[ËÙ[‹œ›Ú™Xİ™Ù]ÜÙ][™ÜÊ
KœİX]JBˆÚ]Ù[‹œ›Ú™Xİ˜ÛÛ›™Xİ[Û‚ˆ›ÜˆÙYÛY[[ˆÙYÛY[Î‚ˆ[\ÜœÛÛ‚‚ˆÙ[‹œ›Ú™Xİ˜ÛÛ›™Xİ[Û‹™^Xİ]Jˆ•TUHÙYÛY[ÈÑU]X[]WÙ›YÜ×ÚœÛÛOÈÒT‘HYOÈ‹ˆ
œÛÛ‹™[\ÊÛÜY
ÙYÛY[œ]X[]WÙ›YÜÊK[œİ\™WØ\ØÚZOQ˜[ÙJKÙYÛY[šY
Kˆ
BˆÙ[‹X›WÛ[Ù[œ™Yœ™\Ú

BˆÙ[‹—Ùš[\—Ü›Ø›[WÜ›İÜÊÙ[‹œ›Ø›[\×ØXİ[Û‹š\ĞÚXÚÙY

JBˆ›Ø›[WØÛİ[Hİ[J›ÛÛ
][Kœ]X[]WÙ›YÜÊH›Üˆ][H[ˆÙYÛY[ÊBˆÙ[‹œİ]\Ğ˜\Š
KœÚİÓY\ÜØYÙJˆ¹¨à9§éyk£9¢$;ï&Ü›Ø›[WØÛİ[H9§hykeùnezg :) y¬ê9¡#È‹L
B‚ˆYˆ˜[œÛ]WÜ[™[™ÊÙ[ŠHOˆ›Û™N‚ˆYˆ›İÙ[‹—Ü™\]Z\™WÜ›Ú™Xİ

N‚ˆ™]\›‚ˆYˆ›İÙ[‹œ›Ú™Xİ™Ù]ÜÙ][™ÜÊ
K˜[œÛ][Û—Ù[˜X›Y‚ˆSY\ÜØYÙP›Şš[™›Ü›X][ÛŠÙ[‹¹§*¹d+ùå*9ïîú+äH‹º+íùab9o 9d+úhmº`ê8 '9d+ùå*9ïîú+äx 'yo 9aløà ˆŠBˆ™]\›‚ˆ˜\ÙWİ\›ÚÈHR[œ]X[ÙË™Ù]^
ˆÙ[‹¹ïîú+äy§#yb¨H‹“Ü[RKXÛÛ\]X›H˜\ÙHT“;ï&ˆ‹^Hš‹ËÌLËŒŒŒNŒLMÍİŒH‚ˆ
BˆYˆ›İÚÎ‚ˆ™]\›‚ˆ[Ù[ÚÈHR[œ]X[ÙË™Ù]^
Ù[‹¹ïîú+äyª(yg¢È‹¹ª(yg¢ùd#yéì;ï&ˆŠBˆYˆ›İÚÈÜˆ›İ[Ù[‚ˆ™]\›‚ˆÙ][™ÜÈHÙ[‹œ›Ú™Xİ™Ù]ÜÙ][™ÜÊ
BˆÙ][™ÜË˜[œÛ][Û—Û[Ù[H[Ù[ˆÙ[‹œ›Ú™XİœØ]™WÜÙ][™ÜÊÙ][™ÜÊBˆ\×ÛØØ[H˜\ÙWİ\›œİ\İÚ]

š‹ËÌLËŒŒŒH‹š‹ËÛØØ[ÜİŠJBˆÙ^HHˆˆYˆ\×ÛØØ[[ÙH
Ü™Y[X[İÜ™J
K™Ù]
›Ü[˜ZKXÛÛ\]X›HŠHÜˆˆŠBˆYˆÙ[‹˜[œÛ][Û—İ™XY[™Ù[‹˜[œÛ][Û—İ™XYš\Ô[›š[™Ê
N‚ˆSY\ÜØYÙP›Şš[™›Ü›X][ÛŠÙ[‹¹ïîú+äy«hùg*:/ä:(c‹º+íùëbyo¡yodùbcyïîú+äy¢ny«(yk£9¢$8à ˆŠBˆ™]\›‚ˆÙ[‹—ÜØ]™WÜÚYWØÛÛ^

Bˆ™XYH˜[œÛ][Û•™XY
ˆ›Ú™XİÜ]\Ù[‹œ›Ú™Xİœ]ˆÛÛ™šYÏT›İšY\ÛÛ™šYÊˆ˜\ÙWİ\›X˜\ÙWİ\›\WÚÙ^OZÙ^KİXİ\™YÛİ]]Q˜[ÙKÙ™›[™O\Ù][™ÜË›Ù™›[™Bˆ
Kˆ›Û\\Ù[‹œ›Û\ÙY]ÔZ[•^

KˆÛÜÜØ\OWÜ\œÙWÙÛÜÜØ\JÙ[‹™ÛÜÜØ\WÙY]ÔZ[•^

JKˆ\™[\Ù[‹ˆ
BˆÙ[‹\Ú×Û\İ˜Y][Jˆ¹ïîú+ä{ï&Û[Ù[HŠBˆ™XYœ›ÙÜ™\ÜË˜ÛÛ›™Xİ
ˆ[X™HÛ™Kİ[ˆÙ[‹œİ]\Ğ˜\Š
KœÚİÓY\ÜØYÙJˆ¹ïîú+äy¢ny«(HÙÛ™_KŞİİ[HŠBˆ
Bˆ™XYœİXØÙYYY˜ÛÛ›™Xİ
Ù[‹—İ˜[œÛ][Û—ÜİXØÙYYY
Bˆ™XY™˜Z[Y˜ÛÛ›™Xİ
Ù[‹—İ˜[œÛ][Û—Ù˜Z[Y
Bˆ™XY™š[š\ÚY˜ÛÛ›™Xİ
™XY™[]S]\ŠBˆ™XY™š[š\ÚY˜ÛÛ›™Xİ
[X™NˆÙ]]ŠÙ[‹˜[œÛ][Û—İ™XY‹›Û™JJBˆÙ[‹˜[œÛ][Û—İ™XYH™XYˆ™XYœİ\

B‚ˆYˆİ˜[œÛ][Û—ÜİXØÙYYY
Ù[‹ÛÛ\]Yˆ[ØXÚYˆ[İÜYˆ›ÛÛ
HOˆ›Û™N‚ˆYˆÙ[‹œ›Ú™Xİ‚ˆÙ[‹X›WÛ[Ù[œ™Yœ™\Ú

BˆÙ[‹—Ùš[\—Ü›Ø›[WÜ›İÜÊÙ[‹œ›Ø›[\×ØXİ[Û‹š\ĞÚXÚÙY

JBˆYˆİÜY‚ˆÙ[‹œİ]\Ğ˜\Š
KœÚİÓY\ÜØYÙJˆˆ¹ïîú+äymì¹¦ ¹`g;ï&¹k£9¢$ØÛÛ\]YH9§h{ï#9ï$ùkf9doy.+HØØXÚYH9§hH‹Ìˆ
Bˆ[ÙN‚ˆÙ[‹œİ]\Ğ˜\Š
KœÚİÓY\ÜØYÙJˆˆ¹ïîú+äyk£9¢$ØÛÛ\]YH9§h{ï#9ï$ùkf9doy.+HØØXÚYH9§hH‹Ìˆ
B‚ˆYˆİ˜[œÛ][Û—Ù˜Z[Y
Ù[‹Y\ÜØYÙNˆİŠHOˆ›Û™N‚ˆSY\ÜØYÙP›Ş˜Üš]XØ[
Ù[‹¹ïîú+äyi,z-)H‹Y\ÜØYÙJBˆYˆÙ[‹œ›Ú™Xİ‚ˆÙ[‹X›WÛ[Ù[œ™Yœ™\Ú

B‚ˆYˆÙX\˜ÚÜ™\XÙJÙ[ŠHOˆ›Û™N‚ˆYˆ›İÙ[‹—Ü™\]Z\™WÜ›Ú™Xİ

N‚ˆ™]\›‚ˆÛİ\˜ÙKÚÈHR[œ]X[ÙË™Ù]^
Ù[‹¹¤'9í(¹¦ïù£hˆ‹¹§éy¢o¹c§ù¥¡ûï&ˆŠBˆYˆ›İÚÈÜˆ›İÛİ\˜ÙN‚ˆ™]\›‚ˆ™\XÙ[Y[ÚÈHR[œ]X[ÙË™Ù]^
Ù[‹¹¤'9í(¹¦ïù£hˆ‹¹¦ïù£h¹..»ï&ˆŠBˆYˆ›İÚÎ‚ˆ™]\›‚ˆÚ[™ÙYHˆ›ÜˆÙYÛY[[ˆÙ[‹œ›Ú™Xİ›\İÜÙYÛY[Ê
N‚ˆYˆÛİ\˜ÙH[ˆÙYÛY[œÛİ\˜ÙWİ^‚ˆÙ[‹œ›Ú™Xİ\]WÜÛİ\˜ÙWİ^
ˆÙYÛY[šYÙYÛY[œÛİ\˜ÙWİ^œ™\XÙJÛİ\˜ÙK™\XÙ[Y[
KØÚÏUYBˆ
BˆÚ[™ÙY
ÏHBˆÙ[‹X›WÛ[Ù[œ™Yœ™\Ú

BˆÙ[‹—Ùš[\—Ü›Ø›[WÜ›İÜÊÙ[‹œ›Ø›[\×ØXİ[Û‹š\ĞÚXÚÙY

JBˆÙ[‹œİ]\Ğ˜\Š
KœÚİÓY\ÜØYÙJˆ¹mì¹¦ïù£hˆØÚ[™ÙYH9§hykeùneH‹L
B‚ˆYˆÙš[\—Ü›Ø›[WÜ›İÜÊÙ[‹[˜X›Yˆ›ÛÛ
HOˆ›Û™N‚ˆ›Üˆ›İËÙYÛY[[ˆ[[Y\˜]JÙ[‹X›WÛ[Ù[œÙYÛY[ÊN‚ˆÙ[‹X›KœÙ]›İÒY[Š›İË[˜X›Y[™›İ›ÛÛ
ÙYÛY[œ]X[]WÙ›YÜÊJB‚ˆYˆ^ÜÙX[ÙÊÙ[ŠHOˆ›Û™N‚ˆYˆ›İÙ[‹—Ü™\]Z\™WÜ›Ú™Xİ

N‚ˆ™]\›‚ˆ›Ü›X]ÈH”Ô•

‹œÜ
NÎÕÙX••

‹
NÎĞTÔÈ

‹˜\ÜÊNÎù¥¡ù§+

‹
NÎÒ”ÓÓˆ

‹šœÛÛŠH‚ˆ]Ù[XİYHQš[QX[ÙË™Ù]Ø]™Qš[S˜[YJÙ[‹¹kï9aî¹keùneH‹ˆ‹›Ü›X]ÊBˆYˆ›İ]‚ˆ™]\›‚ˆ›Ü›X]ÛX\HÂˆ”Ô•ˆ^Ü›Ü›X]”Ô•ˆ•ÙX••ˆ^Ü›Ü›X]••ˆTÔÈˆ^Ü›Ü›X]TÔËˆ¹¥¡ù§+ˆ^Ü›Ü›X]•ˆ’”ÓÓˆˆ^Ü›Ü›X]’”ÓÓ‹ˆBˆİ]]Ù›Ü›X]H™^
ˆ
˜[YH›ÜˆÙ^K˜[YH[ˆ›Ü›X]ÛX\š][\Ê
HYˆÙ[XİYœİ\İÚ]
Ù^JJK›Û™Bˆ
Bˆİ]]Ù›Ü›X]Hİ]]Ù›Ü›X]Üˆ^Ü›Ü›X]
]
]
KœİY™š^›İš\
‹ˆŠK›İÙ\Š
JBˆÚÚXÙ\ÈHÈ¹c§ù¥¡È—Bˆ[İÙY™X\ÛÛˆHØ[—Ù^Ü
Ù[‹œ›Ú™Xİ›\İÜÙYÛY[Ê
K^ÜÛÛ[•S”ÓUSÓŠBˆYˆ[İÙY‚ˆÚÚXÙ\Ë™^[™
Èº+äy¥¡È‹¹cã:+ëH—JBˆÛÛ[Û˜[YKÚÈHR[œ]X[ÙË™Ù]][JÙ[‹¹kï9aî¹a¡yk®H‹¹a¡yk®{ï&ˆ‹ÚÚXÙ\ËY]X›OQ˜[ÙJBˆYˆ›İÚÎ‚ˆ™]\›‚ˆÛÛ[ÛX\HÂˆ¹c§ù¥¡Èˆ^ÜÛÛ[”ÓÕTÑKˆº+äy¥¡Èˆ^ÜÛÛ[•S”ÓUSÓ‹ˆ¹cã:+ëHˆ^ÜÛÛ[’SS‘ÕPSˆBˆN‚ˆ^ÜÜİX]\ÊˆÙ[‹œ›Ú™Xİ›\İÜÙYÛY[Ê
Kˆ]ˆİ]]Ù›Ü›X][İ]]Ù›Ü›X]ˆÛÛ[XÛÛ[ÛX\ØÛÛ[Û˜[YWKˆ
BˆÙ[‹œİ]\Ğ˜\Š
KœÚİÓY\ÜØYÙJˆ¹mì¹kï9aî»ï&Ü]H‹L
Bˆ^Ù\˜[œÛ][Û•[˜]˜Z[X›Q\œ›Ü‚ˆSY\ÜØYÙP›Şš[™›Ü›X][ÛŠÙ[‹¹¥è9¬åykï9aîº+äy¥¡È‹™X\ÛÛŠBˆ^Ù\^Ù\[Ûˆ\È^Î‚ˆSY\ÜØYÙP›Ş˜Üš]XØ[
Ù[‹¹kï9aî¹i,z-)H‹İŠ^ÊJB‚ˆYˆÜÙYZ×ÜÙ[XİY
Ù[‹[™^ˆS[Ù[[™^
HOˆ›Û™N‚ˆYˆ[™^š\Õ˜[Y

H[™[™^œ›İÊ
H[ŠÙ[‹X›WÛ[Ù[œÙYÛY[ÊN‚ˆÙ[‹œ^Y\‹œÙYZ×Û\ÊÙ[‹X›WÛ[Ù[œÙYÛY[ÖÚ[™^œ›İÊ
WKœİ\Û\ÊB‚ˆYˆÜ™\]Z\™WÜ›Ú™Xİ
Ù[ŠHOˆ›ÛÛ‚ˆYˆÙ[‹œ›Ú™Xİ‚ˆ™]\›ˆYBˆSY\ÜØYÙP›Şš[™›Ü›X][ÛŠÙ[‹¹¬¨y§"zhnyæëˆ‹º+íùab9¥¬9nî¹¢%¹¢dùo œİ›Úˆ:hnyæë¸à ˆŠBˆ™]\›ˆ˜[ÙB‚ˆYˆÚİ×ØX›İ]
Ù[ŠHOˆ›Û™N‚ˆSY\ÜØYÙP›Ş˜X›İ]
ˆÙ[‹ˆˆ¹alù.£ĞTÓSQ_H‹ˆˆĞTÓSQ_H××İ™\œÚ[Û—×ßW—¹/g: !{ï&ĞUUÔŸW¹êæ{ï&Ğ’SP’SWÕT“Wˆ‚ˆˆ‘Ú]X»ï&ÑÒUP—ÕT“W¹k¦9¥®yâb9§+;ï&‘Ú]Xˆ™[X\Ù\×º+®9cëú+à{ï&“RU—ˆ‚ˆ¹ë+9."y¥®yîá9.íº+®9cëú+éº)àHT‘ÔT•WÓ“ÕPÑTË›Y8à ˆ‹ˆ
B‚ˆYˆ˜YÑ[\‘]™[
Ù[‹]™[
HOˆ›Û™NˆÈ›ÜXNˆ‚ˆYˆ]™[›Z[YQ]J
Kš\Õ\›Ê
N‚ˆ]™[˜XØÙ\›ÜÜÙYXİ[ÛŠ
B‚ˆYˆ›Ü]™[
Ù[‹]™[
HOˆ›Û™NˆÈ›ÜXNˆ‚ˆ]ÈHÔ]
][KÓØØ[š[J
JH›Üˆ][H[ˆ]™[›Z[YQ]J
K\›Ê
HYˆ][Kš\ÓØØ[š[J
WBˆYˆ›İ]Î‚ˆ™]\›‚ˆš\œİH]ÖÌBˆYˆš\œİœİY™š^›İÙ\Š
HOH‹œİ›Úˆ‚ˆÙ[‹—ÛÜ[—Ü›Ú™XİÜ]
š\œİ
Bˆ[YˆÙ[‹—Ü™\]Z\™WÜ›Ú™Xİ

N‚ˆÙ[‹œ›Ú™XİœÙ]ÛYYXJš\œİ
BˆÙ[‹œ^Y\‹›ØY
š\œİ
Bˆ]™[˜XØÙ\›ÜÜÙYXİ[ÛŠ
B‚ˆYˆÛÜÙQ]™[
Ù[‹]™[
HOˆ›Û™NˆÈ›ÜXNˆ‚ˆYˆÙ[‹˜[œØÜš\[Û—İ™XY[™Ù[‹˜[œØÜš\[Û—İ™XYš\Ô[›š[™Ê
N‚ˆSY\ÜØYÙP›Şš[™›Ü›X][ÛŠˆÙ[‹ˆº+á¹b*ù.îùb¨yl&¹§*¹îäù§gÈ‹ˆ¹odùbcz+á¹b*ù¢ny«(yk£9¢$9nm¹/çykf9bcy.#z ïyalúeëyê"ùn£øà ¹ª(yg¢ú/æùê"ùo ¹n.:` 9aî¹d#»ï#:hnyæë¹cëù h¹i#xà ˆ‹ˆ
Bˆ]™[šYÛ›Ü™J
Bˆ™]\›‚ˆYˆÙ[‹˜[œÛ][Û—İ™XY[™Ù[‹˜[œÛ][Û—İ™XYš\Ô[›š[™Ê
N‚ˆYˆÙ[‹œ›Ú™Xİ[™Ù[‹œ›Ú™Xİ™Ù]ÜÙ][™ÜÊ
K˜[œÛ][Û—Ù[˜X›Y‚ˆ\[[™PÛÛÜ™[˜]ÜŠÙ[‹œ›Ú™Xİ
KœÙ]İ˜[œÛ][Û—Ù[˜X›Y
˜[ÙJBˆÙ[‹˜[œÛ][Û—İÙÙÛK˜›ØÚÔÚYÛ˜[ÊYJBˆÙ[‹˜[œÛ][Û—İÙÙÛKœÙ]ÚXÚÙY
˜[ÙJBˆÙ[‹˜[œÛ][Û—İÙÙÛK˜›ØÚÔÚYÛ˜[Ê˜[ÙJBˆSY\ÜØYÙP›Şš[™›Ü›X][ÛŠˆÙ[‹ˆ¹ïîú+äy¢ny«(yl&¹§*¹îäù§gÈ‹ˆ¹mì¹`g9«hº, ùn©¹¥¬9¢ny«(xà º+íùëbyo¡yodùbcz+íù¬`¹/çykf9k£9¢$9d#¹a£yalúeëyê"ùn£øà ˆ‹ˆ
Bˆ]™[šYÛ›Ü™J
Bˆ™]\›‚ˆYˆÙ[‹œ›Ú™Xİ‚ˆÙ[‹—ÜØ]™WÜÚYWØÛÛ^

BˆÙ[‹œ›Ú™Xİ˜ÛÜÙJ
BˆÙ[‹œ›Ú™XİH›Û™Bˆİ\\Š
K˜ÛÜÙQ]™[
]™[
B‚ˆYˆÜØ]™WÜÚYWØÛÛ^
Ù[ŠHOˆ›Û™N‚ˆYˆ›İÙ[‹œ›Ú™Xİ‚ˆ™]\›‚ˆÙ[‹œ›Ú™XİœØ]™WØXİ]™WÜ›Û\
Ù[‹œ›Û\ÙY]ÔZ[•^

JBˆÙ[‹œ›Ú™XİœØ]™WÙÛÜÜØ\JÜ\œÙWÙÛÜÜØ\JÙ[‹™ÛÜÜØ\WÙY]ÔZ[•^

JJB‚‚™YˆÙ›Ü›X]ÛZ[\ÙXÛÛ™Ê˜[YNˆ[
HOˆİ‚ˆİ\œË™[XZ[™\ˆH]›[Ù
˜[YK×ÍŒÌ
BˆZ[]\Ë™[XZ[™\ˆH]›[Ù
™[XZ[™\‹ŒÌ
BˆÙXÛÛ™ËZ[\ÙXÛÛ™ÈH]›[Ù
™[XZ[™\‹WÌ
Bˆ™]\›ˆˆÚİ\œÎŒ™NÛZ[]\ÎŒ™NÜÙXÛÛ™ÎŒ™KÛZ[\ÙXÛÛ™ÎŒÙH‚‚‚™YˆÜ\œÙWİ[Y\İ[\
˜[YNˆİŠHOˆ[‚ˆ\ÈH˜[YKœ™\XÙJ‹‹‹ˆŠKœÜ]
ˆŠBˆYˆ[Š\ÊHOHÎ‚ˆ˜Z\ÙH˜[YQ\œ›ÜŠ˜[YJBˆİ\œËZ[]\ÈH[
\ÖÌJK[
\ÖÌWJBˆÙXÛÛ™ÈH›Ø]
\ÖÌ—JBˆ™]\›ˆ›İ[™

İ\œÈ
ˆÍŒ
ÈZ[]\È
ˆŒ
ÈÙXÛÛ™ÊH
ˆL
B‚‚™YˆÜ\œÙWÙÛÜÜØ\J˜[YNˆİŠHOˆ\İİ\VÜİ‹İ—WN‚ˆ™\İ[H×Bˆ›Üˆ[™H[ˆ˜[YKœÜ][™\Ê
N‚ˆYˆHˆ[ˆ[™N‚ˆÛİ\˜ÙK\™Ù]H[™KœÜ]
H‹JBˆYˆÛİ\˜ÙKœİš\

H[™\™Ù]œİš\

N‚ˆ™\İ[˜\[™

Ûİ\˜ÙKœİš\

K\™Ù]œİš\

JJBˆ™]\›ˆ™\İ[
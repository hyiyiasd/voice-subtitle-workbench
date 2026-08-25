from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget


class MpvPlayerWidget(QWidget):
    """Small libmpv host. It degrades visibly when the portable DLL is absent."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setMinimumHeight(220)
        self._mpv = None
        self._handle = None
        self._qt_player: QMediaPlayer | None = None
        self._qt_audio: QAudioOutput | None = None
        self._message = QLabel("播放器：尚未加载媒体", self)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setStyleSheet("background:#171717;color:#cfcfcf;padding:20px")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._video = QVideoWidget(self)
        self._video.hide()
        layout.addWidget(self._video)
        layout.addWidget(self._message)
        controls = QWidget(self)
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(6, 3, 6, 3)
        self._play_button = QPushButton("播放")
        self._play_button.clicked.connect(self._toggle_qt_playback)
        self._position = QSlider(Qt.Orientation.Horizontal)
        self._position.setRange(0, 0)
        self._position.sliderMoved.connect(self._seek_qt_player)
        controls_layout.addWidget(self._play_button)
        controls_layout.addWidget(self._position)
        self._controls = controls
        self._controls.hide()
        layout.addWidget(controls)

    def initialize(self, library_path: Path) -> bool:
        if not library_path.is_file():
            return self._initialize_qt_fallback("未找到 libmpv，已切换到 Qt 系统播放后端")
        try:
            self._mpv = ctypes.CDLL(str(library_path))
            self._mpv.mpv_create.restype = ctypes.c_void_p
            self._mpv.mpv_initialize.argtypes = [ctypes.c_void_p]
            self._mpv.mpv_set_option_string.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
            ]
            self._mpv.mpv_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
            self._handle = self._mpv.mpv_create()
            if not self._handle:
                raise RuntimeError("mpv_create 失败")
            self._mpv.mpv_set_option_string(
                self._handle, b"wid", str(int(self.winId())).encode("ascii")
            )
            if self._mpv.mpv_initialize(self._handle) < 0:
                raise RuntimeError("mpv_initialize 失败")
            self._message.hide()
            return True
        except Exception as exc:
            self._handle = None
            return self._initialize_qt_fallback(f"libmpv 初始化失败，已切换后端：{exc}")

    def _initialize_qt_fallback(self, message: str) -> bool:
        try:
            self._qt_audio = QAudioOutput(self)
            self._qt_player = QMediaPlayer(self)
            self._qt_player.setAudioOutput(self._qt_audio)
            self._qt_player.setVideoOutput(self._video)
            self._qt_player.positionChanged.connect(self._position.setValue)
            self._qt_player.durationChanged.connect(
                lambda duration: self._position.setRange(0, max(0, duration))
            )
            self._qt_player.playbackStateChanged.connect(self._playback_state_changed)
            self._qt_player.errorOccurred.connect(self._qt_error)
            self._message.setText(message)
            return True
        except Exception as exc:
            self._message.setText(f"播放器初始化失败：{exc}")
            return False

    def load(self, media: Path) -> None:
        if not self._handle:
            if not self._qt_player:
                self._message.setText(f"媒体：{media.name}\n（播放器不可用）")
                self._message.show()
                return
            self._qt_player.setSource(QUrl.fromLocalFile(str(media)))
            self._controls.show()
            if media.suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm"}:
                self._message.hide()
                self._video.show()
            else:
                self._video.hide()
                self._message.setText(f"音频：{media.name}\n点击下方“播放”试听")
                self._message.show()
            return
        arguments = (ctypes.c_char_p * 3)(
            b"loadfile", str(media).encode(sys.getfilesystemencoding()), None
        )
        self._mpv.mpv_command(self._handle, arguments)

    def seek_ms(self, milliseconds: int) -> None:
        if not self._handle:
            if self._qt_player:
                self._qt_player.setPosition(milliseconds)
            return
        arguments = (ctypes.c_char_p * 4)(
            b"seek", f"{milliseconds / 1000:.3f}".encode("ascii"), b"absolute", None
        )
        self._mpv.mpv_command(self._handle, arguments)

    def _toggle_qt_playback(self) -> None:
        if not self._qt_player:
            return
        if self._qt_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._qt_player.pause()
        else:
            self._qt_player.play()

    def _seek_qt_player(self, milliseconds: int) -> None:
        if self._qt_player:
            self._qt_player.setPosition(milliseconds)

    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self._play_button.setText(
            "暂停" if state == QMediaPlayer.PlaybackState.PlayingState else "播放"
        )

    def _qt_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        self._video.hide()
        self._message.setText(f"媒体播放失败：{message}")
        self._message.show()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._handle and self._mpv:
            self._mpv.mpv_terminate_destroy(self._handle)
            self._handle = None
        if self._qt_player:
            self._qt_player.stop()
        super().closeEvent(event)

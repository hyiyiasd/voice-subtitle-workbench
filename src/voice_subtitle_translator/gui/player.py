from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class MpvPlayerWidget(QWidget):
    """Small libmpv host. It degrades visibly when the portable DLL is absent."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setMinimumHeight(220)
        self._mpv = None
        self._handle = None
        self._message = QLabel("播放器：尚未加载媒体", self)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setStyleSheet("background:#171717;color:#cfcfcf;padding:20px")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._message)

    def initialize(self, library_path: Path) -> bool:
        if not library_path.is_file():
            self._message.setText("未找到 libmpv；字幕编辑仍可使用")
            return False
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
            self._message.setText(f"libmpv 初始化失败：{exc}")
            self._handle = None
            return False

    def load(self, media: Path) -> None:
        if not self._handle:
            self._message.setText(f"媒体：{media.name}\n（libmpv 不可用）")
            self._message.show()
            return
        arguments = (ctypes.c_char_p * 3)(
            b"loadfile", str(media).encode(sys.getfilesystemencoding()), None
        )
        self._mpv.mpv_command(self._handle, arguments)

    def seek_ms(self, milliseconds: int) -> None:
        if not self._handle:
            return
        arguments = (ctypes.c_char_p * 4)(
            b"seek", f"{milliseconds / 1000:.3f}".encode("ascii"), b"absolute", None
        )
        self._mpv.mpv_command(self._handle, arguments)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._handle and self._mpv:
            self._mpv.mpv_terminate_destroy(self._handle)
            self._handle = None
        super().closeEvent(event)

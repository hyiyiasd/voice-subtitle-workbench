from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget


class WaveformWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(72)
        self._samples: list[float] = []

    def set_samples(self, samples: list[float]) -> None:
        self._samples = samples
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#20242a"))
        painter.setPen(QPen(QColor("#55b7ff"), 1))
        center = self.height() // 2
        if not self._samples:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "波形将在音频预处理后显示")
            return
        width = max(1, self.width())
        for x in range(width):
            index = min(len(self._samples) - 1, x * len(self._samples) // width)
            amplitude = max(0.0, min(1.0, abs(self._samples[index])))
            height = int(amplitude * center)
            painter.drawLine(x, center - height, x, center + height)


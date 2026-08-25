from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

TASK_PATH_ROLE = int(Qt.ItemDataRole.UserRole)


class BatchOperationDialog(QDialog):
    """Select media files and one operation without changing the queue immediately."""

    def __init__(
        self,
        groups: list[tuple[Path, list[tuple[Path, bool]]]],
        *,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择批量操作")
        self.resize(680, 520)

        explanation = QLabel(
            "勾选要处理的文件。勾选文件夹会选择整个文件夹，之后仍可取消其中单个文件。"
        )
        explanation.setWordWrap(True)

        operation_row = QHBoxLayout()
        operation_row.addWidget(QLabel("要执行的操作："))
        self.operation_combo = QComboBox()
        self.operation_combo.addItem("转文字", "transcribe")
        self.operation_combo.addItem("翻译已有字幕", "translate")
        self.operation_combo.addItem("SoVITS 改配音（暂未实现）", "sovits")
        sovits_item = self.operation_combo.model().item(2)
        if sovits_item:
            sovits_item.setEnabled(False)
        operation_row.addWidget(self.operation_combo, 1)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["文件夹 / 媒体"])
        for root, media_items in groups:
            group = QTreeWidgetItem([f"📁 {root.name or root}"])
            group.setToolTip(0, str(root))
            group.setFlags(
                group.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsAutoTristate
            )
            self.tree.addTopLevelItem(group)
            for media, checked in media_items:
                try:
                    label = str(media.relative_to(root))
                except ValueError:
                    label = media.name
                child = QTreeWidgetItem([label])
                child.setData(0, TASK_PATH_ROLE, str(media))
                child.setToolTip(0, str(media))
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(
                    0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )
                group.addChild(child)
            group.setExpanded(True)

        select_all = QPushButton("全部勾选")
        select_all.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        clear_all = QPushButton("全部取消")
        clear_all.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        selection_row = QHBoxLayout()
        selection_row.addWidget(select_all)
        selection_row.addWidget(clear_all)
        selection_row.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_selected)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(explanation)
        layout.addLayout(operation_row)
        layout.addWidget(self.tree, 1)
        layout.addLayout(selection_row)
        layout.addWidget(buttons)

    @property
    def selected_operation(self) -> str:
        return str(self.operation_combo.currentData())

    def selected_paths(self) -> list[Path]:
        paths: list[Path] = []
        for group_index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(group_index)
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                if child.checkState(0) == Qt.CheckState.Checked:
                    paths.append(Path(str(child.data(0, TASK_PATH_ROLE))).resolve())
        return paths

    def _set_all(self, state: Qt.CheckState) -> None:
        for group_index in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(group_index).setCheckState(0, state)

    def _accept_if_selected(self) -> None:
        if not self.selected_paths():
            QMessageBox.information(self, "没有选择文件", "请至少勾选一个媒体文件。")
            return
        self.accept()

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)


@dataclass(frozen=True, slots=True)
class TranslationServiceSettings:
    provider: str
    base_url: str
    model: str
    api_key: str
    structured_output: bool


PRESETS = [
    ("OpenAI", "openai", "https://api.openai.com/v1", True),
    ("DeepSeek", "deepseek", "https://api.deepseek.com/v1", False),
    ("智谱 GLM", "zhipu", "https://open.bigmodel.cn/api/paas/v4", False),
    ("Ollama（本地）", "ollama", "http://127.0.0.1:11434/v1", False),
    ("LM Studio（本地）", "lm-studio", "http://127.0.0.1:1234/v1", False),
    ("自定义 OpenAI-compatible", "openai-compatible", "", False),
]


class TranslationSettingsDialog(QDialog):
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        model: str,
        has_saved_key: bool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("翻译服务设置")
        self.resize(560, 250)

        self.provider_combo = QComboBox()
        for label, provider_id, preset_url, structured in PRESETS:
            self.provider_combo.addItem(
                label,
                {"id": provider_id, "base_url": preset_url, "structured": structured},
            )
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("例如：https://api.example.com/v1")
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("例如：gpt-4.1-mini、deepseek-chat、qwen2.5:7b")
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText(
            "已保存；留空保持不变" if has_saved_key else "本地服务可留空"
        )

        form = QFormLayout()
        form.addRow("服务类型：", self.provider_combo)
        form.addRow("Base URL：", self.base_url_edit)
        form.addRow("模型名称：", self.model_edit)
        form.addRow("API Key：", self.key_edit)
        note = QLabel("API Key 只保存到 Windows 凭据管理器，不写入项目或配置文件。")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

        index = next(
            (
                item_index
                for item_index, (_, provider_id, _, _) in enumerate(PRESETS)
                if provider_id == provider
            ),
            len(PRESETS) - 1,
        )
        self.provider_combo.setCurrentIndex(index)
        preset = self.provider_combo.currentData()
        self.base_url_edit.setText(base_url or preset["base_url"])
        self.model_edit.setText(model)
        self.provider_combo.currentIndexChanged.connect(self._preset_changed)

    def _preset_changed(self) -> None:
        preset = self.provider_combo.currentData()
        self.base_url_edit.setText(preset["base_url"])

    def values(self) -> TranslationServiceSettings:
        preset = self.provider_combo.currentData()
        return TranslationServiceSettings(
            provider=preset["id"],
            base_url=self.base_url_edit.text().strip(),
            model=self.model_edit.text().strip(),
            api_key=self.key_edit.text(),
            structured_output=bool(preset["structured"]),
        )

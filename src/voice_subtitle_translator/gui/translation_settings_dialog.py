from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from voice_subtitle_translator.credentials import CredentialStore
from voice_subtitle_translator.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderConfig,
)


@dataclass(frozen=True, slots=True)
class TranslationServiceSettings:
    provider: str
    base_url: str
    model: str
    api_key: str
    structured_output: bool


PRESETS = [
    ("OpenAI", "openai", "https://api.openai.com/v1", "gpt-4.1-mini", True),
    ("DeepSeek", "deepseek", "https://api.deepseek.com", "deepseek-v4-flash", True),
    ("智谱 GLM", "zhipu", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash", False),
    ("Ollama（本地）", "ollama", "http://127.0.0.1:11434/v1", "", False),
    ("LM Studio（本地）", "lm-studio", "http://127.0.0.1:1234/v1", "", False),
    ("自定义 OpenAI-compatible", "openai-compatible", "", "", False),
]


class InterfaceTestThread(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, config: ProviderConfig, *, model: str, text: str, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.model = model
        self.text = text
        self.provider: OpenAICompatibleProvider | None = None

    def run(self) -> None:
        try:
            self.provider = OpenAICompatibleProvider(self.config)
            result = self.provider.test_connection(text=self.text, model=self.model)
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if self.provider:
                try:
                    self.provider.close()
                except Exception:
                    pass
                self.provider = None

    def cancel(self) -> None:
        if self.provider:
            try:
                self.provider.close()
            except Exception:
                pass


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
        self.resize(620, 350)
        self.test_thread: InterfaceTestThread | None = None

        self.provider_combo = QComboBox()
        for label, provider_id, preset_url, preset_model, structured in PRESETS:
            self.provider_combo.addItem(
                label,
                {
                    "id": provider_id,
                    "base_url": preset_url,
                    "model": preset_model,
                    "structured": structured,
                },
            )
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("例如：https://api.example.com/v1")
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText(
            "例如：gpt-4.1-mini、deepseek-v4-flash、qwen2.5:7b"
        )
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText(
            "已保存；留空保持不变" if has_saved_key else "本地服务可留空"
        )
        self.test_text_edit = QLineEdit("请用简体中文回答：API 接口测试成功。")
        self.test_button = QPushButton("测试接口")
        self.test_button.clicked.connect(self._test_interface)
        test_row = QHBoxLayout()
        test_row.addWidget(self.test_text_edit, 1)
        test_row.addWidget(self.test_button)

        form = QFormLayout()
        form.addRow("服务类型：", self.provider_combo)
        form.addRow("Base URL：", self.base_url_edit)
        form.addRow("模型名称：", self.model_edit)
        form.addRow("API Key：", self.key_edit)
        form.addRow("测试内容：", test_row)
        note = QLabel("API Key 只保存到 Windows 凭据管理器，不写入项目或配置文件。")
        note.setWordWrap(True)
        self.provider_note = QLabel()
        self.provider_note.setWordWrap(True)
        self.provider_note.setStyleSheet("color:#8a5a00")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.provider_note)
        layout.addWidget(note)
        layout.addWidget(buttons)

        index = next(
            (
                item_index
                for item_index, (_, provider_id, _, _, _) in enumerate(PRESETS)
                if provider_id == provider
            ),
            len(PRESETS) - 1,
        )
        self.provider_combo.setCurrentIndex(index)
        preset = self.provider_combo.currentData()
        if provider == "deepseek" and base_url.rstrip("/") == "https://api.deepseek.com/v1":
            self.base_url_edit.setText("https://api.deepseek.com")
        else:
            self.base_url_edit.setText(base_url or preset["base_url"])
        if provider == "deepseek" and model in ("", "deepseek-chat", "deepseek-reasoner"):
            self.model_edit.setText("deepseek-v4-flash")
        else:
            self.model_edit.setText(model or preset["model"])
        self.provider_combo.currentIndexChanged.connect(self._preset_changed)
        self._update_provider_note()

    def _preset_changed(self) -> None:
        preset = self.provider_combo.currentData()
        self.base_url_edit.setText(preset["base_url"])
        self.model_edit.setText(preset["model"])
        self._update_provider_note()

    def _update_provider_note(self) -> None:
        if self.provider_combo.currentData()["id"] == "deepseek":
            self.provider_note.setText(
                "DeepSeek 当前官方入口为 https://api.deepseek.com；"
                "旧的 deepseek-chat / deepseek-reasoner 已停止使用，默认改为 deepseek-v4-flash。"
            )
        else:
            self.provider_note.clear()

    def _test_interface(self) -> None:
        values = self.values()
        if not values.base_url or not values.model or not self.test_text_edit.text().strip():
            QMessageBox.information(self, "无法测试", "请填写 Base URL、模型名称和测试内容。")
            return
        is_local = values.base_url.startswith(("http://127.0.0.1", "http://localhost"))
        key = values.api_key or CredentialStore().get(values.provider) or ""
        if not is_local and not key:
            QMessageBox.information(self, "缺少 API Key", "请先输入或保存该服务的 API Key。")
            return
        self.test_button.setEnabled(False)
        self.test_button.setText("测试中…")
        thread = InterfaceTestThread(
            ProviderConfig(
                id=values.provider,
                base_url=values.base_url,
                api_key=key,
                timeout_seconds=30,
            ),
            model=values.model,
            text=self.test_text_edit.text().strip(),
            parent=self,
        )
        thread.succeeded.connect(self._test_succeeded)
        thread.failed.connect(self._test_failed)
        thread.finished.connect(self._test_finished)
        self.test_thread = thread
        thread.start()

    def _test_succeeded(self, result: str) -> None:
        QMessageBox.information(self, "接口测试成功", result)

    def _test_failed(self, message: str) -> None:
        QMessageBox.critical(self, "接口测试失败", message)

    def _test_finished(self) -> None:
        thread = self.test_thread
        self.test_thread = None
        self.test_button.setEnabled(True)
        self.test_button.setText("测试接口")
        if thread:
            thread.deleteLater()

    def reject(self) -> None:
        if self.test_thread and self.test_thread.isRunning():
            self.test_thread.cancel()
            if not self.test_thread.wait(1500):
                self.test_thread.terminate()
                self.test_thread.wait(500)
        super().reject()

    def values(self) -> TranslationServiceSettings:
        preset = self.provider_combo.currentData()
        return TranslationServiceSettings(
            provider=preset["id"],
            base_url=self.base_url_edit.text().strip(),
            model=self.model_edit.text().strip(),
            api_key=self.key_edit.text(),
            structured_output=bool(preset["structured"]),
        )

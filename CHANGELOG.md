# 变更日志

本项目遵循语义化版本。

## [0.1.0] - Unreleased

### 新增

- 绿色目录、uv/Python 3.11 工程和 Windows 便携构建骨架。
- `.vstproj` SQLite 项目、字幕修订、任务恢复和翻译缓存。
- 原文/译文/双语字幕导出及质量检查。
- 独立工作进程协议、Provider 接口和 OpenAI-compatible 翻译适配器。
- Silero VAD、ReazonSpeech K2 和 faster-whisper 的本地识别链路及固定模型清单。
- 带逐文件 SHA-256 校验和下载进度的绿色模型管理器。
- 简体中文 PySide6 工作台。
- 媒体优先工作流：拖入 MP3/WAV/MP4 自动建项目、识别并按开关继续翻译。
- 可持久化的 API、本地 Ollama/LM Studio 和自定义翻译服务设置。
- 修正模型容量显示放大 1024 倍的问题，并补充 tiny、base、small 三档多语言模型。
- 模型管理器增加详细介绍、推荐场景、中文语言名称和更清晰的硬件提示。
- 模型下载支持用户现有的 HTTPS 代理设置，离线模式仍会在创建网络客户端前拒绝下载。
- tiny、base、small 增加 ModelScope、HF Mirror、Hugging Face 三级下载策略；所有通道共用固定大小和 SHA-256 校验。

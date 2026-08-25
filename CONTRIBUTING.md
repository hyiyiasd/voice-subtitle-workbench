# 贡献指南

感谢参与“语音转字幕”。提交代码前请：

1. 从议题开始说明问题和预期行为。
2. 运行 `scripts\bootstrap.ps1` 建立工作区内环境。
3. 运行 `scripts\test.ps1` 和 `uv run --no-sync ruff check .`。
4. 不提交模型、媒体、API Key、GPU 运行包、构建产物或 `.vstproj`。
5. 新增网络访问必须接入离线策略并提供“不会联网”的测试。
6. 新增第三方二进制或模型必须更新供应清单、许可证、源码地址和 SHA-256。

提交即表示你有权按项目的 MIT License 提供该贡献。

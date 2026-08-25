# 语音转字幕

Windows 优先、简体中文、解压即用的字幕工作台。项目采用 MIT License，官方构建仅通过本项目的 GitHub Releases 发布。

官方仓库：https://github.com/hyiyiasd/voice-subtitle-workbench

当前版本：`0.1.0` 开发阶段。项目系统、字幕编辑、质量检查、原文/译文导出和 OpenAI-compatible 翻译闭环已经进入实现；ASR 模型和 GPU 运行包不会随源码或主程序 ZIP 分发，必须通过经过 SHA-256 校验的模型清单按需安装。

## 主要能力

- 直接拖入或选择 MP3、WAV、M4A、FLAC、MP4、MKV、MOV、WebM 等媒体，自动建立项目并开始转文字。
- 可导入或直接拖入整个文件夹；任务队列按文件夹分组、可展开，每个文件显示当前阶段和独立进度条。
- 文件夹和媒体提供复选框，支持整组全选/取消后再排除单个文件；“批量操作”可对所选文件执行转文字或翻译。
- 右键媒体可单独转文字、翻译或查看详情；双击会切换媒体，并在中央详情区显示处理阶段、进度和历史记录。
- SoVITS 改配音仅在任务菜单中标记为“暂未实现”，不注册实现，也不引入相关依赖。
- 单文件 `.vstproj` SQLite 项目，保存字幕、译文、修订、任务状态、缓存、提示词和术语。
- 两种明确工作流：仅语音识别，以及识别后翻译为简体中文。
- 翻译首次默认关闭；关闭时不创建翻译 Provider、不读取 API Key、不发起翻译请求。
- 日语、英语和日英混合 ASR Provider 接口；支持 ReazonSpeech K2 与 faster-whisper 的本地实现。
- OpenAI 原生结构化输出，以及 DeepSeek、智谱、Ollama、LM Studio 和自定义 OpenAI-compatible 服务。
- SRT、VTT、ASS、TXT 和 JSON 导出；没有有效译文时只允许原文导出。
- 人工修改自动锁定，重新识别只保存候选，不覆盖锁定字幕。
- 模型进程与 GUI 分离；批次提交后可在模型崩溃或程序重启后恢复。

## 绿色目录

程序不使用 AppData。发布 ZIP 解压后，程序会在自身目录创建：

```text
data/
  config/
  models/
  cache/
  logs/
  temp/
  gpu-runtime/
  projects/       # 拖入媒体时自动创建的项目
```

目录不可写时程序会停止并提示移动位置，不会回退到系统盘。API Key 是唯一例外：它保存到 Windows 凭据管理器。

开发环境同样限制在仓库目录。所有 PowerShell 命令应先加载 `scripts/env.ps1`，它会把 Python、uv 缓存、临时目录和模型缓存指向 `.local`。

```powershell
.\scripts\bootstrap.ps1
.\scripts\test.ps1
.\scripts\run.ps1
```

系统无需预装 Python；`uv` 会将 Python 3.11 安装到 `.local\python`。
项目位于中文路径时使用非 editable 安装，避免 Windows Python 以系统代码页读取 `.pth` 路径。

## CLI

```powershell
. .\scripts\env.ps1
uv run --no-sync voice-subtitle-translator create demo.vstproj --media sample.mp4
uv run --no-sync voice-subtitle-translator add-segment demo.vstproj --start-ms 0 --end-ms 2500 --text "こんにちは"
uv run --no-sync voice-subtitle-translator export demo.vstproj demo.srt --format srt --content source
uv run --no-sync voice-subtitle-translator info demo.vstproj
uv run --no-sync voice-subtitle-translator models list
uv run --no-sync voice-subtitle-translator models download silero-vad-v6
uv run --no-sync voice-subtitle-translator models download reazonspeech-k2-ja
uv run --no-sync voice-subtitle-translator transcribe demo.vstproj --model reazonspeech-k2-ja
```

CLI 的 `--translate/--no-translate` 必须放在子命令之前。省略时单次 CLI 调用默认不翻译。`--offline` 会禁止远程 Provider，并设置模型库离线环境。

便携 ZIP 中可直接使用 `vst-cli.exe` 执行同样的命令，无需安装 Python。

## 模型与硬件

- 日语轻量首选：ReazonSpeech K2 日语模型，约 153 MB，无需独立显卡。
- 快速预览：faster-whisper `tiny`，约 75 MB，体积最小但准确率较低。
- 低配置日常使用：faster-whisper `base`，约 141 MB。
- 通用均衡推荐：faster-whisper `small`，约 464 MB。
- 复杂音频和正式字幕：faster-whisper `medium`，约 1.43 GB。
- RTX 3070 高质量高速档：faster-whisper `large-v3-turbo`，约 1.51 GB。
- 日英混合 ReazonSpeech K2 `ja-en` 目前只保留兼容条目，上游恢复稳定访问前不开放下载。
- GPU 推理提供 RTX 50 系推荐、RTX 20/30/40 系均衡、FP16 高精度、BF16 实验和 CPU INT8 档位；RTX 5070 默认推荐 `int8_float16`。
- VAD、ASR、本地翻译和 GPU 运行包均按需安装，不进入 Git 或主 ZIP。

模型管理器只接受包含固定来源、版本、大小、许可证和 SHA-256 的清单。Silero VAD、ReazonSpeech K2 日语版以及五个 faster-whisper 模型已经固定到具体上游修订，可通过 GUI 或 CLI 按需下载并逐文件校验。模型列表提供简短推荐场景，选中后会显示更完整的语言、速度、精度、硬件需求和适用音频介绍。日英混合模型的官方匿名下载地址当前不可用，因此清单会安全地拒绝自动下载，不会改用来源不明的镜像。

模型管理器会始终显示模型总目录和当前模型安装目录的绝对路径。自动下载失败时，错误窗口也会给出手动放置目录；用户可以自行复制模型文件到该目录，再使用“校验所选”检查文件完整性。

tiny、base、small 按“ModelScope 国内 CDN → `hf-mirror.com` → Hugging Face 官方源”的顺序自动尝试。三个通道均固定到已核对的模型修订；镜像只作为传输通道，模型来源、文件大小和 SHA-256 仍以官方清单为准。镜像返回不同内容时程序会删除临时文件并尝试下一通道，所有通道均失败才会报错。medium 和 large-v3-turbo 在国内镜像连通性完成验证前仍只使用官方源。

媒体音轨优先由便携版 FFmpeg 标准化；开发构建尚未提供经过供应链审计的 FFmpeg 时，会使用随 faster-whisper 安装的 PyAV 运行库解码常见音视频格式。播放器优先使用 libmpv；便携包中未提供 libmpv 时，自动使用 Qt Multimedia 播放音视频。

GPU 设置可按需下载 NVIDIA 官方 CUDA 12.9、cuBLAS 12.9 和 cuDNN 9.24 绿色运行库，下载量约 1.27 GB，保存到程序旁 `data\gpu-runtime`，不进入 Git 或主 ZIP。下载按“清华 PyPI 镜像 → 阿里云 PyPI 镜像 → 官方 PyPI”的顺序自动回退，所有通道共用固定文件大小和 SHA-256 校验。设置页始终显示 DLL 保存目录的绝对路径，失败时列出手动放置目录和必需文件名。来自 NVIDIA 的软件包会保留 NVIDIA Proprietary Software 许可文件。GPU 初始化失败时会显示中文说明，不会静默改用 CPU。

主工具栏保留添加媒体、导入文件夹、批量操作、开始识别和导出字幕。新建/打开项目位于“项目”，批量选择、任务恢复、字幕检查和翻译位于“处理”，模型、GPU 与翻译服务位于“设置”，版本和许可信息位于“帮助”。

## 翻译、隐私与联网

字幕被当作不可信数据，和系统翻译指令分开传递。返回结果必须覆盖完全一致的稳定字幕 ID；缺失、重复和未知 ID 会导致批次缩小并重试。

使用远程翻译服务时，字幕、提示词、标题、背景、人物关系和术语可能发送给界面显示的服务商。关闭翻译时不会发送这些数据。离线模式下，程序禁止远程 HTTP 请求；已下载的本地模型仍可使用。

在 GUI 的“翻译服务设置”中可选择 OpenAI、DeepSeek、智谱、Ollama、LM Studio 或自定义 OpenAI-compatible 服务。拖入媒体前勾选“启用翻译”后，识别完成会自动继续翻译；未勾选时只执行识别，不读取 API Key。

API Key 不写入项目、配置或日志，只保存在 Windows 凭据管理器。Token 和费用信息均为估算，实际账单以服务商为准。

## 架构

```text
PySide6 GUI / CLI
        │
        ├── Project service ── .vstproj SQLite（唯一写入者）
        │
        └── Pipeline coordinator
                 ├── VAD worker
                 ├── ASR worker
                 └── Translation worker
```

模型工作进程不直接写项目。每个批次成功后，主进程用一个数据库事务保存结果和恢复游标。

## 构建便携 ZIP

```powershell
.\scripts\build.ps1
```

构建采用 PyInstaller `onedir`，然后生成 ZIP 和 SHA-256。正式 Release 还必须提供经过审计的 LGPL FFmpeg/libmpv 二进制、对应源码获取方式、构建参数、SBOM 和第三方许可证；供应清单未完成时不得发布。

首版未签名，Windows 可能显示 SmartScreen 提示。请只从 GitHub Releases 获取，并对照同一 Release 中的 `.sha256` 文件：

```powershell
Get-FileHash .\voice-subtitle-translator-0.1.0-windows-x64.zip -Algorithm SHA256
```

## 暂定路线图

> 下一版本（暂定）：AI 声音替换与配音实验功能。计划尝试分离原人声和背景声，通过 GPT-SoVITS、So-VITS-SVC 或兼容服务，以用户提供并具有合法授权的训练音色重新生成或转换人声，再根据字幕时间轴完成对齐和混音。最终实现方式取决于模型兼容性、授权要求、显存占用和音质测试结果。

当前版本只保留 `VoiceProvider`、`StemSeparationProvider` 和 `VoiceRenderRequest` 接口，不注册实现、不展示按钮、不包含模型、训练数据或依赖。未来使用前必须要求用户确认拥有声音、数据集和输出内容的合法授权。

## 官方声明

> 本项目是免费开源软件，官方构建版本仅通过本项目 GitHub Releases 发布。请勿冒充官方、删除版权或许可证声明、捆绑恶意软件，或以“官方授权”名义误导收费。依据 MIT License，第三方可以复制、修改、再发布及收费，但必须保留版权与许可声明；任何第三方修改版均不代表作者立场。请通过 SHA-256 校验文件完整性。

作者：゚八奈見杏菜  
B站主页：https://space.bilibili.com/358626768

# 语音转字幕

Windows 优先、简体中文、解压即用的字幕工作台。项目采用 MIT License，官方构建仅通过本项目的 GitHub Releases 发布。

官方仓库：https://github.com/hyiyiasd/voice-subtitle-workbench

当前版本：`0.1.0` 开发阶段。媒体识别为原文 SRT、原文 SRT 翻译为中文 SRT、字幕编辑和质量检查已经形成闭环；ASR 模型和 GPU 运行包不会随源码或主程序 ZIP 分发，必须通过经过 SHA-256 校验的模型清单按需安装。

## 主要能力

- 直接拖入或选择 MP3、WAV、M4A、FLAC、MP4、MKV、MOV、WebM 等媒体，只加入任务队列，不会自动识别；通过开始识别、右键或批量操作手动启动。
- 可导入或直接拖入整个文件夹；任务树平时只显示文件名，处理期间才在文件名右侧临时显示一条很短的无文字进度条，当前阶段和百分比集中显示在中央详情区。
- 文件夹和媒体提供复选框，支持整组全选/取消后再排除单个文件；“批量操作”可对所选文件执行转文字或翻译。
- 右键媒体可单独转文字、翻译或查看详情；双击会切换媒体，并在中央紧凑详情区显示处理阶段和进度。
- SoVITS 改配音仅在任务菜单中标记为“暂未实现”，不注册实现，也不引入相关依赖。
- GUI 不显示项目文件概念。识别完成立即生成原文 SRT；用户确认后再选择原文 SRT 生成中文 SRT。
- 恢复游标、缓存和修订只保存在 `data/state/*.sqlite3` 内部状态中，不需要用户打开或管理。
- 翻译首次默认关闭；关闭时不创建翻译 Provider、不读取 API Key、不发起翻译请求。
- 日语、英语和日英混合 ASR Provider 接口；支持 ReazonSpeech K2 与 faster-whisper 的本地实现。
- OpenAI 原生结构化输出，以及 DeepSeek、智谱、Ollama、LM Studio 和自定义 OpenAI-compatible 服务。
- GUI 将 SRT 自动导出到绿色字幕目录，文件名与原媒体相同；CLI 仍支持 SRT、VTT、ASS、TXT 和 JSON。
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
  state/          # 软件内部恢复与缓存状态，不需要用户操作
  subtitles/
    原文/          # 与媒体同名的原文 SRT
    中文/          # 与原文 SRT 同名的中文 SRT
    双语/          # 与媒体同名的双语 SRT
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

当前版本以 GUI 的两步 SRT 工作流为主。CLI 暂时只公开模型检查和下载命令；内部状态调试命令不作为用户工作流。

```powershell
. .\scripts\env.ps1
uv run --no-sync voice-subtitle-translator models list
uv run --no-sync voice-subtitle-translator models download silero-vad-v6
uv run --no-sync voice-subtitle-translator models download reazonspeech-k2-ja
```

便携 ZIP 中可直接使用 `vst-cli.exe` 执行同样的命令，无需安装 Python。

## 模型与硬件

- 日语轻量首选：ReazonSpeech K2 日语模型，约 153 MB，无需独立显卡。
- 日语高质量首选：Kotoba-Whisper v2.0 Faster，约 1.41 GB，针对日语蒸馏并可直接使用 CTranslate2 GPU 推理。
- 快速预览：faster-whisper `tiny`，约 75 MB，体积最小但准确率较低。
- 低配置日常使用：faster-whisper `base`，约 141 MB。
- 通用均衡推荐：faster-whisper `small`，约 464 MB。
- 复杂音频和正式字幕：faster-whisper `medium`，约 1.43 GB。
- 完整质量档：faster-whisper `large-v3`，约 2.88 GB，适合复杂日语、噪声和日英混说。
- RTX 3070 高质量高速档：faster-whisper `large-v3-turbo`，约 1.51 GB。
- ReazonSpeech NeMo v2 作为数小时日语长音频兼容计划展示；当前没有集成 NeMo/PyTorch 运行时，因此不开放下载或识别。
- 日英混合 ReazonSpeech K2 `ja-en` 目前只保留兼容条目，上游恢复稳定访问前不开放下载。
- GPU 推理提供 RTX 50 系推荐、RTX 20/30/40 系均衡、FP16 高精度、BF16 实验和 CPU INT8 档位；RTX 5070 默认推荐 `int8_float16`。
- VAD、ASR、本地翻译和 GPU 运行包均按需安装，不进入 Git 或主 ZIP。

模型管理器只接受包含固定来源、版本、大小、许可证和 SHA-256 的清单。Silero VAD、ReazonSpeech K2 日语版、Kotoba-Whisper v2.0 Faster 以及六个 faster-whisper 模型已经固定到具体上游修订，可通过 GUI 或 CLI 按需下载并逐文件校验。模型列表提供简短推荐场景，选中后会显示更完整的语言、速度、精度、硬件需求和适用音频介绍。日英混合 K2 与 NeMo v2 只显示兼容计划，当前会安全地拒绝自动下载。

模型管理器会始终显示模型总目录和当前模型安装目录的绝对路径。自动下载失败时，错误窗口也会给出手动放置目录；用户可以自行复制模型文件到该目录，再使用“校验所选”检查文件完整性。

tiny、base、small 按“ModelScope 国内 CDN → `hf-mirror.com` → Hugging Face 官方源”的顺序自动尝试；Kotoba-Whisper v2.0 Faster 和完整 large-v3 按“`hf-mirror.com` → Hugging Face 官方源”的顺序尝试。所有通道都固定到已核对的模型修订；镜像只作为传输通道，模型来源、文件大小和 SHA-256 仍以官方清单为准。镜像返回不同内容时程序会删除临时文件并尝试下一通道，所有通道均失败才会报错。medium 和 large-v3-turbo 在国内镜像连通性完成验证前仍只使用官方源。

媒体音轨优先由便携版 FFmpeg 标准化；开发构建尚未提供经过供应链审计的 FFmpeg 时，会使用随 faster-whisper 安装的 PyAV 运行库解码常见音视频格式。播放器优先使用 libmpv；便携包中未提供 libmpv 时，自动使用 Qt Multimedia 播放音视频。

GPU 设置可按需下载 NVIDIA 官方 CUDA 12.9、cuBLAS 12.9 和 cuDNN 9.24 绿色运行库，下载量约 1.27 GB，保存到程序旁 `data\gpu-runtime`，不进入 Git 或主 ZIP。下载按“清华 PyPI 镜像 → 阿里云 PyPI 镜像 → 官方 PyPI”的顺序自动回退，所有通道共用固定文件大小和 SHA-256 校验。设置页始终显示 DLL 保存目录的绝对路径，失败时列出手动放置目录和必需文件名。来自 NVIDIA 的软件包会保留 NVIDIA Proprietary Software 许可文件。GPU 初始化失败时会显示中文说明，不会静默改用 CPU。

主工具栏保留添加媒体、导入文件夹、批量操作、开始识别和导出字幕。新建/打开项目位于“项目”，批量选择、任务恢复、字幕检查和翻译位于“处理”，模型、GPU 与翻译服务位于“设置”，版本和许可信息位于“帮助”。

“任务队列”和“翻译上下文”可以通过标题栏关闭。关闭后可在顶部“窗口”菜单重新显示，也可使用“恢复默认布局”同时恢复左右两个面板。

## 翻译、隐私与联网

字幕被当作不可信数据，和系统翻译指令分开传递。返回结果必须覆盖完全一致的稳定字幕 ID；缺失、重复和未知 ID 会导致批次缩小并重试。

使用远程翻译服务时，字幕、提示词、标题、背景、人物关系和术语可能发送给界面显示的服务商。关闭翻译时不会发送这些数据。离线模式下，程序禁止远程 HTTP 请求；已下载的本地模型仍可使用。

在 GUI 的“翻译服务设置”中可选择 OpenAI、DeepSeek、智谱、Ollama、LM Studio 或自定义 OpenAI-compatible 服务。设置页可以输入一句测试内容并真实调用接口，成功时弹窗显示模型输出，失败时显示 HTTP 状态和服务返回原因。DeepSeek 默认使用 `https://api.deepseek.com` 和 `deepseek-v4-flash`。

媒体加入任务树后不会自动处理。第一步手动选择“转文字”，完成后固定生成原文 SRT，不会自动调用翻译 API；第二步点击“翻译原文 SRT”，软件重新读取 SRT 并生成同名中文 SRT。任务异常时可点击“强制暂停”终止当前模型/API 请求并清空尚未开始的队列。

DeepSeek 字幕翻译默认关闭思考模式并启用 JSON 输出，避免普通翻译批次进行不必要的高强度推理。

API Key 不写入项目、配置或日志，只保存在 Windows 凭据管理器。Token 和费用信息均为估算，实际账单以服务商为准。

## 架构

```text
PySide6 GUI / CLI
        │
        ├── SRT workflow ── 原文 SRT → 中文 SRT
        ├── Internal state ── data/state/*.sqlite3（恢复与缓存）
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

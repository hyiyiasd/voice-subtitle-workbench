# 第三方组件说明

本文件不是法律意见。正式 Release 必须根据实际锁定版本生成完整清单。

| 组件 | 用途 | 许可证/要求 |
|---|---|---|
| Python | 运行时 | PSF License |
| PySide6 / Qt | GUI | LGPLv3/GPLv3 或商业许可；本项目采用可替换的动态库分发方式 |
| PyInstaller | Windows 打包 | GPLv2-or-later with bootloader exception |
| FFmpeg | 音频预处理 | 只接受未启用 GPL/nonfree 的固定 LGPL 构建，并提供对应源码与构建参数 |
| mpv/libmpv | 播放 | 只接受以 `-Dgpl=false` 构建的 LGPL 版本 |
| httpx | HTTP | BSD-3-Clause |
| keyring | Windows 凭据 | MIT |
| ReazonSpeech | ASR | Apache-2.0；模型以具体模型卡为准 |
| faster-whisper | ASR | MIT；模型以具体模型卡为准 |
| CTranslate2 | 推理 | MIT |
| Silero VAD | VAD | MIT |

发布工作流必须从锁文件和供应清单生成 SBOM，并把各许可证全文复制到产物的 `licenses` 目录。


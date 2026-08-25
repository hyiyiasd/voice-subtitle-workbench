from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import __version__
from .credentials import CredentialStore
from .domain import ProjectSettings, Segment
from .model_manager import ModelManager
from .paths import (
    AppPaths,
    PortableDirectoryError,
    bundled_resource,
    configure_library_environment,
)
from .pipeline import PipelineCoordinator
from .project import Project
from .providers.openai_compatible import OpenAICompatibleProvider, ProviderConfig
from .quality import apply_quality_flags
from .subtitles import ExportContent, ExportFormat, export_subtitles
from .transcription import TranscriptionService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice-subtitle-translator", description="语音转字幕")
    parser.add_argument("--version", action="version", version=__version__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--translate", action="store_true", help="本次调用启用翻译")
    mode.add_argument("--no-translate", action="store_true", help="本次调用不翻译（默认）")
    parser.add_argument("--offline", action="store_true", help="禁止联网")
    parser.add_argument("--resume", action="store_true", help="恢复未完成任务")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="新建项目")
    create.add_argument("project", type=Path)
    create.add_argument("--media", type=Path)
    create.add_argument("--source-language", default="ja")
    create.add_argument("--target-language", default="zh-Hans")

    info = commands.add_parser("info", help="显示项目信息")
    info.add_argument("project", type=Path)

    add = commands.add_parser("add-segment", help="手工加入一条字幕")
    add.add_argument("project", type=Path)
    add.add_argument("--start-ms", type=int, required=True)
    add.add_argument("--end-ms", type=int, required=True)
    add.add_argument("--text", required=True)
    add.add_argument("--language", default="ja")

    export = commands.add_parser("export", help="导出字幕")
    export.add_argument("project", type=Path)
    export.add_argument("output", type=Path)
    export.add_argument("--format", choices=[item.value for item in ExportFormat], default="srt")
    export.add_argument(
        "--content", choices=[item.value for item in ExportContent], default="source"
    )

    check = commands.add_parser("check", help="检查字幕质量")
    check.add_argument("project", type=Path)

    translate = commands.add_parser("translate", help="翻译未完成或已失效的字幕")
    translate.add_argument("project", type=Path)
    translate.add_argument("--base-url", required=True)
    translate.add_argument("--model", required=True)
    translate.add_argument("--provider", default="openai-compatible")
    translate.add_argument("--prompt", default="")
    translate.add_argument("--batch-size", type=int, default=20)

    key = commands.add_parser("set-api-key", help="保存 Provider API Key 到 Windows 凭据管理器")
    key.add_argument("provider")

    models = commands.add_parser("models", help="列出、下载、校验或删除模型")
    models.add_argument("action", choices=["list", "download", "verify", "delete"])
    models.add_argument("model_id", nargs="?")
    models.add_argument("--yes", action="store_true", help="确认删除")

    transcribe = commands.add_parser("transcribe", help="对项目媒体执行 VAD 和语音识别")
    transcribe.add_argument("project", type=Path)
    transcribe.add_argument("--model", default="reazonspeech-k2-ja")
    transcribe.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    transcribe.add_argument("--compute-type", default=None)
    transcribe.add_argument("--ffmpeg", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = AppPaths.discover()
    try:
        paths.ensure()
    except PortableDirectoryError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    configure_library_environment(paths, offline=args.offline)
    try:
        return _dispatch(args, paths)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace, paths: AppPaths) -> int:
    if args.command == "create":
        settings = ProjectSettings(
            translation_enabled=bool(args.translate),
            source_language=args.source_language,
            target_language=args.target_language,
            offline=bool(args.offline),
        )
        with Project.create(args.project, settings) as project:
            if args.media:
                project.set_media(args.media)
            print(project.path)
        return 0
    if args.command == "set-api-key":
        key = getpass.getpass(f"请输入 {args.provider} API Key：")
        CredentialStore().set(args.provider, key)
        print("已保存到 Windows 凭据管理器。")
        return 0
    if args.command == "models":
        manager = ModelManager(paths, bundled_resource("models/manifest.json"))
        if args.action == "list":
            values = [
                {
                    "id": model.descriptor.id,
                    "name": model.descriptor.display_name,
                    "size_bytes": model.descriptor.size_bytes,
                    "license": model.descriptor.license,
                    "vram_mb": model.descriptor.recommended_vram_mb,
                    "downloadable": model.downloadable,
                    "installed": manager.is_installed(model.descriptor.id),
                    "note": model.note,
                }
                for model in manager.models.values()
            ]
            print(json.dumps(values, ensure_ascii=False, indent=2))
            return 0
        if not args.model_id:
            raise ValueError(f"models {args.action} 需要 model_id。")
        if args.action == "download":
            manager.download(args.model_id, offline=bool(args.offline))
            print(f"已安装并校验：{args.model_id}")
        elif args.action == "verify":
            manager.verify(args.model_id)
            print(f"校验通过：{args.model_id}")
        elif args.action == "delete":
            if not args.yes:
                raise ValueError("删除模型需要明确添加 --yes。")
            manager.delete(args.model_id)
            print(f"已删除模型：{args.model_id}")
        return 0
    with Project.open(args.project) as project:
        if args.command == "info":
            settings = project.get_settings()
            print(
                json.dumps(
                    {
                        "project": str(project.path),
                        "media": str(project.resolve_media() or ""),
                        "segments": len(project.list_segments()),
                        "pipeline_mode": settings.pipeline_mode.value,
                        "offline": settings.offline,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "add-segment":
            order_key = len(project.list_segments())
            project.add_segment(
                Segment(
                    order_key=order_key,
                    start_ms=args.start_ms,
                    end_ms=args.end_ms,
                    source_text=args.text,
                    language=args.language,
                    human_locked=True,
                ),
                reason="cli_manual",
            )
        elif args.command == "export":
            export_subtitles(
                project.list_segments(),
                args.output,
                output_format=ExportFormat(args.format),
                content=ExportContent(args.content),
            )
            print(args.output.resolve())
        elif args.command == "check":
            segments = project.list_segments()
            apply_quality_flags(segments, project.get_settings().subtitle)
            print(
                json.dumps(
                    {segment.id: sorted(segment.quality_flags) for segment in segments},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "translate":
            if not args.translate:
                raise RuntimeError("CLI 默认不翻译；请在子命令前明确添加 --translate。")
            settings = project.get_settings()
            settings.translation_enabled = True
            settings.translation_provider = args.provider
            settings.translation_model = args.model
            settings.offline = bool(args.offline)
            project.save_settings(settings)
            is_local = args.base_url.startswith(("http://127.0.0.1", "http://localhost"))
            api_key = "" if is_local else (CredentialStore().get(args.provider) or "")
            if not is_local and not api_key:
                raise RuntimeError(f"未在 Windows 凭据管理器中找到 {args.provider} API Key。")
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    id=args.provider,
                    base_url=args.base_url,
                    api_key=api_key,
                    offline=bool(args.offline),
                )
            )
            try:
                result = PipelineCoordinator(project).translate_pending(
                    provider,
                    prompt=args.prompt,
                    batch_size=args.batch_size,
                )
            finally:
                provider.close()
            print(
                json.dumps(
                    {
                        "task_id": result.task_id,
                        "completed": result.completed,
                        "cached": result.cached,
                        "stopped_by_switch": result.stopped_by_switch,
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "transcribe":
            manager = ModelManager(paths, bundled_resource("models/manifest.json"))
            ffmpeg = args.ffmpeg or (paths.root / "runtime" / "ffmpeg.exe")
            task_id = TranscriptionService(
                project,
                paths=paths,
                model_manager=manager,
                ffmpeg_path=ffmpeg,
            ).run(
                model_id=args.model,
                device=args.device,
                compute_type=args.compute_type,
            )
            print(json.dumps({"task_id": task_id, "segments": len(project.list_segments())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

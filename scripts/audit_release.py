from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    vendor = json.loads((root / "vendor" / "manifest.json").read_text(encoding="utf-8"))
    models = json.loads((root / "models" / "manifest.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    if not vendor.get("release_ready"):
        errors.append("vendor/manifest.json 尚未标记 release_ready")
    for component in vendor["components"]:
        for field in ("version", "source_url", "binary_url", "sha256"):
            if not component.get(field) or component[field] == "UNPINNED":
                errors.append(f"{component['id']} 缺少固定字段：{field}")
    for model in models["models"]:
        if model.get("downloadable"):
            if not model.get("artifacts"):
                errors.append(f"可下载模型没有文件清单：{model['id']}")
            for artifact in model.get("artifacts", []):
                if not artifact.get("sha256") or not artifact.get("size_bytes"):
                    errors.append(f"模型文件缺少校验信息：{model['id']}")
    if errors:
        print("Release 许可/供应链审计失败：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Release 供应清单审计通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


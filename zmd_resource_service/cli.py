from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Callable, TypeVar

from .manifest import package_bytes, sanitize_manifest, pack_filename
from .privacy import public_summary, redact_text
from .pipeline import Pipeline

EXPORT_STAGES = ("masterdata", "textures", "audio", "video", "raw")
T = TypeVar("T")


def send_bark_notification(title: str, body: str) -> bool:
    configured = os.environ.get("ZMD_BARK_NOTIFY_SCRIPT")
    helper = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".codex/skills/bark-skill/scripts/notify.py"
    )
    if not helper.is_file():
        print(f"warning: Bark helper was not found: {helper}", file=sys.stderr)
        return False

    result = subprocess.run(
        ["python3", str(helper), "--title", title, "--body", body],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        print("warning: Bark notification delivery failed", file=sys.stderr)
        return False
    return True


def run_download_task(name: str, version: str, operation: Callable[[], T]) -> T:
    try:
        result = operation()
    except Exception:
        send_bark_notification(
            "ZMD 下载任务失败",
            f"{name}（{version}）执行报错，请查看任务日志。",
        )
        raise

    send_bark_notification(
        "ZMD 下载任务完成",
        f"{name}（{version}）已完成。",
    )
    return result


def human_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="endfield-resource")
    result.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("ZMD_DATA_ROOT", "/Volumes/wd/ZmdResourceService")),
    )
    result.add_argument("--workers", type=int, default=2)
    result.add_argument(
        "--patch-memory-mb",
        type=int,
        default=int(os.environ.get("ZMD_PATCH_MEMORY_MB", "1536")),
    )
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="fetch and display the current official full-package plan")
    sub.add_parser("download", help="download and MD5-verify every package volume")
    sub.add_parser("extract-vfs", help="extract only StreamingAssets from verified packages")
    sub.add_parser("bootstrap", help="download, verify, and extract StreamingAssets")
    sub.add_parser("resource-plan", help="plan direct CDN resource patches without downloading")
    sub.add_parser("resource-sync", help="download, apply, and immediately delete CDN patches")
    export = sub.add_parser("export", help="run EndfieldStudio exporters in Docker")
    export.add_argument("--version")
    export.add_argument(
        "--stages",
        nargs="+",
        choices=EXPORT_STAGES,
        default=list(EXPORT_STAGES),
        help="export selected stages; defaults to all in visible-output-first order",
    )
    sub.add_parser("mark-export", help="verify export sections and write an inventory")
    stream = sub.add_parser(
        "stream-export",
        help="export one verified VFS chunk at a time, atomically commit it, then remove it",
    )
    stream.add_argument("--version")
    return result


def run_export(project_root: Path, data_root: Path, version: str, stages: list[str]) -> None:
    env = dict(os.environ)
    env["ZMD_DATA_ROOT"] = str(data_root)
    env["ZMD_VERSION"] = version
    env["ZMD_EXPORT_STAGES"] = " ".join(stages)
    subprocess.run(
        ["docker", "compose", "run", "--rm", "exporter", "/app/scripts/export-all.sh"],
        cwd=project_root,
        env=env,
        check=True,
    )


def run_stream_export(project_root: Path, data_root: Path, version: str) -> None:
    env = dict(os.environ)
    env["ZMD_DATA_ROOT"] = str(data_root)
    env["ZMD_VERSION"] = version
    subprocess.run(
        ["docker", "compose", "run", "--rm", "exporter", "/app/scripts/stream-export.sh"],
        cwd=project_root,
        env=env,
        check=True,
    )


def _main() -> int:
    args = parser().parse_args()
    if args.workers < 1 or args.workers > 8:
        raise SystemExit("--workers must be between 1 and 8")
    pipeline = Pipeline(args.data_root, args.workers)
    payload = pipeline.latest()
    version = str(payload["version"])
    if args.command == "plan":
        clean = sanitize_manifest(payload)
        print(
            json.dumps(
                {
                    "version": version,
                    "package_count": len(clean["pkg"]["packs"]),
                    "download_bytes": package_bytes(clean),
                    "download_size": human_bytes(package_bytes(clean)),
                    "installed_bytes": int(clean["pkg"]["total_size"]),
                    "installed_size": human_bytes(int(clean["pkg"]["total_size"])),
                    "first_package": pack_filename(clean["pkg"]["packs"][0]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "download":
        print(
            run_download_task(
                "完整基包下载与校验",
                version,
                lambda: pipeline.download(payload),
            )
        )
    elif args.command == "extract-vfs":
        print(pipeline.extract_vfs(payload))
    elif args.command == "bootstrap":
        def bootstrap() -> Path:
            pipeline.download(payload)
            return pipeline.extract_vfs(payload)

        print(run_download_task("基包下载与 VFS 初始化", version, bootstrap))
    elif args.command == "resource-plan":
        plan = pipeline.resource_plan(payload)
        plan = {
            key: value
            for key, value in plan.items()
            if key not in {"actions", "full_actions"}
        }
        plan["patch_size"] = human_bytes(plan["patch_bytes"])
        plan["full_size"] = human_bytes(plan["full_bytes"])
        plan["estimated_peak"] = human_bytes(plan["estimated_peak_bytes"])
        print(json.dumps(public_summary(plan), ensure_ascii=False, indent=2))
    elif args.command == "resource-sync":
        if args.patch_memory_mb < 512:
            raise SystemExit("--patch-memory-mb must be at least 512")
        result = run_download_task(
            "CDN 增量资源同步",
            version,
            lambda: pipeline.sync_resources(payload, args.patch_memory_mb),
        )
        result = {
            key: value
            for key, value in result.items()
            if key not in {"actions", "full_actions"}
        }
        print(json.dumps(public_summary(result), ensure_ascii=False, indent=2))
    elif args.command == "export":
        export_version = args.version or version
        run_export(
            Path(__file__).resolve().parents[1],
            args.data_root.resolve(),
            export_version,
            args.stages,
        )
        if set(args.stages) == set(EXPORT_STAGES):
            print(pipeline.write_export_marker(export_version))
        else:
            print(
                json.dumps(
                    {
                        "version": export_version,
                        "completed_stages": args.stages,
                        "export_root": str(args.data_root.resolve()),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
    elif args.command == "mark-export":
        print(pipeline.write_export_marker(version))
    elif args.command == "stream-export":
        export_version = args.version or version

        def stream_export() -> Path:
            run_stream_export(
                Path(__file__).resolve().parents[1],
                args.data_root.resolve(),
                export_version,
            )
            marker = pipeline.write_export_marker(export_version)
            shutil.rmtree(pipeline.work)
            return marker

        print(run_download_task("逐块解包与原子导出", export_version, stream_export))
    return 0


def main() -> int:
    try:
        return _main()
    except Exception:
        print(redact_text(traceback.format_exc()), file=sys.stderr, end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

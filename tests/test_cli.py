from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from zmd_resource_service import cli


def test_send_bark_notification_uses_configured_helper(tmp_path: Path, monkeypatch) -> None:
    helper = tmp_path / "notify.py"
    helper.touch()
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("ZMD_BARK_NOTIFY_SCRIPT", str(helper))
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.send_bark_notification("完成", "正文") is True
    assert calls == [["python3", str(helper), "--title", "完成", "--body", "正文"]]


def test_run_download_task_notifies_success(monkeypatch) -> None:
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli,
        "send_bark_notification",
        lambda title, body: notices.append((title, body)) or True,
    )

    assert cli.run_download_task("资源同步", "1.4.4", lambda: 42) == 42
    assert notices == [("ZMD 下载任务完成", "资源同步（1.4.4）已完成。")]


def test_run_download_task_notifies_failure(monkeypatch) -> None:
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli,
        "send_bark_notification",
        lambda title, body: notices.append((title, body)) or True,
    )

    def fail() -> None:
        raise RuntimeError("signed URL must not appear in Bark")

    with pytest.raises(RuntimeError):
        cli.run_download_task("资源同步", "1.4.4", fail)

    assert notices == [
        ("ZMD 下载任务失败", "资源同步（1.4.4）执行报错，请查看任务日志。")
    ]

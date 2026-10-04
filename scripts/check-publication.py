#!/usr/bin/env python3
"""Check the Git publication set without reading runtime data."""
from pathlib import Path
import json
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, check=True,
    )
    paths = sorted(set(result.stdout.decode().split("\0")) - {""})
    private_values = []
    private = ROOT / "config/service.json"
    if private.is_file():
        settings = json.loads(private.read_text())
        private_values = [settings.get(key, "") for key in ("launcherUrl", "appCode")]
    forbidden = re.compile(r"https?://[^\s\"'<>]*(?:hycdn\.|[?&]auth_key=)(?!example)", re.I)
    failures = []
    for name in paths:
        path = ROOT / name
        parts = Path(name).parts
        if (name == ".env" or name.startswith(".env.") and name != ".env.example"
            or name.startswith("config/") and name != "config/service.example.json"
            or parts[0] in {"data", "state", ".work", ".baseline", ".tools"}
            or ".bak" in path.name):
            failures.append((name, "private or generated file"))
            continue
        if not path.is_file():
            continue
        content = path.read_bytes()
        if any(value and value.encode() in content for value in private_values):
            failures.append((name, "private source configuration"))
        if b"\0" not in content:
            text = content.decode("utf-8", errors="replace")
            # Synthetic fixture URLs use reserved example domains or dummy hosts.
            for match in forbidden.finditer(text):
                url = match.group()
                if ".example/" not in url and "example.com/" not in url:
                    failures.append((name, "resource URL"))
                    break
    for name, reason in failures:
        print(f"FAIL {name}: {reason}")
    print(f"Publication check: {len(paths)} files, {len(failures)} failures")
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())

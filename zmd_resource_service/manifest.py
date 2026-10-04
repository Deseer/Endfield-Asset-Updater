from __future__ import annotations

import copy
import json
import os
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .network import open_with_retry
from .config import source_config


def fetch_latest(version: str = "0.0.0", timeout: int = 30) -> dict[str, Any]:
    config = source_config()
    query = urllib.parse.urlencode(
        {
            "version": version,
            "appcode": config["appCode"],
            "channel": config["channel"],
            "sub_channel": config["subChannel"],
        }
    )
    request = urllib.request.Request(
        f"{config['launcherUrl']}?{query}",
        headers={"User-Agent": "endfield-resource-service/0.1"},
    )
    with open_with_retry(request, timeout=timeout) as response:
        payload = json.load(response)
    validate_manifest(payload)
    return payload


def validate_manifest(payload: dict[str, Any]) -> None:
    pkg = payload.get("pkg")
    if not isinstance(pkg, dict) or not isinstance(pkg.get("packs"), list):
        raise ValueError("launcher response does not contain a full package manifest")
    if not payload.get("version"):
        raise ValueError("launcher response does not contain a version")
    for index, pack in enumerate(pkg["packs"]):
        missing = {"url", "md5", "package_size"} - set(pack)
        if missing:
            raise ValueError(f"pack {index} is missing fields: {sorted(missing)}")


def pack_filename(pack: dict[str, Any]) -> str:
    name = Path(urllib.parse.urlsplit(pack["url"]).path).name
    if not name or name in {".", ".."}:
        raise ValueError("invalid package URL")
    return name


def package_bytes(payload: dict[str, Any]) -> int:
    return sum(int(item["package_size"]) for item in payload["pkg"]["packs"])


def sanitize_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(payload)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if key.endswith("url") and isinstance(child, str):
                    parts = urllib.parse.urlsplit(child)
                    value[key] = urllib.parse.urlunsplit(
                        (parts.scheme, parts.netloc, parts.path, "", "")
                    )
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(result)
    return result


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path


def source_config() -> dict[str, str]:
    path = Path(os.environ.get("ENDFIELD_SERVICE_CONFIG_PATH", "config/service.json")).expanduser()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Set ENDFIELD_SERVICE_CONFIG_PATH to a valid private service JSON") from None
    if not isinstance(value, dict):
        raise ValueError("Private service config must be a JSON object")
    for key in ("launcherUrl", "appCode", "channel", "subChannel"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"Private service config requires {key}")
    parts = urllib.parse.urlsplit(value["launcherUrl"])
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment or parts.username:
        raise ValueError("launcherUrl must be an HTTPS endpoint without credentials or query")
    return value

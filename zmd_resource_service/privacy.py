from __future__ import annotations

import re
from typing import Any

_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def redact_text(value: str) -> str:
    return _URL.sub("[redacted-url]", value)


def public_summary(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: public_summary(child) for key, child in value.items()}
    if isinstance(value, list):
        return [public_summary(child) for child in value]
    return redact_text(value) if isinstance(value, str) else value

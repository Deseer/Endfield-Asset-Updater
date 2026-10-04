from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any


def open_with_retry(
    request: urllib.request.Request,
    timeout: int,
    retries: int = 5,
) -> Any:
    for attempt in range(1, retries + 1):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except (OSError, urllib.error.URLError):
            if attempt == retries:
                raise
            time.sleep(min(2**attempt, 10))
    raise AssertionError("unreachable")

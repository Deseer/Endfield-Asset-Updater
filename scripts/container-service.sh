#!/bin/bash
set -euo pipefail

exec python3 -u -m zmd_resource_service.watcher

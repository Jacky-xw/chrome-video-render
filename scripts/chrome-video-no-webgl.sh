#!/usr/bin/env bash
# 通过 CVR 的 Android 兼容配置启动视频 Chrome。
set -euo pipefail
CVR_SCRIPTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$CVR_SCRIPTS/chrome-android.sh" --disable-webgl --disable-webgl2 "$@"

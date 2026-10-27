#!/usr/bin/env bash
# 在独立 mount namespace 准备共享内存并启动 Chrome。
set -euo pipefail
CVR_SCRIPTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REAL_BROWSER="${REAL_BROWSER:-${VIDEO_CHROME_REAL:-}}"
if [ -z "$REAL_BROWSER" ]; then
  source "$CVR_SCRIPTS/video-browser-env.sh"
fi
if [ ! -x "${REAL_BROWSER:-/nonexistent}" ]; then
  echo "Chrome not found or not executable: ${REAL_BROWSER:-unset}" >&2
  exit 1
fi
SHM_SOURCE="${SHM_SOURCE:-${TMPDIR:-/tmp}/cvr-chrome-shm}"
if [ -e /dev/shm ] && [ ! -d /dev/shm ]; then
  echo "/dev/shm exists but is not a directory" >&2
  exit 1
fi
mkdir -p "$SHM_SOURCE"
chmod 1777 "$SHM_SOURCE"
export REAL_BROWSER SHM_SOURCE
exec unshare -m bash -c '
  set -e
  mount --bind "$SHM_SOURCE" /dev/shm
  chmod 1777 /dev/shm
  test -d /dev/shm && test -w /dev/shm && test -x /dev/shm
  exec "$REAL_BROWSER" "$@"
' -- "$@"

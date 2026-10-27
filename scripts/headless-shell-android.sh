#!/usr/bin/env bash
# 在独立 mount namespace 准备共享内存并启动 Headless Shell。
set -euo pipefail
CVR_SCRIPTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HEADLESS_SHELL="${HEADLESS_SHELL:-${VIDEO_HEADLESS_REAL:-}}"
if [ -z "$HEADLESS_SHELL" ]; then
  source "$CVR_SCRIPTS/video-browser-env.sh"
  HEADLESS_SHELL="$VIDEO_HEADLESS_REAL"
fi
if [ ! -x "${HEADLESS_SHELL:-/nonexistent}" ]; then
  echo "Headless Shell not found or not executable: ${HEADLESS_SHELL:-unset}" >&2
  exit 1
fi
SHM_SOURCE="${SHM_SOURCE:-${TMPDIR:-/tmp}/cvr-headless-shm}"
if [ -e /dev/shm ] && [ ! -d /dev/shm ]; then
  echo "/dev/shm exists but is not a directory" >&2
  exit 1
fi
mkdir -p "$SHM_SOURCE"
chmod 1777 "$SHM_SOURCE"
export HEADLESS_SHELL SHM_SOURCE
exec unshare -m bash -c '
  set -e
  mount --bind "$SHM_SOURCE" /dev/shm
  chmod 1777 /dev/shm
  test -d /dev/shm && test -w /dev/shm && test -x /dev/shm
  exec "$HEADLESS_SHELL" "$@"
' -- "$@"

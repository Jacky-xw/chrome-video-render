# 导出当前安装目录的浏览器启动器，并发现可用浏览器。
CVR_SCRIPTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export CVR_SCRIPTS
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-${CVR_BROWSER_ROOT:-$HOME/.cache/ms-playwright}}"
if [ -z "${VIDEO_CHROME_REAL:-}" ] || [ ! -x "${VIDEO_CHROME_REAL:-/nonexistent}" ]; then
  VIDEO_CHROME_REAL="$(find "$PLAYWRIGHT_BROWSERS_PATH" -maxdepth 4 -path '*/chromium-*/chrome-linux*/chrome' -type f -print 2>/dev/null | sort -t- -k2,2nr | head -n 1)"
fi
if [ -z "${VIDEO_HEADLESS_REAL:-}" ] || [ ! -x "${VIDEO_HEADLESS_REAL:-/nonexistent}" ]; then
  VIDEO_HEADLESS_REAL="$(find "$PLAYWRIGHT_BROWSERS_PATH" -maxdepth 4 -path '*/chromium_headless_shell-*/chrome-linux*/headless_shell' -type f -print 2>/dev/null | sort -t- -k2,2nr | head -n 1)"
fi
export VIDEO_CHROME_REAL VIDEO_HEADLESS_REAL
export VIDEO_CHROME_LAUNCHER="$CVR_SCRIPTS/chrome-video-no-webgl.sh"
export VIDEO_HEADLESS_LAUNCHER="$CVR_SCRIPTS/headless-shell-no-webgl.sh"
export REAL_BROWSER="$VIDEO_CHROME_REAL"
export HEADLESS_SHELL="$VIDEO_HEADLESS_REAL"
export HEADLESS_SHELL_LAUNCHER="$VIDEO_HEADLESS_LAUNCHER"
export CHROME_BIN="$VIDEO_HEADLESS_LAUNCHER"
export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH="$VIDEO_HEADLESS_LAUNCHER"
export HYPERFRAMES_BROWSER_PATH="$VIDEO_HEADLESS_LAUNCHER"
export REMOTION_BROWSER_EXECUTABLE_PATH="$VIDEO_CHROME_LAUNCHER"
export FFMPEG_PATH="${FFMPEG_PATH:-$(command -v ffmpeg || true)}"
export FFPROBE_PATH="${FFPROBE_PATH:-$(command -v ffprobe || true)}"

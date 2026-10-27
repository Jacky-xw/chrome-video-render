#!/usr/bin/env bash
# 输出浏览器、视频引擎和硬件环境的 JSON 或文本报告。
set -euo pipefail
JSON=0
[ "${1:-}" = "--json" ] && JSON=1
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 - "$JSON" "$SCRIPT_DIR" <<'PY'
import json, os, platform, shutil, sys, importlib.util, glob, subprocess

def subprocess_cache():
    """读取 npm 缓存目录；优先读配置文件和默认值，避免为一次查询启动 npm 进程。"""
    for candidate in ('npm_config_cache',):
        value = os.environ.get(candidate)
        if value:
            return value
    try:
        for line in open(os.path.join(os.path.expanduser('~'), '.npmrc'), encoding='utf-8'):
            key, _, value = line.partition('=')
            if key.strip() == 'cache' and value.strip():
                return os.path.expanduser(value.strip())
    except OSError:
        pass
    # npm 的默认缓存目录；存在就不必再调用 npm。
    default = os.path.join(os.path.expanduser('~'), '.npm')
    if os.path.isdir(default):
        return default
    try:
        return subprocess.run(['npm', 'config', 'get', 'cache'], capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return default
# Rootfs 会过滤模块搜索路径，按当前脚本的真实位置直接加载。
spec = importlib.util.spec_from_file_location('cvr_runtime', os.path.join(sys.argv[2], 'cvr_runtime.py'))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
discover_browsers = runtime.discover_browsers
hardware_report = runtime.hardware_report

as_json = sys.argv[1] == '1'
chrome, shell = discover_browsers()
hardware = hardware_report()
shm = os.path.isdir('/dev/shm')
ffmpeg = shutil.which('ffmpeg')
ffprobe = shutil.which('ffprobe')

def package_version(directory):
    manifest = os.path.join(directory, 'remotion', 'package.json')
    try:
        with open(manifest) as f:
            return json.load(f)['version']
    except (OSError, ValueError, KeyError):
        return None

project = os.environ.get('CVR_PROJECT', os.getcwd())
remotion_root = os.path.join(project, 'node_modules')
if not os.path.isfile(os.path.join(remotion_root, '.bin', 'remotion')):
    # 只报告目标项目实际安装，不借用其他项目的依赖。
    remotion_root = None
if remotion_root is None:
    remotion_root = os.path.join(project, 'node_modules')
remotion = os.path.join(remotion_root, '.bin', 'remotion')
remotion_ok = os.path.isfile(remotion) and os.access(remotion, os.X_OK)
hf_package = shutil.which('hyperframes')
hf_candidates = [os.path.join(project, 'node_modules', 'hyperframes', 'package.json')]
# 同时发现项目依赖和已有 npx 缓存。
npm_cache = os.environ.get('npm_config_cache') or subprocess_cache()
hf_candidates += glob.glob(os.path.join(npm_cache, '_npx', '*', 'node_modules', 'hyperframes', 'package.json'))
def version_key(value):
    """按数字段比较版本；预发布后缀不用于排序，也不混比字符串和整数。"""
    import re
    return tuple(int(x) for x in re.findall(r'\d+', value.split('-', 1)[0]))

hf_version = None
hf_versions = []
for manifest in hf_candidates:
    if not os.path.isfile(manifest):
        continue
    try:
        with open(manifest) as f:
            candidate = json.load(f)
        if candidate.get('bin', {}).get('hyperframes'):
            hf_versions.append((version_key(candidate['version']), candidate['version']))
    except (OSError, ValueError, KeyError):
        pass
if hf_versions:
    hf_version = max(hf_versions)[1]
hyperframes_ok = bool(hf_package or hf_version)

chrome_launcher = os.path.join(sys.argv[2], 'chrome-video-no-webgl.sh')
shell_launcher = os.path.join(sys.argv[2], 'headless-shell-no-webgl.sh')
browser = chrome or shell
result = {
    'ok': bool(browser and ffmpeg and ffprobe),
    'arch': platform.machine(),
    'os': platform.system(),
    'chrome': chrome,
    'chromium': shell,
    'default_browser': browser,
    'policy': 'chrome-first; headless-shell-only when engine requires it',
    'rendering_policy': {'browser_launchers': 'CVR Android mount namespace', 'backend_audit': 'on demand'},
    'hardware': {
        **hardware,
        'dev_shm': shm,
        'display': bool(os.environ.get('DISPLAY')),
        'ci': os.environ.get('CI') == 'true',
    },
    'engines': {
        'project': project,
        'remotion': remotion_ok,
        'remotion_path': remotion if remotion_ok else None,
        'remotion_version': package_version(remotion_root) if remotion_ok else None,
        'hyperframes_cli': hyperframes_ok,
        'hyperframes_version': hf_version,
    },
    'recommended': {
        'mode': 'foreground-parallel-segments-with-hardware-budget',
        'worker_budget': 'computed at render time from CPU/cgroup/memory',
        # 不写死每任务上限：并行数由渲染时的物理大核数与实测内存共同决定。
        'max_workers_per_job': 'computed (physical big cores, capped by measured memory)',
        'global_job_lock': True,
        'audio': 'engine PCM chunks; Remotion official combineChunks; AAC once at delivery',
        'background': False,
        'cloud': False,
    },
    'browser_env': {
        'remotion': {
            'BROWSER_EXECUTABLE': chrome_launcher if chrome and os.access(chrome_launcher, os.X_OK) else (browser['path'] if browser else None),
            'CHROME_MODE': 'chrome-for-testing' if chrome else 'headless-shell',
        },
        'hyperframes': {
            'HYPERFRAMES_BROWSER_PATH': shell_launcher if shell and os.access(shell_launcher, os.X_OK) else None,
        },
        'headless_shell': {
            'HEADLESS_SHELL': shell['path'] if shell else None,
            'HEADLESS_SHELL_LAUNCHER': shell_launcher if shell else None,
            'PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH': shell_launcher if shell else None,
        },
    },
    'notes': [
        'headless browser is still foreground execution',
        'HyperFrames requires its supported Headless Shell through the plugin Android launcher; no fake frame-range splitting',
        'graphics backend audit is optional and is not a routine render step',
        'no /dev/shm: Remotion and Headless Shell need their plugin launchers' if not shm else '/dev/shm available; plugin launchers remain supported',
        'environment availability is not a substitute for an end-to-end audio/video sync test',
    ],
}
if as_json:
    print(json.dumps(result, ensure_ascii=False, indent=2))
else:
    print('Chrome:', chrome or 'not found')
    print('Headless Shell:', shell or 'not found')
    print('Recommended:', result['recommended'])
    print('OK:', result['ok'])
PY

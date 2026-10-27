#!/usr/bin/env python3
"""提供 HyperFrames 前台分段渲染、渲染方案选择、硬件并行预算和成片验收。

与 remotion-segmented.py 共享同一套硬件探测与预算逻辑：并行上限来自本机
探测到的物理大核数，调用方显式给出上限时才截断。
"""
import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import importlib.util
_spec = importlib.util.spec_from_file_location("cvr_runtime", Path(__file__).resolve().with_name("cvr_runtime.py"))
_runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runtime)
hardware_report = _runtime.hardware_report
media_info = _runtime.media_info
verify_video = _runtime.verify_video
worker_budget = _runtime.worker_budget

SCRIPTS = Path(__file__).resolve().parent


def run(command, cwd=None, env=None):
    """前台阻塞运行，保留上游 stdout/stderr，便于失败定位和审计。"""
    print("\n+", " ".join(map(str, command)), flush=True)
    subprocess.run([str(x) for x in command], cwd=cwd, env=env, check=True)


def _fmt_ms(value):
    return "-" if value is None else f"{value / 1000:.2f}s"


def print_timings(timings, total_ms):
    """rvc 结束时打印各阶段耗时，直接指出时间花在哪里。"""
    order = [
        ("environment_ms", "环境探测"),
        ("render_ms", "渲染"),
        ("verify_ms", "校验"),
    ]
    parts = []
    for key, label in order:
        if timings.get(key) is not None:
            parts.append(f"{label} {_fmt_ms(timings[key])}")
    print("CVR timing: " + " | ".join(parts) + f" | 合计 {_fmt_ms(total_ms)}", flush=True)


def find_cli(explicit=None):
    """定位 hyperframes 可执行文件：显式路径优先，其次 PATH，最后项目内。"""
    if explicit:
        path = Path(explicit)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise RuntimeError(f"--cli is not executable: {explicit}")
    found = shutil.which("hyperframes")
    if found:
        return found
    local = Path.cwd() / "node_modules" / ".bin" / "hyperframes"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    raise RuntimeError("hyperframes CLI not found; install it or pass --cli")


def load_variables(args):
    """读取声明变量：--variables 与 --variables-file 二选一。"""
    text = Path(args.variables_file).read_text() if args.variables_file else args.variables
    data = json.loads(text) if text else {}
    if not isinstance(data, dict):
        raise RuntimeError("variables must be a JSON object")
    return data


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=".")
    p.add_argument("--output", required=True)
    p.add_argument("--composition", default="index.html")
    p.add_argument("--quality", default="looks", choices=["draft", "looks", "delivery"])
    p.add_argument("--fps", help="可选覆盖：整数或分数帧率，如 30、30000/1001；默认读取 composition")
    p.add_argument("--workers", type=int, help="最大 worker 数，仍受硬件预算限制")
    p.add_argument("--max-workers", type=int, default=None,
                   help="单任务硬上限；省略时按本机物理大核数，不写死固定值")
    p.add_argument("--variables", help="HyperFrames 声明变量的 JSON 对象")
    p.add_argument("--variables-file")
    p.add_argument("--cli", help="已安装的 hyperframes 可执行文件；默认从 PATH 或当前项目 node_modules 查找")
    p.add_argument("--sync-check", action="store_true", help="对当前输出执行闪帧/脉冲同步检查")
    p.add_argument("--reference-pulse", help="校准样片使用的原始 PCM 脉冲")
    p.add_argument("--mode", choices=["quick", "full"], default="quick", help="quick 为 cvr 轻量启动，full 为 rvc 详细启动")
    args = p.parse_args()
    if args.max_workers is not None and not 1 <= args.max_workers <= 32:
        p.error("--max-workers must be 1..32")
    if args.workers is not None and args.workers < 1:
        p.error("--workers must be positive")
    if args.variables and args.variables_file:
        p.error("--variables and --variables-file are mutually exclusive")
    if args.fps:
        _runtime.parse_fraction(args.fps)

    started = time.monotonic()
    project = Path(args.project).resolve()
    output = Path(args.output).resolve()
    if not project.is_dir():
        p.error("project does not exist")
    cli = find_cli(args.cli)

    workstation = hardware_report()
    if args.mode == "full":
        # 带上 project，使 worker_budget 能读到本机实测的内存系数；否则 HyperFrames
        # 与 Remotion 两条路径会按两套不同的口径决定并行度。
        budget = worker_budget({**workstation, "project": str(project)}, engine="hyperframes",
                               requested=args.workers, maximum=args.max_workers)
        print(f"CVR hardware budget: {budget['workers']} worker(s) "
              f"({budget['memory_basis']}, cpu_limit={budget['cpu_limit']}, "
              f"memory_limit={budget['memory_limit']})", flush=True)
    else:
        # quick 默认单 worker；显式传 --workers 时按请求执行，不静默忽略。
        budget = {"workers": max(1, args.workers or 1)}

    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = project / ".cvr-hyperframes.foreground.lock"
    lock_handle = open(lock_path, "a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        raise RuntimeError(f"Another CVR job holds {lock_path}; wait for it instead of stacking parallel workers")
    try:
        command = [cli, "render", str(project / args.composition),
                   "--output", str(output), "--quality", args.quality,
                   "--workers", str(budget["workers"])]
        if args.fps:
            command += ["--fps", args.fps]
        variables = load_variables(args)
        if variables:
            command += ["--variables", json.dumps(variables, ensure_ascii=False)]
        render_started = time.monotonic()
        run(command, cwd=project, env=os.environ.copy())
        render_ms = int((time.monotonic() - render_started) * 1000)
        verify_started = time.monotonic()
        verified = verify_video(output, {"require_audio": False}, decode=args.mode == "full")
        verify_ms = int((time.monotonic() - verify_started) * 1000)
        sync_report = {"status": "not-measured"}
        if args.sync_check:
            sync_command = [sys.executable, str(SCRIPTS / "check-av-sync.py"), str(output), "--json"]
            if args.reference_pulse:
                sync_command += ["--reference-pulse", args.reference_pulse]
            result = subprocess.run(sync_command, capture_output=True, text=True, check=True)
            sync_report = json.loads(result.stdout)
            sync_report["status"] = "passed"
        timings = {"render_ms": render_ms, "verify_ms": verify_ms}
        total_ms = int((time.monotonic() - started) * 1000)
        report = {
            "ok": True,
            "mode": args.mode,
            "output": str(output),
            "composition": args.composition,
            "quality": args.quality,
            "timings": {**timings, "total_ms": total_ms},
            "budget": budget,
            "hardware_at_start": workstation,
            "verification": {"container_and_streams": "passed",
                             "full_decode": "passed" if args.mode == "full" else "skipped",
                             "decoded_frames": verified.get("decoded_frames"),
                             "sync": sync_report},
        }
        (output.parent / (output.stem + ".render-report.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if args.mode == "full":
            print_timings(timings, total_ms)
        print("RENDER_OK " + json.dumps({"ok": True, "mode": args.mode, "output": str(output),
                                         "duration": verified.get("duration"), "sync": sync_report}, ensure_ascii=False), flush=True)
    finally:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print("FOREGROUND_RENDER_FAILED command exit", exc.returncode, file=sys.stderr)
        sys.exit(exc.returncode or 1)
    except Exception as exc:
        print("FOREGROUND_RENDER_FAILED", exc, file=sys.stderr)
        sys.exit(1)

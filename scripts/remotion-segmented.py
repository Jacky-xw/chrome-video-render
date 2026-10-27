#!/usr/bin/env python3
"""提供 Remotion 前台分段渲染、渲染方案选择、硬件并行预算和成片验收。"""
import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Rootfs 会过滤 sys.path，按真实文件位置直接加载公共模块。
import importlib.util
_spec = importlib.util.spec_from_file_location("cvr_runtime", Path(__file__).resolve().with_name("cvr_runtime.py"))
_runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runtime)
hardware_report = _runtime.hardware_report
media_info = _runtime.media_info
verify_video = _runtime.verify_video
worker_budget = _runtime.worker_budget

SCRIPTS = Path(__file__).resolve().parent
# 探针分段的帧数下限。Remotion 分块音频要求至少 5 帧，但段太小时单段开销会
# 盖过并行收益，30 帧是一个保守下限。帧数不足以切出所需段数时，探针能测到
# 的并行度就到这个上限为止；PROBE_FRAMES 已按常见大核数选够。
PROBE_MIN_SEGMENT_FRAMES = 30
PROBE_SHORT_COMPOSITION = "CvrProbeShort"
PROBE_LONG_COMPOSITION = "CvrProbeLong"


def run(command, cwd=None, env=None):
    """前台阻塞运行，保留上游 stdout/stderr，便于失败定位和审计。

    不捕获输出：渲染进度要实时显示。失败时返回退出码，由调用方结合上下文
    判断原因——子进程被 SIGKILL（OOM/超时）时 stderr 是空的，光看输出无法
    区分，需要退出码。
    """
    print("\n+", " ".join(map(str, command)), flush=True)
    completed = subprocess.run([str(x) for x in command], cwd=cwd, env=env)
    return completed.returncode


def parse_composition(stdout, composition):
    """为旧测试解析整数 composition 表格；生产渲染使用官方 API。"""
    import re
    for line in stdout.splitlines():
        row = re.match(r"^\s*" + re.escape(composition) + r"\s+(\d+)\s+(\d+)x(\d+)\s+(\d+)\s*(?:\(([\d.]+)\s*sec\))?", line)
        if not row:
            continue
        first, width, height, second = map(int, row.group(1, 2, 3, 4))
        seconds = float(row.group(5)) if row.group(5) else None
        for fps, total in ((first, second), (second, first)):
            if fps < 1:
                continue
            if seconds is None and (fps, total) == (first, second):
                return {"fps": fps, "width": width, "height": height, "total": total}
            if seconds is not None and abs(total - seconds * fps) <= 1.5:
                return {"fps": fps, "width": width, "height": height, "total": total}
        if seconds is not None:
            raise RuntimeError(f"Cannot disambiguate composition row: {line.strip()}")
    raise RuntimeError(f"Cannot read composition {composition!r}")


def _pid_alive(pid):
    """为旧测试检查 PID 是否仍存活。"""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().split(") ", 1)[1].split()[0] == "Z":
                return False
    except OSError:
        pass
    return True


def project_node(project, remotion=None):
    """只用目标项目声明的依赖；默认不借用样例项目的 Remotion 版本。"""
    if remotion:
        root = Path(remotion).resolve()
        if not root.is_dir():
            raise RuntimeError("--remotion must point to a node_modules directory containing .bin/remotion")
    else:
        root = Path(project).resolve() / "node_modules"
    cli = root / ".bin" / "remotion"
    manifest = root / "remotion" / "package.json"
    if not cli.is_file() or not manifest.is_file():
        raise RuntimeError(f"Remotion is not installed in target project: {root}")
    return root, cli, json.loads(manifest.read_text())["version"]


def _fmt_ms(value):
    """把毫秒格式化成便于阅读的秒数；缺失时返回短横线。"""
    return "-" if value is None else f"{value / 1000:.2f}s"


def print_timings(timings, total_ms):
    """rvc 结束时打印各阶段耗时，直接指出时间花在哪里。"""
    order = [
        ("environment_ms", "环境探测"),
        ("node_startup_ms", "node 启动"),
        ("bundle_ms", "bundle"),
        ("select_composition_ms", "selectComposition"),
        ("render_segments_ms", "渲染分段"),
        ("combine_ms", "合并"),
        ("final_encode_ms", "最终编码"),
        ("verify_ms", "校验"),
    ]
    parts = []
    for key, label in order:
        if timings.get(key) is not None:
            parts.append(f"{label} {_fmt_ms(timings[key])}")
    hit = timings.get("bundle_cache_hit")
    suffix = " (缓存命中)" if hit else (" (重新打包)" if hit is False else "")
    print("CVR timing: " + " | ".join(parts) + suffix + f" | 合计 {_fmt_ms(total_ms)}", flush=True)


def _probe_segment_frames(total_frames, max_candidate_workers):
    """探针的段长：保证每个候选都能切出足够多的段，且各候选段长一致。

    worker 侧用 Math.min(workers, ranges.length) 启动并发，段数不足时多出来
    的 worker 直接空转：90 帧的短探针按自适应只切 1 段，1/2/3/4 个 worker 的
    耗时就会完全相同，测出的「最优值」纯属噪声。

    段长按候选数上限统一决定，而不是随当前候选变化——否则 1 个 worker 时切
    一大段、4 个 worker 时切四小段，各候选的实际工作量不同，比较就失去意义。
    固定段长后，唯一的变量只剩并行度。
    """
    total = max(1, int(total_frames or 1))
    # 要按「最大的候选 worker 数」切，而不是候选个数：候选 [2,3,4] 有 3 个值，
    # 但最大候选要 4 个 worker，只切 3 段的话第 4 个 worker 仍然空转。
    need = max(1, int(max_candidate_workers or 1))
    per = max(PROBE_MIN_SEGMENT_FRAMES, total // need)
    return min(per, total)


def _run_probe_render(project, browser, chrome_mode, root, composition, workers, max_workers, idx,
                      ceiling=None, entry="src/index.ts", hardware=None, total_frames=None,
                      max_candidate_workers=None):
    """渲染一次探针片并返回是否成功，用于渲染方案测速。

    探针走与正式渲染完全相同的路径（含分段与合并），因此测出的相对快慢
    可以直接迁移到真实视频。输出写到临时目录，结束后立即删除。
    """
    import tempfile
    # project 可能是 str 也可能是 Path（调用方来源不同），统一转成 Path，
    # 否则 project / entry 会抛 TypeError。
    project = Path(project)
    probe_dir = Path(tempfile.mkdtemp(prefix="cvr-probe-"))
    output = probe_dir / f"probe-{idx}-{workers}.mp4"
    env = os.environ.copy()
    env.update({
        "REMOTION_BROWSER_EXECUTABLE_PATH": browser,
        "CVR_BROWSER": browser,
        "CVR_CHROME_MODE": chrome_mode,
        "CVR_MODE": "quick",
        "CVR_PROJECT": str(project),
        # 探针必须用与主渲染相同的入口，否则自定义 --entry 的项目会跑错文件。
        "CVR_ENTRY": str((project / entry).resolve()),
        "CVR_COMPOSITION": composition,
        "CVR_OUTPUT": str(output),
        # 探针强制切成 >= workers 段，否则并行测不出来（见 _probe_segment_frames）。
        "CVR_SEGMENT_FRAMES": str(_probe_segment_frames(total_frames, max_candidate_workers)) if total_frames else "",
        "CVR_WORKERS": str(workers),
        # 必须与主渲染用同一份硬件报告（含 topology），否则探针与正式渲染的
        # cpu_limit 口径不同，测出的最优值在主渲染里可能开不出来。
        "CVR_HARDWARE_JSON": json.dumps(hardware or hardware_report()),
        "CVR_MAX_WORKERS": str(max_workers),
        # 探针要按主渲染同样的上限收紧，否则可能测出一个主渲染开不了的值，
        # 方案写进去也用不上。
        **({"CVR_WORKER_CEILING": str(ceiling)} if ceiling else {}),
        "CVR_REQUESTED_WORKERS": str(workers),
        "CVR_INPUT_PROPS": "{}",
        "NODE_PATH": str(root),
    })
    # 绑到大核：能效核吞吐远低于大核，一个 worker 掉上去就会拖住整轮。
    command = ["node", str(SCRIPTS / "remotion-render-worker.cjs")]
    affinity = _runtime.affinity_prefix((hardware or {}).get("topology"), workers)
    if affinity:
        command = ["taskset", "-c", affinity] + command
    try:
        completed = subprocess.run(
            command, cwd=project, env=env, capture_output=True, text=True,
        )
        return completed.returncode == 0 and output.is_file()
    except OSError:
        return False
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def run_probes(project, browser, chrome_mode, root, ceiling, max_workers, mode,
               entry="src/index.ts", hardware=None):
    """测速：分别测短视频与长视频的最优 worker 数并写入方案文件。

    短、长两类各测一遍。候选由 probe_candidates() 按物理大核数给出（最多
    三个），每个候选跑 PROBE_ROUNDS（3）轮取中位数；差距在 PROBE_TIE_RATIO
    以内的判为打平，取 worker 数较小的。总耗时随候选数线性增长，量级是
    候选数 × 2 类 × 轮次次探针渲染。
    """
    # 候选先算出来：最大候选数决定探针要切多少段，也决定探针帧数。
    candidates = _runtime.probe_candidates(ceiling)
    max_candidate = max(candidates) if candidates else 1
    # 探针片必须存在于目标项目，随 skill 分发的副本在这里安装，结束时还原。
    # 注册用的帧数必须与测速时一致，否则分段数与实际帧数对不上。
    installed = _runtime.install_probes(project, max_candidate)
    if installed:
        print(f"CVR probe: installed probe compositions {sorted(installed)}", flush=True)
    probe_frames = {kind: _runtime.probe_frame_count(kind, max_candidate)
                    for kind in ("short", "long")}
    plans = {}
    for kind, composition in (("short", PROBE_SHORT_COMPOSITION), ("long", PROBE_LONG_COMPOSITION)):
        counter = {"n": 0}

        def runner(workers, comp, _kind=kind):
            counter["n"] += 1
            return _run_probe_render(project, browser, chrome_mode, root, comp, workers, max_workers,
                                     counter["n"], ceiling=ceiling, entry=entry, hardware=hardware,
                                     total_frames=probe_frames[_kind], max_candidate_workers=max_candidate)

        print(f"CVR probe [{kind}]: testing workers={candidates} "
              f"({_runtime.PROBE_ROUNDS} round(s) each)", flush=True)
        # 失败的候选单独收集：它们代表「没测到」，而不是「测过且更慢」。
        failures = []
        results = _runtime.probe_worker_count(project, composition, candidates, runner, failures=failures)
        if failures:
            detail = ", ".join(f"{f['workers']}w@round{f['round']}" for f in failures)
            print(f"CVR probe [{kind}]: candidates that failed to render: {detail}", flush=True)
        if not results:
            print(f"CVR probe [{kind}]: no usable result after "
                  f"{len(candidates)} candidate(s); falling back to hardware budget", flush=True)
            continue
        best = results[0]
        # 记录用的帧数必须落在正确的一类里：两类探针的实际帧数（120/240）都
        # 低于 300 帧的分界，直接用会被判成 short，所以按类别取代表值。
        total = _runtime.SHORT_VIDEO_MAX_FRAMES + 1 if kind == "long" else 90
        # 变量名不能叫 entry：entry 是函数参数（源码入口），被覆盖后第二个循环
        # 传给探针的 entry 会变成 dict，探针直接跑错。
        recorded = _runtime.record_plan(project, total, {
            "workers": best["workers"],
            "seconds": best["seconds"],
            "stable": best["stable"],
            "measured": results,
            # 哪些候选没有测出结果。非空说明候选集不完整，最优值可能落在缺口里。
            "failed_candidates": failures,
            "candidates_tested": [r["workers"] for r in results],
        }, source=f"{mode}-probe")
        plans[kind] = recorded
        detail = ", ".join(f"{r['workers']}w={r['seconds']}s{'' if r['stable'] else '(unstable)'}" for r in results)
        print(f"CVR probe [{kind}]: best workers={best['workers']} ({detail})", flush=True)
    # 探针改动过用户的 src/，测完必须还原，否则仓库里会残留与项目无关的内容。
    removed = _runtime.uninstall_probes(project)
    if removed:
        print(f"CVR probe: restored project files ({len(removed)})", flush=True)
    return plans


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True)
    parser.add_argument("--entry", default="src/index.ts")
    parser.add_argument("--composition", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--segment-frames", type=int, help="每段帧数；省略时按总帧数自适应")
    parser.add_argument("--workers", type=int, help="显式指定并行段数；给出时跳过方案探测")
    parser.add_argument("--max-workers", type=int, default=None,
                        help="单任务硬上限；省略时按本机物理大核数，不写死固定值")
    parser.add_argument("--input-props", help="JSON 对象字符串，传给官方 selectComposition 和所有 renderMedia")
    parser.add_argument("--input-props-file", help="包含 JSON 对象的文件；与 --input-props 二选一")
    parser.add_argument("--browser", help="通常省略；统一 CVR 启动器由 render-env.sh 提供")
    parser.add_argument("--remotion", help="显式 node_modules 路径；默认仅目标项目")
    parser.add_argument("--sync-check", action="store_true", help="校准 composition 的逐标记同步检查")
    parser.add_argument("--measure", action="store_true",
                        help="显式运行探针测速并写入方案；默认不测速，直接用本机物理大核数")
    parser.add_argument("--mode", choices=["quick", "full"], default="quick", help="quick 为 cvr 轻量启动，full 为 rvc 详细启动")
    args = parser.parse_args()
    if args.max_workers is not None and not 1 <= args.max_workers <= 32:
        parser.error("--max-workers must be 1..32")
    if args.workers is not None and args.workers < 1:
        parser.error("--workers must be positive")
    if args.segment_frames is not None and args.segment_frames < 5:
        parser.error("--segment-frames must be at least 5 (Remotion chunk audio requirement)")
    if args.input_props and args.input_props_file:
        parser.error("--input-props and --input-props-file are mutually exclusive")

    started = time.monotonic()
    project = Path(args.project).resolve()
    output = Path(args.output).resolve()
    if not project.is_dir():
        parser.error("project does not exist")
    root, _cli, _version = project_node(project, args.remotion)
    for executable in ("node", "ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            parser.error(executable + " not found")
    environment_ms = None
    if args.mode == "full":
        env_started = time.monotonic()
        info = json.loads(subprocess.run([str(SCRIPTS / "render-env.sh"), "--json"], capture_output=True, text=True, check=True, env={**os.environ, "CVR_PROJECT": str(project)}).stdout)
        environment_ms = int((time.monotonic() - env_started) * 1000)
        browser = args.browser or info["browser_env"]["remotion"]["BROWSER_EXECUTABLE"]
        chrome_mode = "chrome-for-testing" if info["chrome"] else "headless-shell"
    else:
        browser = args.browser or _runtime.quick_browser(project)
        chrome_mode = "chrome-for-testing" if browser and "headless_shell" not in browser else "headless-shell"
    if not browser:
        parser.error("No Chrome detected")
    props_text = Path(args.input_props_file).read_text() if args.input_props_file else args.input_props
    props = json.loads(props_text) if props_text else {}
    if not isinstance(props, dict):
        parser.error("input props must be a JSON object")

    hardware = hardware_report()
    # 核心结构探测一次后缓存，之后直接读，不再重复探测。
    profile, profile_cached = _runtime.resolve_core_profile(project, hardware)
    if args.mode == "full":
        topo = profile.get("topology") or {}
        # 只报探测事实；worker 数由下面的方案行给出，避免两处数字不一致。
        print(f"CVR core profile: {'cached' if profile_cached else 'probed'} "
              f"(physical big cores={topo.get('big_cores') or 'n/a'}, "
              f"schedulable={topo.get('big_cores_schedulable') or 'n/a'})", flush=True)
    # 带上 project，使 worker_budget 能读到本机实测的内存系数。
    budget = worker_budget({**hardware, "topology": profile.get("topology"), "project": str(project)},
                           requested=args.workers, maximum=args.max_workers)
    ceiling = _runtime.effective_worker_ceiling(hardware, profile.get("topology"), args.max_workers)

    # 选择渲染方案：短/长视频各自一套，存在方案文件里。
    # 默认不测速，用本机物理大核数直接开跑；--measure 可显式测速并覆盖方案。
    plan_kind = None
    chosen_workers = None
    plan_source = "budget"
    plan_label = "hardware budget"
    duration = meta_width = meta_height = None
    if args.workers is not None:
        # 显式指定优先级最高，不做任何探测。
        chosen_workers = args.workers
        plan_source = "manual"
        plan_label = "manual --workers"
    else:
        # 先取真实帧数，才知道该用短还是长的方案。
        try:
            meta = json.loads(subprocess.run(
                ["node", str(SCRIPTS / "remotion-probe-composition.cjs")],
                cwd=project, env={**os.environ, "NODE_PATH": str(root),
                                  "CVR_PROJECT": str(project), "CVR_COMPOSITION": args.composition,
                                  "CVR_ENTRY": str((project / args.entry).resolve()),
                                  "CVR_INPUT_PROPS": json.dumps(props, ensure_ascii=False),
                                  "CVR_BROWSER": browser, "CVR_CHROME_MODE": chrome_mode,
                                  "CVR_MODE": args.mode},
                capture_output=True, text=True, check=True,
            ).stdout)
            duration = int(meta["durationInFrames"])
            # 内存兜底必须用本次真实分辨率，不能用默认值：1080p 与 320x180 的
            # 每 worker 估算差好几倍，用错会把 worker 数砍得不合理。
            meta_width = int(meta.get("width") or 0) or None
            meta_height = int(meta.get("height") or 0) or None
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError):
            duration = meta_width = meta_height = None
        if duration:
            plan_kind = _runtime.classify_video_length(duration)
            # 仅在显式要求时测速：测速成本高，默认路径不应承担。
            if args.measure:
                print("CVR measure: running probes for short and long plans", flush=True)
                run_probes(project, browser, chrome_mode, root, ceiling, args.max_workers, args.mode,
                           args.entry, {**hardware, "topology": profile.get("topology")})
            existing = _runtime.plan_for(project, duration)
            if existing and existing.get("workers"):
                # 已有记录时直接采用，不再测速。
                chosen_workers = existing.get("workers")
                plan_source = existing.get("source", "recorded")
                plan_label = plan_source
            else:
                # 没有记录时用默认值立即开跑，不等待测速。
                # 默认值只依据本机物理大核数，不含任何预设结论。
                chosen_workers = _runtime.default_workers(profile.get("topology"), args.max_workers)
                plan_source = "default"
                plan_label = "default: physical big cores, no measurement yet"
        else:
            plan_label = "composition metadata unavailable, using hardware budget"
    if chosen_workers is not None:
        # 方案值由实测得出，优先采用，只受物理大核上限约束。
        plan_workers = min(int(chosen_workers), ceiling)
        # 之后仅用内存做兜底，并按本次实际分辨率估算。核数不足时 taskset 会
        # 退回可用核，而内存不足会直接 OOM，因此只保留这一层限制。
        # 必须带上 project：内存系数是按本机实测存在项目缓存里的，漏掉就会
        # 继续用公式估算值，测了也等于没测。
        memory_budget = worker_budget({**hardware, "topology": profile.get("topology"), "project": str(project)},
                                      width=int(meta_width or 1920), height=int(meta_height or 1080),
                                      requested=plan_workers, maximum=plan_workers)
        memory_limit = memory_budget["memory_limit"] or plan_workers
        workers = min(plan_workers, memory_limit)
        # 内存受限时的说明：每 worker 实测占用、当前可用量、其它进程已占用的部分。
        _total = (hardware.get("memory_total_bytes") or 0) / 1024**3
        _avail = (hardware.get("memory_available_bytes") or 0) / 1024**3
        _per = memory_budget["estimated_bytes_per_worker"] / 1024**2
        _basis = memory_budget.get("memory_basis", "estimated")
        memory_detail = (f"{_per:.0f} MiB/worker {_basis}, {_avail:.1f} GB free of {_total:.1f} GB"
                         + (f", {_total - _avail:.1f} GB used by other processes" if _total > _avail else ""))
        budget = {**memory_budget, "workers": workers, "plan_workers": plan_workers,
                  "limited_by": "memory" if workers < plan_workers else "plan"}
        # 在这一步之后打印，此时 workers 已经是真实生效值：方案里可能写着超出
        # 天花板或内存允许的数字，先报原值会与实际执行的并行度不符。
        if args.mode == "full":
            # 与方案原值比较，而不是与 plan_workers：plan_workers 已在赋值时
            # 被 ceiling 压过，用它比较会让「8 被压到 4」这种降级不显示。
            requested_workers = int(chosen_workers)
            detail = f" -> {workers}" if workers != requested_workers else ""
            limit = ""
            if workers < requested_workers:
                # 说明是谁把数字压下来的：先看天花板，再看内存。
                if ceiling < requested_workers:
                    limit = f" (capped at {ceiling} big cores)"
                elif memory_limit < requested_workers:
                    # 说清楚是谁把数字压下来的，以及能不能改善。用户看到
                    # workers 从 4 掉到 1 会以为机器不够用；实际往往是别的
                    # 进程占着内存。给出占用情况，他才知道关掉别的程序会更快。
                    limit = f" (memory limited: {memory_detail})"
            # 报原值再报生效值，如 "workers=8 -> 4"，让降级一眼可见。
            shown = f"{requested_workers} -> {workers}" if detail else f"{workers}"
            print(f"CVR render plan: {plan_kind or 'unknown'} video "
                  f"({duration if duration else '?'} frames) -> workers={shown} "
                  f"[{plan_label}]{limit}", flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    # 串行同一 composition 的独立渲染任务。
    lock_path = project / f".cvr-{args.composition}.foreground.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = open(lock_path, "a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        raise RuntimeError(f"Another CVR job holds {lock_path}; wait for it instead of stacking parallel workers")
    try:
        env = os.environ.copy()
        env.update({
            "REMOTION_BROWSER_EXECUTABLE_PATH": browser,
            "CVR_BROWSER": browser,
            "CVR_CHROME_MODE": chrome_mode,
            "CVR_MODE": args.mode,
            "CVR_PROJECT": str(project),
            "CVR_ENTRY": str((project / args.entry).resolve()),
            "CVR_COMPOSITION": args.composition,
            "CVR_OUTPUT": str(output),
            "CVR_SEGMENT_FRAMES": str(args.segment_frames or ""),
            "CVR_WORKERS": str(budget["workers"]),
            # 把本机实测的内存系数一并传下去：JS 侧用它替代公式估算，两侧口径一致。
            "CVR_HARDWARE_JSON": json.dumps({
                **hardware,
                "topology": profile.get("topology"),
                "measured_bytes_per_worker": (_runtime.load_memory_calibration(project) or {}).get("bytes_per_worker"),
                "memory_safety_factor": _runtime.MEMORY_SAFETY_FACTOR,
            }),
            # 未显式限制时传上限本身（即本机大核数）：worker 侧用 Number() 解析，
            # 传字符串 "None" 会得到 NaN 并让分段数算错。
            "CVR_MAX_WORKERS": str(args.max_workers if args.max_workers is not None else ceiling),
            "CVR_WORKER_CEILING": str(ceiling),
            "CVR_REQUESTED_WORKERS": str(args.workers or ""),
            "CVR_INPUT_PROPS": json.dumps(props, ensure_ascii=False),
            "NODE_PATH": str(root),
        })
        # 正式渲染同样绑大核：并行数再准，调度器把小核分给某个 worker 仍会
        # 拖住整轮。worker 数已知，只绑这么多个大核，避免多余的空置。
        render_command = ["node", str(SCRIPTS / "remotion-render-worker.cjs")]
        affinity = _runtime.affinity_prefix(profile.get("topology"), budget["workers"])
        if affinity:
            render_command = ["taskset", "-c", affinity] + render_command
        if args.mode == "full":
            print(f"CVR affinity: pinning workers to big cores [{affinity or 'none'}]", flush=True)

        # 逐段渲染：先规划出分段范围，再一段一个前台进程渲完，
        # 最后单独合并。每段渲完即落盘，进程退出不会带走已完成的段。
        plan = _plan_segments(project, root, env)
        seg_dir = Path(plan["segment_dir"])
        ranges = plan["ranges"]
        if args.mode == "full":
            print(f"CVR segmentation: {len(ranges)} segment(s) x {plan['segment_frames']} frames "
                  f"-> {seg_dir}", flush=True)
        segment_timings = _render_segments_foreground(
            project, root, env, profile, budget["workers"], ranges, seg_dir,
        )
        # 段阶段的真实并发数：受段数与 worker 上限共同约束，传给 combine
        # 让报告里的并发数与实际渲染一致，而不是合并时的重算值。
        effective_workers = max(1, min(budget["workers"], len(ranges)))
        combine_code = _combine_segments(project, root, env, seg_dir, effective_workers)
        if combine_code != 0:
            raise RuntimeError(json.dumps({
                "reason": "out_of_memory" if combine_code in (2, -9, 137) else
                          "timeout" if combine_code == 3 else "render_error",
                "exit": combine_code,
                "workers": budget.get("workers"),
                "per_worker_mib": round(budget.get("estimated_bytes_per_worker", 0) / 1024**2),
                "memory_basis": budget.get("memory_basis"),
            }, ensure_ascii=False))
        expected = json.loads((output.parent / (output.stem + ".render-report.json")).read_text())
        # 报告里要能查到实际绑了哪些核，否则事后无法核对「跑的是不是大核」。
        expected["affinity"] = affinity or None
        # 把分段执行的细节并进报告：段目录、每段进程数，便于事后核对与续跑。
        expected.setdefault("timings", {}).update(segment_timings)
        expected["segment_dir"] = str(seg_dir)
        verify_started = time.monotonic()
        verified = verify_video(output, {
            "frames": expected["frames"], "fps": expected["fps"],
            "width": expected["width"], "height": expected["height"],
            "duration": expected["duration"], "require_audio": False,
            "sample_rate": expected.get("sample_rate"), "channels": expected.get("channels"),
            "start_time_tolerance": 0.05,
        }, decode=args.mode == "full")
        timings = expected.setdefault("timings", {})
        if environment_ms is not None:
            timings["environment_ms"] = environment_ms
        timings["verify_ms"] = int((time.monotonic() - verify_started) * 1000)
        sync_report = expected.get("sync", {"status": "not-measured"})
        if args.sync_check:
            # 对包含校准信号的本次输出测量同步偏移。
            command = [sys.executable, str(SCRIPTS / "check-av-sync.py"), str(output), "--json"]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            sync_report = json.loads(result.stdout)
            sync_report["status"] = "passed"
        expected["sync"] = sync_report
        expected["mode"] = args.mode
        expected["core_profile_cached"] = profile_cached
        # 记录本次实测的内存峰值，用于校准「每 worker 占多少」的估算。
        # 没有采样时返回 None，跳过；记录失败不影响渲染结果。
        memory_obs = expected.get("memory") or {}
        if memory_obs.get("peak_system_used_bytes"):
            _runtime.record_memory_calibration(project, {
                "peak_system_used_bytes": memory_obs.get("peak_system_used_bytes"),
                "workers": memory_obs.get("workers") or budget["workers"],
                "width": (memory_obs.get("resolution") or [None, None])[0] or expected.get("width"),
                "height": (memory_obs.get("resolution") or [None, None])[1] or expected.get("height"),
            })
        if plan_kind:
            expected["render_plan"] = {"kind": plan_kind, "frames": duration, "workers": budget["workers"]}
            # 把本次真实渲染的耗时记录下来。这不是测速：没有额外渲染开销，
            # 数据来自用户本来就要跑的那一次，用于日后判断默认值是否合适。
            _runtime.record_observation(project, {
                "kind": plan_kind,
                "frames": duration,
                "workers": budget["workers"],
                "affinity": affinity or None,
                "seconds": round(timings.get("render_segments_ms", 0) / 1000.0, 2),
                "total_seconds": round(time.monotonic() - started, 2),
                "source": plan_source,
                "width": expected.get("width"),
                "height": expected.get("height"),
            })
        if args.mode == "full":
            expected["budget"] = budget
            expected["hardware_at_start"] = hardware
            expected["verification"] = {
                "decoded_frames": verified.get("decoded_frames"),
                "container_and_streams": "passed",
                "full_audio_video_decode": "passed",
                "sync": sync_report,
            }
        # 先补上总耗时再落盘，保证报告里的 timings 是完整的。
        total_ms = int((time.monotonic() - started) * 1000)
        timings["total_ms"] = total_ms
        (output.parent / (output.stem + ".render-report.json")).write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
        summary = {"ok": True, "mode": args.mode, "output": str(output), "frames": expected["frames"], "fps": expected["fps"], "duration": expected["duration"]}
        if args.mode == "full":
            summary["sync"] = sync_report
            print_timings(timings, total_ms)
        print("RENDER_OK " + json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _segment_env(base_env, index):
    """给某一段的独立渲染进程准备环境变量。

    每段一个进程，因此段号要带进去；段产物目录显式指定，保证同一任务的所有
    段落落在同一处，能被后续进程读回来继续用。
    """
    env = dict(base_env)
    env["CVR_STAGE"] = "segment"
    env["CVR_SEGMENT_INDEX"] = str(index)
    return env


def _core_affinity_list(profile):
    """返回当前能绑定的大核；被 cgroup 屏蔽的核直接排除。

    绑核前先实测，绑不上就跳过，不让调度问题让整段渲染失败。
    """
    topo = profile.get("topology") if isinstance(profile, dict) else None
    candidates = _runtime.big_core_indices(topo)
    usable = []
    for core in candidates:
        try:
            check = subprocess.run(["taskset", "-c", str(core), "true"],
                                   capture_output=True, timeout=5)
            if check.returncode == 0:
                usable.append(core)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return usable


def _render_segments_foreground(project, root, env, profile, workers, ranges, seg_dir):
    """逐段渲染：每段一个独立前台进程、一段一个 Chrome、渲完即退出。

    为什么要一段一个进程：整片在一个 node 进程里渲染时，进程被外层超时砍掉，
    已经渲好的段落跟着一起没了——临时目录在 finally 里被删掉，几十秒的渲染
    白做。拆成一段一进程后，段产物在段落完成时就落盘，进程退出也不会带走；
    浏览器随之关闭，下次开新进程开新浏览器，不复用上一个的状态。

    并行度：同时最多 workers 段在跑，每个进程绑到一个自己的大核上，互不抢核。
    """
    import threading

    total = len(ranges)
    cores = _core_affinity_list(profile)
    lock = threading.Lock()
    next_index = {"value": 0}
    failures = []
    timings = {"render_segments_ms": 0, "segment_processes": 0, "segments_rendered": 0,
               "segments_skipped": 0}

    def worker_slot(slot):
        """一个并发槽：反复领取下一段，渲完立刻退出浏览器再领下一段。"""
        while True:
            with lock:
                index = next_index["value"]
                if index >= total:
                    return
                next_index["value"] = index + 1
            # 段号对应绑核：绑核前先实测，绑不上就不绑，不让调度问题杀死渲染。
            affinity = None
            if cores:
                candidate = str(cores[slot % len(cores)])
                try:
                    check = subprocess.run(["taskset", "-c", candidate, "true"],
                                           capture_output=True, timeout=5)
                    if check.returncode == 0:
                        affinity = candidate
                except (OSError, subprocess.TimeoutExpired):
                    pass
            command = ["node", str(SCRIPTS / "remotion-render-worker.cjs")]
            if affinity:
                command = ["taskset", "-c", affinity] + command
            segment_env = _segment_env(env, index)
            segment_env["CVR_SEGMENT_DIR"] = str(seg_dir)
            # 进子进程前先检查完成标记，用于计数；段进程本身也会做这个检查。
            already_done = (seg_dir / f"{str(index + 1).zfill(2)}.json").exists()
            try:
                completed = subprocess.run(command, cwd=project, env=segment_env)
                code = completed.returncode
            except OSError as exc:
                with lock:
                    failures.append({"index": index, "exit": -1, "error": str(exc)})
                continue
            with lock:
                timings["segment_processes"] += 1
                if code == 0:
                    if already_done:
                        timings["segments_skipped"] += 1
                    else:
                        timings["segments_rendered"] += 1
                else:
                    failures.append({"index": index, "exit": code})
            if code != 0:
                # 一段失败就停下：后面的段即使渲了，合并时也缺这一段，
                # 继续渲染只是浪费机器时间。已完成的段保留，下次接着来。
                with lock:
                    next_index["value"] = total
                return

    started = time.monotonic()
    slots = max(1, min(workers, total))
    threads = [threading.Thread(target=worker_slot, args=(slot,), daemon=False)
               for slot in range(slots)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    timings["render_segments_ms"] = int((time.monotonic() - started) * 1000)
    if failures:
        detail = {
            "reason": "out_of_memory" if any(f["exit"] in (2, -9, 137) for f in failures) else
                      "timeout" if any(f["exit"] == 3 for f in failures) else "render_error",
            "exit": failures[0]["exit"],
            "failed_segments": [f["index"] for f in failures],
            "workers": workers,
        }
        raise RuntimeError(json.dumps(detail, ensure_ascii=False))
    return timings


def _combine_segments(project, root, env, seg_dir, effective_workers=None):
    """用独立进程合并已落盘的分段，产出最终成片。

    effective_workers 是段阶段的真实并发数。combine 阶段不开浏览器，
    重算预算只会得到一个与段阶段无关的数字，所以直接把真实并发传下去，
    让报告里的 memory.workers 与段阶段一致。
    """
    combine_env = dict(env)
    combine_env["CVR_STAGE"] = "combine"
    combine_env["CVR_SEGMENT_DIR"] = str(seg_dir)
    if effective_workers:
        combine_env["CVR_EFFECTIVE_WORKERS"] = str(int(effective_workers))
    command = ["node", str(SCRIPTS / "remotion-render-worker.cjs")]
    # 绑核前先实测；绑不上就不绑。cgroup 掩码会随负载变化，探测通过到
    # spawn 之间仍可能被收紧，taskset 失败不该让合并失败。
    topology = None
    if env.get("CVR_HARDWARE_JSON"):
        try:
            topology = json.loads(env["CVR_HARDWARE_JSON"]).get("topology")
        except (ValueError, TypeError):
            pass
    if topology:
        for core in _runtime.big_core_indices(topology):
            try:
                check = subprocess.run(["taskset", "-c", str(core), "true"],
                                       capture_output=True, timeout=5)
                if check.returncode == 0:
                    command = ["taskset", "-c", str(core)] + command
                    break
            except (OSError, subprocess.TimeoutExpired):
                continue
    return run(command, cwd=project, env=combine_env)


def _plan_segments(project, root, env):
    """问 worker 要分段范围，不渲染。"""
    plan_env = dict(env)
    plan_env["CVR_STAGE"] = "plan"
    completed = subprocess.run(
        ["node", str(SCRIPTS / "remotion-render-worker.cjs")],
        cwd=project, env=plan_env, capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(json.dumps({
            "reason": "plan_error",
            "exit": completed.returncode,
            "detail": (completed.stderr or completed.stdout or "").strip()[-800:],
        }, ensure_ascii=False))
    for line in completed.stdout.splitlines():
        if line.startswith("CVR_PLAN_OK "):
            return json.loads(line[len("CVR_PLAN_OK "):])
    raise RuntimeError("segment plan produced no plan output")


def _describe_failure(exc):
    """把失败翻译成「可能的原因 + 该调什么」，一行给机器读。

    OOM 与超时都会让子进程被 SIGKILL：exit -9、stderr 为空。上层只看到
    「失败了」无法区分，只能逐个试错。这里按退出码与 stderr 归类，并给出
    对应建议，使重试有方向。
    """
    code = getattr(exc, "returncode", None)
    stderr = (getattr(exc, "stderr", None) or "").strip() if isinstance(getattr(exc, "stderr", None), str) else ""
    detail = stderr.splitlines()[-1] if stderr else ""
    reason, hint = "render_error", "check the composition and project dependencies"
    # 渲染子进程失败时，上面抛出的 RuntimeError 里带着结构化 detail，先解析它。
    text = str(exc)
    embedded = None
    if text.startswith("{") and '"reason"' in text:
        try:
            embedded = json.loads(text)
        except ValueError:
            embedded = None
    if embedded:
        code = embedded.get("exit", code)
        stderr = text  # 让下面的分类能读到 reason
    if code in (-9, 137) or "out_of_memory" in stderr or (embedded or {}).get("reason") == "out_of_memory":
        reason = "out_of_memory"
        hint = "reduce --workers or --max-workers, or close other apps to free memory"
    elif code in (-15, 143):
        reason, hint = "terminated", "the process was asked to stop; rerun when idle"
    elif code in (-2, 130):
        reason, hint = "interrupted", "interrupted by user"
    elif code == 3 or "timeout" in stderr.lower() or (embedded or {}).get("reason") == "timeout":
        reason, hint = "timeout", "a segment exceeded its time budget; raise CVR_SEGMENT_TIMEOUT_MS or reduce segment count"
    elif "not installed in target project" in text or "No Chrome detected" in text:
        reason, hint = "environment", "install the missing dependency or point --project at a built project"
    payload = {"reason": reason, "exit": code, "hint": hint}
    if embedded:
        for key in ("workers", "per_worker_mib", "memory_basis"):
            if embedded.get(key) is not None:
                payload[key] = embedded[key]
    if embedded and embedded.get("detail"):
        payload["detail"] = embedded["detail"][:300]
    if detail:
        payload["detail"] = detail[:300]
    return "CVR_RENDER_FAILED " + json.dumps(payload, ensure_ascii=False)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(_describe_failure(exc), file=sys.stderr)
        sys.exit(exc.returncode or 1)
    except KeyboardInterrupt:
        print("CVR_RENDER_FAILED " + json.dumps({"reason": "interrupted", "exit": 130,
                                                 "hint": "interrupted by user"}, ensure_ascii=False),
              file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(_describe_failure(exc), file=sys.stderr)
        sys.exit(1)

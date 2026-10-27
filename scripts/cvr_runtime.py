#!/usr/bin/env python3
"""提供浏览器发现、硬件预算、渲染方案、帧率解析和媒体验收函数。"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
import subprocess
import time
from fractions import Fraction
from pathlib import Path

# 核心画像缓存的版本号。字段或判定方式变化时递增，使旧缓存失效并重新探测。
CORE_PROFILE_VERSION = 3
RENDER_PLAN_VERSION = 1
# 短视频与长视频的分界：与分段自适应的第一档边界一致，300 帧（10 秒@30fps）。
SHORT_VIDEO_MAX_FRAMES = 300
# 每个候选 worker 跑的探针轮次。多轮取中位数以抵消单轮波动。
PROBE_ROUNDS = 3
# 轮次间相对偏差超过此比例时标记为不稳定。该标记仅作提示，不参与排序。
PROBE_STABILITY_LIMIT = 0.35
# 两个候选的中位数差距小于这个比例时判为「打平」，取 worker 数较小的那个。
# 并行度越高，调度与内存开销越大，打平时少用 worker 更稳。
PROBE_TIE_RATIO = 0.05
# 探针候选个数。
PROBE_CANDIDATE_COUNT = 3
# 探针片的基准帧数。分段按 30 帧/段下限算，120 帧正好切 4 段，够覆盖最多
# 4 个候选；帧数太少会让候选数超过段数，多出来的 worker 空转、测不出并行
# 差异。分成 short/long 两套只为反映两类的相对体量，具体帧数由
# probe_frame_count() 按候选数再校正。
PROBE_FRAMES = {"short": 120, "long": 240}
# 每个候选至少需要的分段数 × 这个帧数 = 探针应有的总帧数下限。
PROBE_SEGMENT_MIN_FRAMES = 30
# 大核的最低频率（kHz，2.1GHz）。真实数据取自各芯片公开规格：
#   - 天玑 8500 / 8400 的 A725 全大核最低档是 2.2G / 2.15G，都是大核；
#   - 天玑 9500 的 C1-Pro 是 2.7G，也是大核；
#   - 骁龙 8 Elite 系列为 2+6 全大核，没有小核。
# 而传统混合架构的能效核（A520/A510 等）在 1.8G-2.0G。取 2.1G 正好把两者分开。
BIG_CORE_MIN_FREQ_KHZ = 2100000
# 手机 SoC 的物理大核数上限。主流旗舰为 8 核（如 2+6、1+3+4），全大核设计下
# 大核数同样不超过 8。超过此值多为频率档判定异常，按上限封顶。
MAX_MOBILE_BIG_CORES = 8
# 实测内存系数的安全余量。回归给出的是平均每 worker 增量，单次运行的实际峰值
# 可能高出两成以上；按平均值直接算并行度会偶发 OOM，因此上浮这个比例。
MEMORY_SAFETY_FACTOR = 1.3
# 内存系数拟合的可信度下限。worker 数变化引起的峰值变化，至少要占总峰値波动
# 的这个比例，拟合出的斜率才算信号而非噪声。容器把内存卡在同一上限时，各次
# 峰值几乎相同，斜率会随机落在正负之间；不设这道门槛就会拿噪声当系数。
MEMORY_FIT_MIN_SIGNAL_RATIO = 2.0
# 每 worker 内存增量在物理上的下限。一个 headless Chromium 进程的常驻内存
# 不会低于几百 MB；拟合值低于此数，说明测到的不是 worker 的开销，而是容器
# 上限、页面缓存之类的其它部分，不能拿来当系数。
MEMORY_MIN_BYTES_PER_WORKER = 400 * 1024**2

BROWSER_ROOT = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.environ.get("CVR_BROWSER_ROOT") or str(Path.home() / ".cache" / "ms-playwright"))


def run_capture(command, **kwargs):
    """阻塞运行并返回文本 stdout；失败交给调用方，不创建后台任务。"""
    return subprocess.run([str(x) for x in command], capture_output=True, text=True, check=True, **kwargs).stdout


def probe_version(candidates):
    """只探测实际存在且可执行的浏览器；版本号来自二进制而非目录名。"""
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        try:
            result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=12)
            if result.returncode == 0:
                lines = (result.stdout or result.stderr).splitlines()
                if lines:
                    return {"path": str(path), "version": lines[0]}
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def _version_key(path):
    """Playwright 修订目录按数字排序，避免把 1148 排在 1248 后面。"""
    match = re.search(r"-(\d+)(?:/|$)", str(path))
    return (int(match.group(1)) if match else -1, str(path))


def discover_browsers():
    """保留显式覆盖，否则从系统和全部 Playwright 修订目录动态选择。"""
    chrome_candidates = [
        os.environ.get("VIDEO_CHROME_REAL"),
        os.environ.get("CHROME_VIDEO_RENDER_CHROME"),
        shutil.which("google-chrome"),
        shutil.which("chrome"),
        "/opt/google/chrome/chrome",
    ]
    shell_candidates = [
        os.environ.get("VIDEO_HEADLESS_REAL"),
        os.environ.get("HEADLESS_SHELL"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    chrome_candidates += sorted(glob.glob(str(BROWSER_ROOT / "chromium-*/chrome-linux*/chrome")), key=_version_key, reverse=True)
    shell_candidates += sorted(glob.glob(str(BROWSER_ROOT / "chromium_headless_shell-*/chrome-linux*/headless_shell")), key=_version_key, reverse=True)
    return probe_version(chrome_candidates), probe_version(shell_candidates)


def _read_cgroup_number(paths):
    """同时支持 cgroup v2 和 Android 常见的 cgroup v1 路径；无法读取时不猜测限额。"""
    for name in paths:
        try:
            value = Path(name).read_text().strip()
            if value and value != "max":
                return int(value)
        except (OSError, ValueError):
            continue
    return None


def _read_cpu_quota():
    # 读取 cgroup CPU 配额。
    try:
        fields = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if len(fields) == 2 and fields[0] != "max":
            return max(1, math.floor(int(fields[0]) / int(fields[1])))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    quota = _read_cgroup_number(["/sys/fs/cgroup/cpu/cpu.cfs_quota_us"])
    period = _read_cgroup_number(["/sys/fs/cgroup/cpu/cpu.cfs_period_us"])
    if quota is not None and quota > 0 and period:
        return max(1, math.floor(quota / period))
    return None


def hardware_report():
    """读取调度可见 CPU、内存和 Android/cgroup 约束，供每次任务重新计算预算。"""
    total = available = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in ("MemTotal", "MemAvailable"):
                amount = int(value.strip().split()[0]) * 1024
                if key == "MemTotal":
                    total = amount
                else:
                    available = amount
    except OSError:
        pass
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    quota = _read_cpu_quota()
    if quota:
        cpus = min(cpus, quota)
    cgroup_max = _read_cgroup_number(["/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"])
    # 有 cgroup 内存限制时收紧可用预算。
    if cgroup_max and 0 < cgroup_max < (1 << 60):
        total = min(total or cgroup_max, cgroup_max)
        available = min(available, total)
    return {"cpu_count": cpus, "memory_total_bytes": total or None, "memory_available_bytes": available, "cpu_quota": quota}


def physical_topology():
    """读取芯片全部物理核的频率，得到真实的大核数量。

    容器与 Android 的 cgroup 会把核从 CPU 亲和性里屏蔽掉，使
    sched_getaffinity() 只返回一部分核，同一台设备在不同时刻可能是
    「6 核 2 大核」「7 核 3 大核」「8 核 4 大核」。但被屏蔽的核，其
    /sys/.../cpufreq 频率文件仍可读取，因此这里按物理核统计，得到与当前
    调度状态无关的稳定结果。读不到频率信息时返回 None。
    """
    base = Path("/sys/devices/system/cpu")
    try:
        cores = sorted(int(p.name[3:]) for p in base.glob("cpu[0-9]*") if p.name[3:].isdigit())
    except OSError:
        return None
    freqs = []
    # 保留每个频率对应的核编号：绑核时必须知道哪个 CPU 编号是大核。
    freq_pairs = []
    for index in cores:
        for name in ("cpuinfo_max_freq", "cpuinfo_peak_freq"):
            try:
                value = int((base / f"cpu{index}" / "cpufreq" / name).read_text().strip())
                freqs.append(value)
                freq_pairs.append([value, index])
                break
            except (OSError, ValueError):
                continue
    if len(freqs) < 2:
        return None
    fastest = max(freqs)
    slowest = min(freqs)
    ordered = sorted(freqs)
    median = ordered[len(ordered) // 2]
    # 只靠相对比例无法区分「能效核」与「性能核」：以骁龙的 2+6 全大核为例，
    # 按公开规格是 2 个 ~4.7G 超大核加 6 个 ~3.6G 性能核，最慢核与最快核相差
    # 约四分之一，纯比例法会把整组性能核误判成小核，大核数被大幅低估。
    #
    # 改用两层判断：
    # 1) 绝对下限 —— 能效小核的频率在真实手机 SoC 上都不超过 2.2GHz
    #    （天玑 8500 的 A725 是 2.2G、天玑 9500 的 C1-Pro 是 2.7G，
    #    骁龙 8 系则完全取消了小核）。低于该值的核一定不是大核。
    # 2) 相对下限 —— 大核之间频率也可能差一档，取中位数与最慢核 1.2 倍
    #    中的较大者。三档结构（若干能效核 + 若干性能核 + 1-2 个超大核）里，
    #    频率居中的性能核会因此被正确保留。
    if slowest >= BIG_CORE_MIN_FREQ_KHZ:
        # 最慢的核都高于能效上限，说明这是全大核架构（骁龙 8 Elite 的 2+6、
        # 天玑 9500 的 1+3+4、天玑 8500 的 1+3+4），所有核都是大核。
        # 此时任何「相对比例」判断都会把较慢的大核群误判掉——按公开规格，
        # 这类架构里次快核与最快核的频率差可以接近 1/3。
        threshold = BIG_CORE_MIN_FREQ_KHZ
    else:
        # 含能效小核的混合架构：在中位数与能效上限之间取小，再与最慢核的
        # 1.2 倍取大。这样三档结构里的性能核被保留，能效核被排除。
        threshold = max(min(median, BIG_CORE_MIN_FREQ_KHZ), slowest * 1.2)
    big = sum(1 for f in freqs if f >= threshold)
    # 手机 SoC 大核数不会超过 8（骁龙 2+6、天玑 1+3+4 均为 8 核），
    # 判档异常时按上限兜底。
    big = min(big, MAX_MOBILE_BIG_CORES)
    try:
        allowed = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        allowed = None
    # 用物理核频率定标准，但按可调度核数算「当前真正能用的大核」。
    big_schedulable = 0
    for index in cores:
        if allowed is not None and index not in allowed:
            continue
        for name in ("cpuinfo_max_freq", "cpuinfo_peak_freq"):
            try:
                freq = int((base / f"cpu{index}" / "cpufreq" / name).read_text().strip())
            except (OSError, ValueError):
                continue
            if freq >= threshold:
                big_schedulable += 1
            break
    schedulable = len(allowed) if allowed is not None else len(freqs)
    return {"cores": len(freqs), "big_cores": big, "max_freq_khz": fastest,
            "median_freq_khz": median, "threshold_khz": int(threshold),
            "freqs": freqs, "freqs_by_core": freq_pairs,
            "schedulable_cores": schedulable,
            "big_cores_schedulable": big_schedulable}


def big_core_indices(topology=None):
    """返回应绑定的物理大核编号。

    并行数再准，调度器仍可能把 worker 扔到能效核上：能效核的吞吐只有大核的
    几分之一，一个 worker 落到上面就会拖住整轮分段，其他 worker 只能干等。
    绑核把「并行度」变成真正可控的量。

    返回值在调用时会用 taskset 验证，被 cgroup 屏蔽的编号不会混进来
    （验证发生在调用时，不是静态保证）。
    """
    topo = topology if isinstance(topology, dict) else (physical_topology() or {})
    threshold = int(topo.get("threshold_khz") or BIG_CORE_MIN_FREQ_KHZ)
    base = Path("/sys/devices/system/cpu")
    pairs = topo.get("freqs_by_core") or []
    try:
        allowed = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        allowed = set()
    if pairs:
        # 逐核频率齐全时直接按编号挑大核。注意这里【不能】用当前掩码过滤：
        # 绑核本身就是主动收窄，掩码里多数时刻只剩 1 个大核，用它过滤会把
        # 本来可用的大核剔光，绑核等于失效。
        return [index for freq, index in pairs if int(freq) >= threshold]
    # 没有逐核频率时才现场读文件；这条路径必须按掩码过滤，否则 taskset 绑到
    # 一个被屏蔽的编号会直接失败。
    picked = []
    for path in sorted(base.glob("cpu[0-9]*")):
        name = path.name
        if not name[3:].isdigit():
            continue
        index = int(name[3:])
        if allowed and index not in allowed:
            continue
        for attr in ("cpuinfo_max_freq", "cpuinfo_peak_freq"):
            try:
                freq = int((path / "cpufreq" / attr).read_text().strip())
            except (OSError, ValueError):
                continue
            if freq >= threshold:
                picked.append(index)
            break
    return picked


def affinity_prefix(topology=None, maximum=None):
    """生成 taskset 的 CPU 列表；拿不到大核时返回空串（不绑核）。

    maximum 限制绑定的核数，让「声明几个 worker」和「允许跑几个核」一致。
    列表里只保留 taskset 实际能绑上的编号：绑一个被 cgroup 屏蔽的核会让
    taskset 直接报错退出，整轮渲染随之失败，这比不绑核糟糕得多。
    """
    # 必须先过滤出「此刻真能绑」的核，再按 worker 数截断。顺序反了的话，
    # 一个被屏蔽的核会占掉名额，剩下的 worker 反而绑不上任何大核。
    def bindable(cores):
        if not cores:
            return False
        try:
            probe = subprocess.run(["taskset", "-c", ",".join(str(c) for c in cores), "true"],
                                   capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return probe.returncode == 0

    # 按「组合」验证而不是逐个核验证：cgroup 掩码是按进程整体设置的，
    # cpu5、cpu7 单独绑会失败（Invalid argument），但 4,5,6,7 整组能绑。
    # 逐个试绑会误判成只有 2 个大核可用，把 3 个 worker 挤到 2 个核上。
    candidates = big_core_indices(topology)
    if maximum:
        candidates = candidates[:max(1, int(maximum))]
    if bindable(candidates):
        return ",".join(str(c) for c in candidates)
    # 整组绑不上时逐个回退，尽量多留一个大核。
    usable = [c for c in candidates if bindable([c])]
    if maximum:
        usable = usable[:max(1, int(maximum))]
    return ",".join(str(c) for c in usable)


def stable_topology(samples=40, interval=0.05):
    """合并多次物理核探测，取可调度大核数的下界。

    cgroup 屏蔽是间歇性的：同一台设备多数时刻只有 2 个大核可调度，偶尔放出
    第 3 个。这里取多次采样的最小值，得到稳定且不会踩空的预算。
    """
    results = []
    for _ in range(max(1, int(samples))):
        current = physical_topology()
        if current:
            results.append(current)
        if interval and len(results) < samples:
            time.sleep(interval)
    if not results:
        return None
    # 频档与物理大核数取最完整的一份，可调度大核数取最小值。
    base = max(results, key=lambda t: (t.get("big_cores") or 0, t.get("max_freq_khz") or 0))
    merged = dict(base)
    schedulable = [t["big_cores_schedulable"] for t in results if t.get("big_cores_schedulable")]
    if schedulable:
        merged["big_cores_schedulable"] = min(schedulable)
        merged["big_cores_schedulable_max"] = max(schedulable)
    merged["samples"] = len(results)
    return merged


def _memory_calibration_path(project=None):
    """内存实测系数的存放位置，与其它缓存文件同目录。"""
    name = "cvr-memory-calibration.json"
    if project:
        candidate = Path(project) / "node_modules" / ".cache" / name
        if candidate.parent.is_dir() or (Path(project) / "node_modules").is_dir():
            return candidate
    return Path.home() / ".cache" / "cvr" / name


def load_memory_calibration(project=None):
    """读取本机实测的每 worker 内存占用；没有记录时返回 None。

    公式里的「固定开销 + 帧缓冲」是按经验估的，不同设备与 composition 的实际
    占用可能相差数倍。有实测值时优先用实测值，避免继续用估算值决定并行度。
    """
    try:
        data = json.loads(_memory_calibration_path(project).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    per_worker = data.get("bytes_per_worker")
    if not isinstance(per_worker, (int, float)) or per_worker <= 0:
        return None
    return data


def record_memory_calibration(project, observation):
    """记录一次实测的内存占用，并更新本机的每 worker 占用系数。

    observation 需含 peak_system_used_bytes、workers、width、height。
    只有样本足够（worker 数 ≥ 2）时才更新系数：单 worker 的系统峰值里，
    浏览器与 node 的固定开销无法与帧缓冲分离，换算不出可用的量。
    """
    peak = observation.get("peak_system_used_bytes")
    workers = int(observation.get("workers") or 1)
    if not peak or workers < 2:
        return None
    path = _memory_calibration_path(project)
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    samples = data.get("samples")
    if not isinstance(samples, list):
        samples = []
    entry = {
        "peak_system_used_bytes": int(peak),
        "workers": workers,
        "width": observation.get("width"),
        "height": observation.get("height"),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    samples.append(entry)
    data["samples"] = samples[-20:]
    # 系统峰值不是「每 worker 占用 × worker 数」：它含一个与并行度无关的固定
    # 基线（浏览器、node、项目本身），直接 peak/workers 会随 worker 数漂移——
    # worker 少时基线被摊薄，算出的系数反而更大，用它限制并行度会越限越少。
    # 因此按 peak = 基线 + 每worker增量 × workers 做最小二乘，取斜率作为每 worker
    # 增量，截距单独记录。样本只有一种 worker 数时无法分离两者，此时退回
    # peak/workers 并标注为粗略值。
    usable = [(int(s_["workers"]), int(s_["peak_system_used_bytes"]))
              for s_ in data["samples"]
              if s_.get("peak_system_used_bytes") and int(s_.get("workers") or 1) >= 1]
    if usable:
        distinct = {w for w, _ in usable}
        if len(distinct) >= 2 and len(usable) >= 2:
            n_ = len(usable)
            sw = sum(w for w, _ in usable)
            sp = sum(p for _, p in usable)
            swp = sum(w * p for w, p in usable)
            sw2 = sum(w * w for w, _ in usable)
            denom = n_ * sw2 - sw * sw
            if denom:
                slope = (n_ * swp - sw * sp) / denom
                intercept = (sp - slope * sw) / n_
                # 光看斜率为正不够：worker 数变化带来的差值若小于样本自身的波动，
                # 拟合出的斜率是噪声，不是信号。典型情形是容器把内存卡在同一
                # 上限，各次峰值几乎相同，斜率随机落在正负之间——恰好为正时
                # 会被当成有效系数，算出的每 worker 增量小到没有意义，反而让
                # 并行度越开越大。这里要求斜率在量级上站得住。
                # 用残差信噪比判断：斜率在「worker 数跨度」上产生的差值，必须
                # 明显大于拟合残差。只看「占总波动的比例」不够——峰值本身没
                # 怎么变时，分母（波动）也小，比例反而会很高，噪声轻松通过。
                peaks = [p_ for _, p_ in usable]
                workers_span = max(distinct) - min(distinct)
                implied = slope * workers_span
                residual = max(
                    abs(p_ - (intercept + slope * w)) for w, p_ in usable
                ) if usable else 0.0
                # implied 要大于残差的 2 倍，才算窗口效应盖过了噪声。
                signal_ratio = (implied / residual) if residual > 0 else float("inf")
                # 除了统计上的显著性，斜率还必须落在物理上成立的量级：每个
                # worker 至少要装下一个浏览器进程，低于下限说明测到的不是
                # worker 本身的增量，而是容器上限、系统缓存等别的东西。
                plausible = slope >= MEMORY_MIN_BYTES_PER_WORKER
                if slope > 0 and plausible and signal_ratio >= MEMORY_FIT_MIN_SIGNAL_RATIO:
                    data["bytes_per_worker"] = int(slope)
                    data["baseline_bytes"] = int(max(0, intercept))
                    data["model"] = "linear"
                    data["fit_signal_ratio"] = round(signal_ratio, 3)
                    data["based_on_samples"] = n_
                    data["distinct_worker_counts"] = sorted(distinct)
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                    except OSError:
                        return None
                    return data
        ratios = sorted(p / max(1, w) for w, p in usable if w >= 2)
        if ratios:
            # 已经观测到多种 worker 数、但斜率被物理下限或信噪比拒绝时，
            # 退回 peak/workers 作为粗略值：这时的峰值里仍含固定基线，
            # 但多种 worker 数至少让中位数有点参考价值。
            #
            # 只有「单一 worker 数」这一种情况必须特别处理：peak/workers 会把
            # 与并行度无关的固定基线（系统本身、浏览器与 node 的常驻开销）也
            # 摊进每个 worker，高估幅度取决于基线占比——在一台 11.3 GiB 的
            # 机器上测到 2.4 GiB 的「每 worker 占用」。一旦把它写进
            # bytes_per_worker，预算算出的 memory_limit 只剩 1，以后每次都只
            # 开 1 个 worker，而单 worker 样本又被本函数拒绝记录，永远凑不齐
            # 第二种 worker 数来拟合，形成死锁。所以这种情形只保留样本。
            if len({w for w, _ in usable}) < 2:
                data.pop("bytes_per_worker", None)
                data["model"] = "insufficient-samples"
                data["based_on_samples"] = len(ratios)
                data["note"] = "single worker count observed; baseline not separable, keeping samples only"
            else:
                data["bytes_per_worker"] = int(ratios[len(ratios) // 2])
                data["model"] = "per-worker-ratio"
                data["based_on_samples"] = len(ratios)
                data["note"] = "fit rejected; peak/workers is coarse because the baseline is included"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return data
    except OSError:
        return None


def worker_budget(hardware=None, engine="remotion", width=1920, height=1080, requested=None, maximum=None):
    """按 CPU、可用内存、目标尺寸和请求上限计算任务内并行数。"""
    h = hardware or hardware_report()
    cpus = max(1, int(h.get("cpu_count") or 1))
    total = int(h.get("memory_total_bytes") or 0)
    available = int(h.get("memory_available_bytes") or 0)
    reserve = max(2 * 1024**3, int(total * 0.20)) if total else 2 * 1024**3
    pixel_bytes = max(1, int(width)) * max(1, int(height)) * 4
    # 每个 worker 的内存占用 = 浏览器固定开销 + 与分辨率成正比的帧缓冲。
    # 两项相加而非取最大值，使预算随输出尺寸真实增长。
    if engine == "hyperframes":
        overhead = 640 * 1024**2
        frame_buffers = 40
    else:
        overhead = 768 * 1024**2
        frame_buffers = 48
    per_worker = int(overhead + pixel_bytes * frame_buffers)
    # 有本机实测系数时以实测为准：公式只是没有观测数据时的起点，一旦测过真实
    # 占用就应当用真实值，否则并行度仍受估算误差支配。
    calibration = load_memory_calibration(h.get("project") if isinstance(h, dict) else None)
    baseline = 0
    if calibration:
        # 回归拟合的是平均值，单次运行可能更高（实测偏差约 25%）。留出安全余量，
        # 宁可少开一个 worker，也不要 OOM 让整轮渲染失败。
        measured = int(calibration["bytes_per_worker"])
        per_worker = max(per_worker, int(measured * MEMORY_SAFETY_FACTOR))
        # 线性模型下，固定基线已经占掉一部分可用内存，必须先从预算里扣掉，
        # 否则会按「全部可用内存都归 worker 用」来算，仍然超配。
        if calibration.get("model") == "linear":
            baseline = int(calibration.get("baseline_bytes") or 0)
    # 物理大核数决定能力上限：超过大核数的 worker 只会被调度到慢核上。
    topology = h.get("topology") if "topology" in h else physical_topology()
    big = int((topology or {}).get("big_cores") or 0) if topology else 0
    cpu_basis = big if big else max(1, cpus // 2)
    # 不用「当前可调度的大核数」收紧 CPU 上限。该值随 cgroup 掩码变化，同一台
    # 机器相邻两次探测可能不同，用它做上限会让并行度每次运行都不一样；而
    # taskset 对被屏蔽的核是静默丢弃、不会失败，多出的 worker 在少数核上仍能
    # 通过流水线重叠获益。物理大核数是芯片固定属性，是稳定的基准。
    # 内存限制仍然生效：内存不足会直接 OOM，与掩码抖动无关。
    # maximum 为 None 表示用户没有显式设限，此时上限就是本机大核数本身。
    cpu_limit = max(1, cpu_basis if maximum is None else min(int(maximum), cpu_basis))
    memory_limit = max(0, (available - reserve - baseline) // per_worker) if available else 1
    workers = max(1, min(cpu_limit, memory_limit or 1))
    if requested is not None:
        workers = max(1, min(workers, int(requested)))
    return {
        "workers": workers,
        "cpu_limit": cpu_limit,
        "memory_limit": memory_limit,
        "memory_reserve_bytes": reserve,
        "estimated_bytes_per_worker": per_worker,
        "requested_workers": requested,
        "cpu_basis": "big_cores" if big else "cores//2",
        "memory_basis": "measured" if calibration else "estimated",
        "memory_baseline_bytes": baseline,
        "topology": topology,
        "note": "heuristic at job start; application memory use can exceed this estimate",
    }


def effective_worker_ceiling(hardware, topology, maximum=None):
    """并行上限：按芯片物理大核数确定，不受当前负载影响。

    取物理大核数而非可调度大核数，因为后者随负载浮动，会让候选集每次不同。
    物理核数代表芯片并行能力上限，用它生成候选可覆盖真实最优值；跑不动的
    候选由探针测速自行淘汰。

    maximum 为 None 时不额外截断，结果完全来自本机探测。
    """
    topo = topology or {}
    big = int(topo.get("big_cores") or 0)
    if big <= 0:
        # 读不到频档时不做 cpu_count//2 之类的推算：那是又一条针对某类设备的
        # 假设。与 default_workers 口径一致，退到 1。
        basis = 1
    else:
        basis = big
    if maximum is None:
        return max(1, basis)
    return max(1, min(int(maximum), basis))


def _core_cache_path(project=None):
    """核心探测结果的缓存位置：优先项目内，其次用户缓存目录。"""
    name = "cvr-core-profile.json"
    if project:
        candidate = Path(project) / "node_modules" / ".cache" / name
        if candidate.parent.is_dir() or (Path(project) / "node_modules").is_dir():
            return candidate
    return Path.home() / ".cache" / "cvr" / name


def load_core_profile(project=None, hardware=None):
    """读取已缓存的核心画像；版本不符或换了设备时返回 None。"""
    path = _core_cache_path(project)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    # 探测口径变更后旧缓存必须重算，否则会沿用过去偏低或偏高的判断。
    if int(data.get("profile_version") or 0) != CORE_PROFILE_VERSION:
        return None
    h = hardware or hardware_report()
    cached_cores = int((data.get("topology") or {}).get("cores") or 0)
    now_cores = int(h.get("cpu_count") or 0)
    # 核数波动不超过一半视为同一台机器；相差过半说明换了设备。
    if cached_cores and now_cores and min(cached_cores, now_cores) * 2 < max(cached_cores, now_cores):
        return None
    return data


def save_core_profile(project=None, hardware=None, topology=None, workers=None):
    """写入核心画像，之后每次运行直接复用，不再探测。"""
    h = hardware or hardware_report()
    data = {
        "profile_version": CORE_PROFILE_VERSION,
        "cpu_count": int(h.get("cpu_count") or 0),
        "topology": topology if topology is not None else physical_topology(),
        "workers": workers,
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = _core_cache_path(project)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return path
    except OSError:
        return None


def resolve_core_profile(project=None, hardware=None):
    """首次运行探测并存档；之后直接读缓存。返回 (画像, 是否命中缓存)。"""
    h = hardware or hardware_report()
    cached = load_core_profile(project, h)
    if cached:
        # 命中缓存时按当前内存重算，内存是每次任务都可能变化的量。
        budget = worker_budget({**h, "topology": cached.get("topology")})
        cached = {**cached, "workers": budget["workers"]}
        return cached, True
    # 按物理核统计频档，可调度大核数取多次采样的下界，结果稳定且不会踩空。
    topology = stable_topology()
    budget = worker_budget({**h, "topology": topology})
    cached = {
        "cpu_count": int(h.get("cpu_count") or 0),
        "topology": topology,
        "workers": budget["workers"],
    }
    save_core_profile(project, h, topology, budget["workers"])
    return cached, False


def classify_video_length(total_frames):
    """把视频分为 short / long 两类，用于选择不同的渲染方案。

    探针测速显示两者最优 worker 数不同，所以方案必须分开记录，
    不能用一套参数覆盖所有长度。
    """
    return "short" if int(total_frames or 0) <= SHORT_VIDEO_MAX_FRAMES else "long"


def _render_plan_path(project=None):
    """渲染方案文件与核心画像同目录，便于整体迁移或清理。"""
    name = "cvr-render-plan.json"
    if project:
        candidate = Path(project) / "node_modules" / ".cache" / name
        if candidate.parent.is_dir() or (Path(project) / "node_modules").is_dir():
            return candidate
    return Path.home() / ".cache" / "cvr" / name


def load_render_plan(project=None):
    """读取渲染方案文件；不存在或版本不符时返回空骨架。"""
    path = _render_plan_path(project)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"plan_version": RENDER_PLAN_VERSION, "plans": {}, "history": []}
    if int(data.get("plan_version") or 0) != RENDER_PLAN_VERSION:
        return {"plan_version": RENDER_PLAN_VERSION, "plans": {}, "history": []}
    data.setdefault("plans", {})
    data.setdefault("history", [])
    return data


def save_render_plan(project, plan):
    """写回渲染方案文件。history 只保留最近 20 条，避免无限增长。"""
    plan["plan_version"] = RENDER_PLAN_VERSION
    plan["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if len(plan.get("history", [])) > 20:
        plan["history"] = plan["history"][-20:]
    path = _render_plan_path(project)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        return path
    except OSError:
        return None


def plan_for(project, total_frames):
    """取出本次视频长度对应的方案；没有则返回 None。"""
    plan = load_render_plan(project)
    return plan["plans"].get(classify_video_length(total_frames))


def record_plan(project, total_frames, entry, source):
    """把一次测量结果写进方案文件，并记录变更历史。

    source 说明来源（first-run / rvc），便于事后判断某个方案是怎么来的。
    """
    plan = load_render_plan(project)
    kind = classify_video_length(total_frames)
    previous = plan["plans"].get(kind)
    entry = {**entry, "kind": kind, "source": source,
             "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    plan["plans"][kind] = entry
    plan.setdefault("history", []).append({
        "kind": kind, "source": source,
        "previous_workers": (previous or {}).get("workers"),
        "workers": entry.get("workers"),
        "seconds": entry.get("seconds"),
        "at": entry["recorded_at"],
    })
    save_render_plan(project, plan)
    return entry


# 记录本轮由 CVR 新建的探针文件，供 uninstall_probes() 清理。
# key 为目标项目绝对路径，value 为 {"files": [Path, ...]}。
_PROBE_INSTALL_STATE = {}


def _observation_path(project=None):
    """观测记录文件：与方案文件同目录，单独存放以免混淆实测与观察。"""
    name = "cvr-observations.json"
    if project:
        candidate = Path(project) / "node_modules" / ".cache" / name
        if candidate.parent.is_dir() or (Path(project) / "node_modules").is_dir():
            return candidate
    return Path.home() / ".cache" / "cvr" / name


def record_observation(project, observation):
    """记录一次真实渲染的耗时，供日后评估默认值。

    与测速不同，这里不额外渲染任何内容，数据来自用户本来就要执行的那一次。
    记录只追加，不修改方案文件；方案仍由显式测量或默认值决定。
    """
    path = _observation_path(project)
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    entries = data.get("observations")
    if not isinstance(entries, list):
        entries = []
    entry = dict(observation)
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    entries.append(entry)
    # 只保留最近 100 条：这是趋势数据，不需要完整历史。
    data["observations"] = entries[-100:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return path
    except OSError:
        # 记录失败不能影响渲染结果，静默跳过。
        return None


def install_probes(project, max_candidate_workers=None):
    """把探针 composition 装进目标项目，返回真正可用的 composition id 集合。

    探针必须存在于目标项目才能渲染，但用户项目里不会有这两个文件。这里从
    skill 自带的 probe/ 目录复制进去，并补上 Composition 注册。
    返回值以 Root.tsx 里的实际注册结果为准，而不是以「复制过文件」为准——
    否则注册失败时上层会误以为可用，先花几分钟测一轮再全部失败。
    """
    # 探针注册的帧数按候选数计算，调用方负责传入实际值；未传时按单个候选处理。
    max_candidate_workers = max(1, int(max_candidate_workers or 1))
    state = _PROBE_INSTALL_STATE.setdefault(str(Path(project).resolve()), {})
    probe_dir = Path(__file__).resolve().parent.parent / "probe"
    source_root = Path(project) / "src"
    if not source_root.is_dir():
        return set()
    copied = set()
    for component in ("CvrProbeShort", "CvrProbeLong"):
        src = probe_dir / f"{component}.tsx"
        dst = source_root / f"{component}.tsx"
        if not src.is_file():
            continue
        try:
            if not dst.is_file():
                dst.write_text(src.read_text())
                # 只登记本轮新建的文件；用户原有的同名文件不归我们删除。
                state.setdefault("files", []).append(dst)
            copied.add(component)
        except OSError:
            continue
    if not copied:
        return set()
    root = source_root / "Root.tsx"
    if not root.is_file():
        return set()
    try:
        text = root.read_text()
    except OSError:
        return set()
    original = text
    # imports 与 elements 分开收集：混在同一个列表里会让解包逻辑出错。
    imports = []
    elements = []
    for name, frames in (("CvrProbeShort", probe_frame_count("short", max_candidate_workers)),
                         ("CvrProbeLong", probe_frame_count("long", max_candidate_workers))):
        if name not in copied:
            continue
        # 已注册但帧数不同时就地改写，使分段数与实际帧数一致。
        # 注册可能写成单行，也可能是 <Composition / id="X" / durationInFrames={N}
        # 的多行形式，因此中间部分允许跨行匹配。
        existing = re.search(
            r'<Composition(?:(?:"[^"]*"|[^>"])*?)\bid="' + name + r'"'
            r'(?:(?:"[^"]*"|[^>"])*?)\bdurationInFrames=\{(\d+)\}',
            text, re.S)
        if existing:
            if int(existing.group(1)) == int(frames):
                continue
            text = text.replace(existing.group(0),
                                existing.group(0).replace(f"durationInFrames={{{existing.group(1)}}}",
                                                          f"durationInFrames={{{frames}}}"), 1)
            continue
        if f"import {{{name}}}" not in text:
            imports.append(f"import {{{name}}} from './{name}';")
        elements.append((name, frames))
    # 帧数就地更新也会改 text，但没有新增 import/元素，所以这里不能只看
    # imports/elements，必须比较原文是否发生变化。
    changed = bool(imports or elements) or text != original
    if changed:
        # 插入点按项目里 Fragment 的实际缩进定位：写死 4 个空格时，缩进为 2 或
        # 制表符的 Root.tsx 会匹配不到，于是 import 加进去、Composition 却没加，
        # 探针静默失效。这里用正则取出 </> 那一行的真实缩进。
        closing = re.search(r"^([ \t]*)</>", text, re.M)
        if elements and closing is None:
            return set()
        block = ""
        if elements and closing:
            # 缩进取 Fragment 内已有子元素的缩进，没有子元素时才用 </> 的缩进
            # 加一级，避免插入的 Composition 与原有写法的缩进风格不一致。
            fragment_start = text.rfind("<>", 0, closing.start())
            inner = re.search(r"\n([ \t]+)<", text[fragment_start:closing.start()]) if fragment_start != -1 else None
            if inner:
                indent = inner.group(1)
            else:
                base = closing.group(1)
                indent = base + ("  " if "\t" not in base else "\t")
            block = "".join(
                f'{indent}<Composition id="{n}" component={{{n}}} '
                f'durationInFrames={{{f}}} fps={{30}} width={{1080}} height={{1920}} />\n'
                for n, f in elements
            )
        try:
            # 先按偏移插入元素，再插 import：调换顺序会让 closing.start() 基于
            # 旧文本的偏移失效，把 import 插进元素中间，写坏文件。
            if block:
                text = text[:closing.start()] + block + text[closing.start():]
            if imports:
                text = text.replace("import {Composition} from 'remotion';",
                                    "import {Composition} from 'remotion';\n" + "\n".join(imports), 1)
            root.write_text(text)
        except OSError:
            return set()
    # 以文件里的实际注册结果为准。
    try:
        final = root.read_text()
    except OSError:
        return set()
    return {name for name in copied if f'id="{name}"' in final}


def uninstall_probes(project):
    """撤销 install_probes() 对目标项目所做的改动。

    只清理本轮由 CVR 新建的文件和对应的 Composition 注册；用户项目里原本
    就存在的同名文件不动。测速结束后必须调用，否则用户的 src/ 与 Root.tsx
    会残留与项目无关的内容。
    """
    key = str(Path(project).resolve())
    state = _PROBE_INSTALL_STATE.pop(key, None)
    if not state:
        return []
    removed = []
    names = {Path(f).stem for f in state.get("files", [])}
    source_root = Path(project) / "src"
    root = source_root / "Root.tsx"
    # 先从 Root.tsx 移除注册与 import，再删文件：顺序反了会让注册指向
    # 不存在的模块，项目在用户下次渲染时直接报错。
    if root.is_file() and names:
        try:
            text = root.read_text()
            for name in sorted(names):
                # 移除整段 <Composition ... id="X" ... />，兼容多行与单行写法。
                text = re.sub(
                    r'[ \t]*<Composition(?:(?:"[^"]*"|[^>"])*?)\bid="' + name + r'"'
                    r'(?:(?:"[^"]*"|[^>"])*?)/>[ \t]*\n',
                    "", text, flags=re.S)
                # import 行用逐行匹配移除，避免正则中混用两种引号。
                kept = []
                for line in text.splitlines(keepends=True):
                    stripped = line.strip()
                    if stripped.startswith("import") and name in stripped and "./" + name in stripped:
                        continue
                    kept.append(line)
                text = "".join(kept)
            # 移除探针后 Fragment 内可能留下空行；压掉它们，避免还原时多出空白。
            text = re.sub(r"<>[ \t]*\n(?:[ \t]*\n)+", "<>\n", text)
            root.write_text(text)
            removed.append(root)
        except OSError:
            pass
    for path in state.get("files", []):
        try:
            Path(path).unlink()
            removed.append(Path(path))
        except OSError:
            continue
    return removed


def probe_worker_count(project, composition, candidates, runners, timeout=600, failures=None):
    """对每个候选 worker 数跑探针，返回按中位数排序的测量结果。

    runners 是「用指定 worker 数渲染一次探针」的可调用对象，由入口脚本注入，
    避免核心模块反向依赖具体引擎。每个候选跑 PROBE_ROUNDS 轮取中位数。

    failures 若传入列表，失败的候选会以 dict 追加进去（含 worker 数与轮次）。
    测速失败不等同于「这个候选很慢」：把它丢掉会让调用方误以为候选集里没有
    它，从而把「没测到」当成「测过且更差」。调用方应据此在报告里标明缺口。
    """
    results = []
    for workers in candidates:
        times = []
        failed = False
        for round_index in range(max(1, PROBE_ROUNDS)):
            started = time.monotonic()
            ok = runners(int(workers), composition)
            if not ok:
                failed = True
                if failures is not None:
                    failures.append({
                        "workers": int(workers),
                        "round": round_index + 1,
                        "reason": "probe render failed",
                    })
                break
            times.append(time.monotonic() - started)
        if failed or not times:
            continue
        ordered = sorted(times)
        # 取中位数而不是最小值：最小值总是偏向「恰好没被降频」的那一轮，
        # 会让所有候选都报告一个平时达不到的理想值。
        median = ordered[len(ordered) // 2]
        best = ordered[0]
        worst = ordered[-1]
        # 轮次间偏差只作为「这次测量可信度」的提示，不参与排序。
        relative = (worst - best) / median if median > 0 else 1.0
        results.append({
            "workers": int(workers),
            "seconds": round(median, 2),
            "best_seconds": round(best, 2),
            "worst_seconds": round(worst, 2),
            "rounds": len(times),
            "stable": relative <= PROBE_STABILITY_LIMIT,
        })
    if not results:
        return results
    # 按中位数排序，但差距在噪声范围内视为打平，打平时取更少的 worker：
    # 并行度越高，调度和内存开销越大，多出来的 worker 未必有实际收益。
    results.sort(key=lambda item: item["seconds"])
    fastest = results[0]["seconds"]
    tied = [item for item in results
            if item["seconds"] <= fastest * (1 + PROBE_TIE_RATIO)]
    choice = min(tied, key=lambda item: item["workers"])
    return [choice] + [item for item in results if item is not choice]


def probe_frame_count(kind, max_candidate_workers):
    """按最大候选数决定探针帧数，保证每个候选都能真正并行。

    基准帧数够覆盖 4 个候选；最大候选更多时段数不够，就测不出高并行的
    收益，这时按 30 帧/段的下限把探针相应加长。大核数有上限，探针长度
    也因此有上限。
    """
    base = PROBE_FRAMES.get(kind, 120)
    need = min(max(1, int(max_candidate_workers or 1)), MAX_MOBILE_BIG_CORES)
    return max(base, need * PROBE_SEGMENT_MIN_FRAMES)


def probe_candidates(limit):
    """按芯片能力给出 3 个候选 worker 数。

    候选由物理大核数决定：该值不受负载影响，使候选集在同一台设备上保持稳定。

    覆盖规则：上限 3 及以下时返回 1..上限；更高时返回 2、3 与真实上限，
    既覆盖常用档位，也测到该设备的能力上限。
    """
    # 按手机 SoC 的大核上限封顶，避免异常读数把探针长度拉得过大。
    top = min(max(1, int(limit or 1)), MAX_MOBILE_BIG_CORES)
    if top <= PROBE_CANDIDATE_COUNT:
        # 直接返回 1..上限。此处不抬高上限：探针会按 CVR_WORKER_CEILING 压回
        # 实际可用值，重复的候选只会浪费测速时间。
        return list(range(1, top + 1))
    return sorted({2, 3, top})


def count_bindable_big_cores(topology=None):
    """返回 taskset 实际能绑上的大核数量。

    不能只看 taskset 的返回码：对被 cgroup 屏蔽的核，它可能返回成功却把该核
    静默丢弃，导致实际可用核数少于请求值。这里在绑定的子进程里回读真实
    亲和性，以其长度为准。探测只涉及若干次 taskset 调用，不渲染。

    掩码本身会随时间变化，因此结果表示「此刻可绑」而非「永久可绑」。
    """
    indices = big_core_indices(topology)
    if not indices:
        return 0
    try:
        # 一次性绑定整组，再回读子进程真实拿到的核数。
        probe = subprocess.run(
            ["taskset", "-c", ",".join(str(i) for i in indices),
             "python3", "-c", "import os;print(len(os.sched_getaffinity(0)))"],
            capture_output=True, text=True, timeout=10)
        if probe.returncode == 0:
            return max(0, int(probe.stdout.strip() or 0))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    # 整组拿不到结果时逐个试，以回读值累加。
    usable = 0
    for index in indices:
        try:
            probe = subprocess.run(
                ["taskset", "-c", str(index),
                 "python3", "-c", "import os;print(len(os.sched_getaffinity(0)))"],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            usable += max(0, int(probe.stdout.strip() or 0))
    return usable


def default_workers(topology=None, maximum=None):
    """未测量时的默认并行数：本机探测到的物理大核数。

    不依赖任何一台机器的测量结果，也不假定设备规模。首次渲染即可使用，
    无需等待测速。

    基准取物理大核数而不是「可绑定的核数」：后者随 cgroup 掩码变化，同一台
    机器相邻两次探测就可能不同，会让默认值抖动。物理大核数由各核的
    cpuinfo_max_freq 得出，是芯片的固定属性。

    maximum 为 None 表示不设额外上限，完全由探测结果决定：4 大核的设备得到
    4，8 大核的设备得到 8，2 大核的设备得到 2。调用方传入具体数字时才截断，
    用于用户显式限制并行度。这里不写死任何设备规模：写死的值在一类设备上
    正确，在另一类上就是浪费或过载。

    worker 数允许超过当下可绑定的核数：taskset 会静默丢弃被屏蔽的核，进程
    不会失败，而多个 worker 在少数核上仍能通过流水线重叠获得收益。若掩码
    收紧到只剩单核，多出的 worker 只会排队，代价是可预期的变慢而非错误。
    """
    topo = topology if isinstance(topology, dict) else (physical_topology() or {})
    big = int(topo.get("big_cores") or 0)
    if big <= 0:
        # 拿不到频档信息时退到 1：宁可慢，也不要在不确定的情况下抢核。
        return 1
    if maximum is None:
        return max(1, big)
    return max(1, min(int(maximum), big))

def adaptive_segment_frames(total_frames, workers=1, minimum=30, maximum=600):
    """按总帧数分档挑分段大小，减少长片的浏览器冷启动次数。

    固定 90 帧时，3 分钟视频会被切成 60 段，每段都要新建一次浏览器。
    这里让段长随总帧数增长：短片保持小段以免长时间无反馈，长片用大段把
    启动开销摊薄。workers 只影响内存预算，不参与段长计算——按 worker 数
    缩小段长会让短片反而被切得更碎。
    """
    total = int(total_frames or 0)
    if total <= 300:
        size = 90
    elif total <= 900:
        size = 150
    elif total <= 3000:
        size = 300
    else:
        size = 450
    return max(int(minimum), min(int(maximum), size))


def resolve_segment_frames(requested, total_frames, workers=1):
    """手动指定优先；未指定时按总帧数自适应。"""
    if requested:
        return int(requested)
    return adaptive_segment_frames(total_frames, workers)


def parse_fraction(value, name="fps"):
    """接受 29.97、30000/1001 等合法正帧率，返回无损 Fraction。"""
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def media_info(path):
    return json.loads(run_capture(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path]))


def stream(info, kind):
    try:
        return next(s for s in info["streams"] if s.get("codec_type") == kind)
    except StopIteration as exc:
        raise RuntimeError(f"missing {kind} stream") from exc


def decode_and_count(path, audio_count=0):
    """一次解码同时校验可解码性并统计视频帧数。

    原本要跑两遍：ffprobe -count_frames 数帧，再用 ffmpeg 解码校验，
    等于把整片解码两次。这里合并成一次 ffmpeg，从 -progress 读 frame=。
    解码失败时抛出解码器自己的报错信息，比 int() 转换失败更好定位。
    """
    command = ["ffmpeg", "-v", "error", "-xerror", "-i", str(path),
               "-map", "0:v:0", "-f", "null", "-progress", "pipe:1", "-"]
    for index in range(audio_count):
        command += ["-map", f"0:a:{index}", "-f", "null", "-"]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        raise RuntimeError(f"{path}: decode failed: {detail[-1] if detail else completed.returncode}")
    frames = None
    for line in completed.stdout.splitlines():
        if line.startswith("frame="):
            frames = int(line.split("=", 1)[1])
    return frames


def verify_video(path, expected=None, decode=True):
    """最终文件逐项核对：容器、实际解码帧数、fps、尺寸和音轨参数。"""
    info = media_info(path)
    video = stream(info, "video")
    result = {"path": str(path), "duration": float(info["format"]["duration"]), "video": video}
    for field in ("width", "height"):
        if expected and field in expected and int(video[field]) != int(expected[field]):
            raise RuntimeError(f"{path}: {field}={video[field]} != {expected[field]}")
    if expected and "fps" in expected:
        actual = parse_fraction(video["avg_frame_rate"])
        requested = parse_fraction(expected["fps"])
        # 比较小数 fps 与其有理数表示。
        equivalent = actual == requested or abs(float(actual) - float(requested)) <= 0.0001
        if not equivalent:
            raise RuntimeError(f"{path}: fps={actual} != {expected['fps']}")
    audio_streams = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    # rvc 需要完整解码时，用同一次解码同时拿到帧数，避免重复解码整片。
    decoded_count = decode_and_count(path, len(audio_streams)) if decode else None
    if expected and "frames" in expected:
        if decoded_count is not None:
            count = decoded_count
        else:
            count_text = run_capture(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", path]).strip()
            count = int(count_text)
        if count != int(expected["frames"]):
            raise RuntimeError(f"{path}: decoded frames={count} != {expected['frames']}")
        result["decoded_frames"] = count
    if expected and "duration" in expected:
        # 允许一个 AAC 包的封装尾差。
        tolerance = 1024 / 48000 + 0.001
        if abs(result["duration"] - float(expected["duration"])) > tolerance:
            raise RuntimeError(f"{path}: container duration={result['duration']} != {expected['duration']}")
    if expected and expected.get("require_audio"):
        if not audio_streams:
            raise RuntimeError(f"{path}: required audio track missing")
        audio = audio_streams[0]
        if expected.get("sample_rate") and int(audio["sample_rate"]) != int(expected["sample_rate"]):
            raise RuntimeError(f"{path}: sample_rate={audio['sample_rate']} != {expected['sample_rate']}")
        if expected.get("channels") and int(audio["channels"]) != int(expected["channels"]):
            raise RuntimeError(f"{path}: channels={audio['channels']} != {expected['channels']}")
        result["audio"] = audio
    if expected and "start_time_tolerance" in expected:
        tolerance = float(expected["start_time_tolerance"])
        for media_stream in info["streams"]:
            start = float(media_stream.get("start_time", 0))
            if abs(start) > tolerance:
                raise RuntimeError(f"{path}: stream start_time={start} exceeds {tolerance}")
    result["format"] = info["format"]
    result["streams"] = info["streams"]
    return result


def quick_browser(project=None):
    """快速返回浏览器启动器，不启动浏览器探测。"""
    scripts = Path(__file__).resolve().parent
    launcher = scripts / "chrome-video-no-webgl.sh"
    shell = scripts / "headless-shell-no-webgl.sh"
    if os.access(launcher, os.X_OK):
        return str(launcher)
    if os.access(shell, os.X_OK):
        return str(shell)
    return None

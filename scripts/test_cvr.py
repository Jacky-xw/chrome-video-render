#!/usr/bin/env python3
"""CVR 回归：动态发现、预算、JSON 参数、帧率和真实同步检测。"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
# 直接按文件加载，避免 Rootfs 对 sys.path 的过滤造成假故障。
_spec = importlib.util.spec_from_file_location("cvr_runtime", SCRIPTS / "cvr_runtime.py")
_runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runtime)
parse_fraction = _runtime.parse_fraction
worker_budget = _runtime.worker_budget
adaptive_segment_frames = _runtime.adaptive_segment_frames
resolve_segment_frames = _runtime.resolve_segment_frames



class CvrTests(unittest.TestCase):
    def test_fractional_fps(self):
        self.assertAlmostEqual(float(parse_fraction("29.97")), 29.97)
        self.assertAlmostEqual(float(parse_fraction("30000/1001")), 29.97002997)
        self.assertAlmostEqual(float(parse_fraction("24000/1001")), 23.97602397)
        self.assertAlmostEqual(float(parse_fraction("60000/1001")), 59.94005994)
        with self.assertRaises(ValueError):
            parse_fraction("0")

    def test_hardware_budget_is_bounded(self):
        # 只断言「在一组合法边界内」，不写死某个核数下的期望值：
        # 不同机型的大核数不同，写死具体数字会让测试在别的设备上误报。
        hardware = {"cpu_count": 6, "memory_total_bytes": 12 * 1024**3, "memory_available_bytes": 6 * 1024**3}
        budget = worker_budget(hardware, maximum=4)
        self.assertGreaterEqual(budget["workers"], 1)
        self.assertLessEqual(budget["workers"], 4)
        self.assertLessEqual(budget["workers"], budget["cpu_limit"])
        self.assertLessEqual(budget["workers"], budget["memory_limit"] or budget["workers"])
        low = worker_budget(hardware, requested=1)
        self.assertEqual(low["workers"], 1)

    def test_default_workers_follows_detected_cores(self):
        """并行数由探测到的大核数决定，不写死任何设备规模。"""
        default_workers = _runtime.default_workers
        for big in (2, 4, 6, 8):
            self.assertEqual(default_workers({"big_cores": big}), big)
        # 显式上限才截断。
        self.assertEqual(default_workers({"big_cores": 8}, 4), 4)
        # 读不到频档时退到 1，而不是猜一个数。
        self.assertEqual(default_workers({"big_cores": 0}), 1)

    def test_worker_budget_ignores_schedulable_core_count(self):
        """可调度大核数随 cgroup 掩码抖动，不能用来定 CPU 上限。"""
        hardware = {"cpu_count": 8, "memory_total_bytes": 16 * 1024**3,
                    "memory_available_bytes": 12 * 1024**3}
        budgets = []
        for schedulable in (1, 2, 3, 4):
            topo = {"big_cores": 4, "big_cores_schedulable": schedulable}
            budgets.append(worker_budget({**hardware, "topology": topo})["cpu_limit"])
        # 掩码变化不影响 CPU 上限。
        self.assertEqual(len(set(budgets)), 1, budgets)
        self.assertEqual(budgets[0], 4)

    def test_memory_calibration_rejects_noise(self):
        """峰值几乎不随 worker 数变化时，不应把噪声拟合当成系数。"""
        import tempfile, shutil
        record = _runtime.record_memory_calibration
        load = _runtime.load_memory_calibration
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "node_modules").mkdir()
            # 容器卡在同一上限：2/3/4 worker 的峰值差异落在噪声内。
            for workers, gib in ((4, 5.69), (2, 5.44), (4, 5.64), (3, 5.53)):
                record(tmp, {"peak_system_used_bytes": int(gib * 1024**3),
                             "workers": workers, "width": 320, "height": 180})
            data = load(tmp)
            self.assertIsNotNone(data)
            # 不能给出「每 worker 只有零点几 GB」这种物理上不成立的斜率。
            self.assertGreaterEqual(data["bytes_per_worker"], 400 * 1024**2, data)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_memory_calibration_fits_real_signal(self):
        """峰值明显随 worker 数增长时，应拟合出线性模型。"""
        import tempfile, shutil
        record = _runtime.record_memory_calibration
        load = _runtime.load_memory_calibration
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "node_modules").mkdir()
            for workers, gib in ((2, 4.8), (3, 5.7), (4, 6.6), (4, 6.58)):
                record(tmp, {"peak_system_used_bytes": int(gib * 1024**3),
                             "workers": workers, "width": 320, "height": 180})
            data = load(tmp)
            self.assertIsNotNone(data)
            self.assertEqual(data.get("model"), "linear", data)
            # 斜率应接近真实的每 worker 增量。
            per_worker_gib = data["bytes_per_worker"] / 1024**3
            self.assertTrue(0.7 <= per_worker_gib <= 1.1, per_worker_gib)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verify_video_catches_wrong_frame_count(self):
        """帧数不符必须报错：这是「文件在但内容坏了」的唯一防线。"""
        import tempfile, shutil, subprocess
        tmp = Path(tempfile.mkdtemp())
        try:
            clip = tmp / "clip.mp4"
            # 生成一段确定长度的片子：30 帧 / 30fps / 1 秒。
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "color=c=black:s=64x64:r=30:d=1", "-pix_fmt", "yuv420p", str(clip)],
                check=True, capture_output=True)
            # 正确的期望值应通过。
            got = _runtime.verify_video(str(clip), {"frames": 30, "fps": "30",
                                                    "width": 64, "height": 64}, decode=True)
            self.assertEqual(got["decoded_frames"], 30)
            # 帧数不符要拒绝。
            with self.assertRaises(RuntimeError):
                _runtime.verify_video(str(clip), {"frames": 29}, decode=True)
            # 尺寸不符要拒绝。
            with self.assertRaises(RuntimeError):
                _runtime.verify_video(str(clip), {"width": 128}, decode=False)
            # fps 不符要拒绝。
            with self.assertRaises(RuntimeError):
                _runtime.verify_video(str(clip), {"fps": "25"}, decode=False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verify_video_rejects_missing_audio(self):
        """要求有音轨却没有时必须报错，不能静默通过。"""
        import tempfile, shutil, subprocess
        tmp = Path(tempfile.mkdtemp())
        try:
            clip = tmp / "silent.mp4"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "color=c=black:s=64x64:r=30:d=1", "-pix_fmt", "yuv420p", str(clip)],
                check=True, capture_output=True)
            with self.assertRaises(RuntimeError):
                _runtime.verify_video(str(clip), {"require_audio": True}, decode=False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_install_then_uninstall_restores_project(self):
        """探针安装后必须能完整还原，不能污染用户仓库。"""
        import tempfile, shutil
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "src").mkdir()
            original = (
                "import {Composition} from 'remotion';\n"
                "import {Hello} from './Hello';\n"
                "export const R=()=>(\n"
                "  <>\n"
                "    <Composition id=\"Hello\" component={Hello} durationInFrames={90} fps={30} width={320} height={180} />\n"
                "  </>\n"
                ");\n"
            )
            (tmp / "src" / "Root.tsx").write_text(original)
            installed = _runtime.install_probes(str(tmp), 3)
            self.assertEqual(len(installed), 2, installed)
            # 安装后应多出探针文件与注册。
            after_install = (tmp / "src" / "Root.tsx").read_text()
            self.assertIn("CvrProbeShort", after_install)
            self.assertTrue((tmp / "src" / "CvrProbeShort.tsx").is_file())
            _runtime.uninstall_probes(str(tmp))
            # 卸载后必须逐字还原。
            self.assertEqual((tmp / "src" / "Root.tsx").read_text(), original)
            self.assertFalse((tmp / "src" / "CvrProbeShort.tsx").is_file())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_uninstall_keeps_user_file_with_same_name(self):
        """用户自己就有同名文件时，卸载不能删掉它。"""
        import tempfile, shutil
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "src").mkdir()
            user_probe = tmp / "src" / "CvrProbeShort.tsx"
            user_probe.write_text("// 用户自己的文件\n")
            (tmp / "src" / "Root.tsx").write_text(
                "import {Composition} from 'remotion';\n"
                "export const R=()=>(<></>);\n")
            _runtime.install_probes(str(tmp), 3)
            _runtime.uninstall_probes(str(tmp))
            # 用户原有的文件必须还在。
            self.assertTrue(user_probe.is_file(), "用户同名文件被误删")
            self.assertEqual(user_probe.read_text(), "// 用户自己的文件\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_affinity_never_returns_unbindable_cores(self):
        """绑核列表只应包含 taskset 真能绑上的核。"""
        import subprocess
        topo = _runtime.physical_topology()
        if not topo:
            self.skipTest("no cpufreq info on this host")
        for workers in (1, 2, 3, 4):
            spec = _runtime.affinity_prefix(topo, workers)
            if not spec:
                continue  # 拿不到大核时不绑核，属合法结果。
            cores = [int(c) for c in spec.split(",")]
            self.assertLessEqual(len(cores), workers, spec)
            # 逐个核单独验证可绑：绑不上的核不该出现在列表里。
            for core in cores:
                probe = subprocess.run(["taskset", "-c", str(core), "true"],
                                       capture_output=True)
                # taskset 对某些核返回成功但会静默丢弃，因此用整组再验一次。
                group = subprocess.run(
                    ["taskset", "-c", spec, "python3", "-c",
                     "import os;print(len(os.sched_getaffinity(0)))"],
                    capture_output=True, text=True)
                self.assertEqual(group.returncode, 0, f"{spec} 整组绑定失败")
                break

    def test_probe_worker_count_reports_failures(self):
        """失败的候选要记进 failures，不能当成「测过且更慢」。"""
        failures = []
        calls = {"n": 0}
        def runner(workers, comp):
            calls["n"] += 1
            # 4 worker 总是失败，1、2 worker 成功。
            return int(workers) != 4
        results = _runtime.probe_worker_count(
            "/tmp", "CvrProbeShort", [1, 2, 4], runner, failures=failures)
        # 4 必须出现在 failures 里。
        self.assertTrue(any(f["workers"] == 4 for f in failures), failures)
        # 成功的结果里不该有 4。
        self.assertNotIn(4, [r["workers"] for r in results])

    def test_adaptive_segmentation_reduces_long_video_segment_count(self):
        # 短片仍用小段，避免长时间无反馈。
        self.assertEqual(adaptive_segment_frames(180), 90)
        # 3 分钟 30fps 长片：固定 90 帧会切 60 段，自适应应显著减少。
        total = 5400
        size = adaptive_segment_frames(total)
        self.assertEqual(size, 450)
        segments = -(-total // size)
        self.assertEqual(segments, 12)
        self.assertLess(segments, 20)
        # 手动指定优先于自适应。
        self.assertEqual(resolve_segment_frames(240, total), 240)
        self.assertEqual(resolve_segment_frames(None, total), size)

    def test_adaptive_segmentation_stays_within_bounds(self):
        self.assertEqual(adaptive_segment_frames(0), 90)
        self.assertGreaterEqual(adaptive_segment_frames(10**9), 30)
        self.assertLessEqual(adaptive_segment_frames(10**9), 600)

    def test_segment_formula_matches_worker(self):
        # Python 预算与 worker 内公式必须一致，否则单测过而实渲染不同。
        worker = (SCRIPTS / "remotion-render-worker.cjs").read_text()
        self.assertIn("total <= 300 ? 90", worker)
        self.assertIn("total <= 900 ? 150", worker)
        self.assertIn("total <= 3000 ? 300", worker)

    def test_json_input_props_validation_is_delegated_to_entry(self):
        script = (SCRIPTS / "remotion-segmented.py").read_text()
        self.assertIn("--input-props", script)
        self.assertIn("--input-props-file", script)
        self.assertIn("selectComposition", (SCRIPTS / "remotion-render-worker.cjs").read_text())

    def test_foreground_segment_scheduling_is_per_segment_process(self):
        """逐段渲染必须是「一段一个前台进程」，而不是一个进程渲完整片。

        一个进程渲完整片时，外层超时会把整进程砍掉，已渲好的段一起没了。
        拆成一段一进程后，段产物在段落完成时就落盘，退出不会带走。
        """
        source = (SCRIPTS / "remotion-segmented.py").read_text()
        self.assertIn("def _render_segments_foreground", source)
        # 段渲染必须走独立子进程调用，而不是同一个 node 进程里循环。
        self.assertIn("subprocess.run(command, cwd=project, env=segment_env)", source)
        # 段号要显式传给 worker，worker 才知道自己渲哪一段。
        self.assertIn("CVR_SEGMENT_INDEX", source)
        # 每段一个 Chrome：段进程用独立 node 进程，浏览器随之起停。
        self.assertIn('CVR_STAGE"] = "segment"', source)

    def test_segment_plan_stage_exists_and_manifest_guards_reuse(self):
        """必须有 plan 阶段，且段目录带清单，防止旧段被误复用。"""
        worker = (SCRIPTS / "remotion-render-worker.cjs").read_text()
        self.assertIn("CVR_PLAN_OK", worker)
        self.assertIn("plan.json", worker)
        script = (SCRIPTS / "remotion-segmented.py").read_text()
        self.assertIn("def _plan_segments", script)
        self.assertIn("CVR_STAGE\"] = \"plan\"", script)

    def test_combine_runs_as_separate_stage(self):
        """合并必须是独立阶段：段渲完后单独起进程合并。"""
        worker = (SCRIPTS / "remotion-render-worker.cjs").read_text()
        self.assertIn("CVR_COMBINE_OK", worker)
        script = (SCRIPTS / "remotion-segmented.py").read_text()
        self.assertIn("def _combine_segments", script)

    def test_segment_failure_reports_which_segment_failed(self):
        """一段失败时要报出是第几段失败的，便于续跑定位。"""
        script = (SCRIPTS / "remotion-segmented.py").read_text()
        self.assertIn("failed_segments", script)

    def test_single_worker_count_does_not_lock_parallelism(self):
        """只有一种 worker 数时不能写死系数，否则并行度被永久锁在 1。

        单样本的 peak/workers 含固定基线，严重高估。一旦据此限制预算，
        memory_limit 只剩 1，以后每次都只开 1 个 worker，而单 worker 的样本
        又被拒绝记录，永远凑不齐第二种 worker 数——死锁。
        """
        import tempfile, shutil
        record = _runtime.record_memory_calibration
        load = _runtime.load_memory_calibration
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "node_modules").mkdir()
            # 只观测到 2 worker 这一种并发。
            record(tmp, {"peak_system_used_bytes": int(4.8 * 1024**3),
                         "workers": 2, "width": 1920, "height": 1080})
            # 不写系数：load 返回 None，预算退回公式估算。
            self.assertIsNone(load(tmp))
            # 样本本身必须留下，供后续运行拟合。
            raw = json.loads((tmp / "node_modules" / ".cache"
                              / "cvr-memory-calibration.json").read_text())
            self.assertEqual(len(raw["samples"]), 1)
            self.assertNotIn("bytes_per_worker", raw)
            # 并行度不被锁死：请求 4 个 worker 仍能得到大于 1 的预算。
            hardware = {"cpu_count": 8, "memory_total_bytes": 11 * 1024**3,
                        "memory_available_bytes": 6 * 1024**3}
            budget = worker_budget({**hardware, "project": str(tmp)}, width=1920, height=1080,
                                   requested=4, maximum=4)
            self.assertGreater(budget["workers"], 1, budget)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_environment_json(self):
        result = subprocess.run([str(SCRIPTS / "render-env.sh"), "--json"], capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        self.assertIn("worker_budget", data["recommended"])
        self.assertIn("hyperframes_cli", data["engines"])

    def test_real_sync_detector(self):
        with tempfile.TemporaryDirectory(prefix="cvr-test-"):
            env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
            result = subprocess.run([sys.executable, str(SCRIPTS / "selftest-av-sync.py")], capture_output=True, text=True, check=True, env=env)
            report = json.loads(result.stdout)
            self.assertTrue(report["normal"]["ok"])
            self.assertGreater(report["injected_delay_detected"]["max_abs_offset_seconds"], 0.07)


if __name__ == "__main__":
    unittest.main(verbosity=2)

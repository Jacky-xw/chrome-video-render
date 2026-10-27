#!/usr/bin/env python3
"""生成独立诊断信号，验证同步检测器能识别正常和延迟输入。"""
import argparse
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np

# Rootfs 会过滤模块搜索路径，按真实文件位置直接加载。
_spec = importlib.util.spec_from_file_location("cvr_check_av_sync", Path(__file__).resolve().with_name("check-av-sync.py"))
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
check = _module.check

FPS = 30
SR = 48000


def generate(directory, duration=4, offsets=None):
    """同一时刻开始一个短脉冲与一帧黑色闪帧，供解码端重新测量。"""
    duration = float(duration)
    offsets = offsets or [0.5, 1.5, 2.5, 3.5]
    sample_count = round(duration * SR)
    times = np.arange(sample_count) / SR
    audio = np.zeros(sample_count, dtype=np.float64)
    for t in offsets:
        mask = (times >= t) & (times < t + 0.04)
        audio[mask] = 0.65 * np.sin(2 * np.pi * 1000 * times[mask])
    wav = directory / "sync.wav"
    mp4 = directory / "sync.mp4"
    raw_audio = (audio * 32767).astype("<i2").tobytes()
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "1",
        "-i", "pipe:0", "-c:a", "pcm_s16le", str(wav),
    ], input=raw_audio, check=True)

    # 直接生成原始灰度闪帧。
    width, height = 64, 36
    black = np.zeros((height, width), dtype=np.uint8)
    white = np.full((height, width), 255, dtype=np.uint8)
    flashes = {round(t * FPS) for t in offsets}
    frames = [black if frame in flashes else white for frame in range(round(duration * FPS))]
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pixel_format", "gray",
        "-video_size", f"{width}x{height}", "-framerate", str(FPS), "-i", "pipe:0",
        "-i", str(wav), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(mp4),
    ], input=b"".join(frame.tobytes() for frame in frames), check=True)
    return mp4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", help="保留诊断文件；默认自动清理")
    args = parser.parse_args()
    context = tempfile.TemporaryDirectory(prefix="cvr-sync-") if not args.work_dir else None
    directory = Path(args.work_dir or context.name)
    directory.mkdir(parents=True, exist_ok=True)
    media = generate(directory)
    report = check(media)
    if not report["ok"]:
        raise SystemExit("CVR sync selftest failed")

    # 用人为延迟验证检测器的失败判定。
    delayed = directory / "delayed.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(directory / "sync.wav"), "-af", "adelay=100", str(delayed)], check=True)
    shifted = directory / "delayed.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-i", str(media), "-i", str(delayed),
        "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest", str(shifted),
    ], check=True)
    delayed_report = check(shifted)
    if delayed_report["ok"] or delayed_report["max_abs_offset_seconds"] < 0.07:
        raise SystemExit("CVR sync selftest could not detect injected offset")
    print(json.dumps({"ok": True, "normal": report, "injected_delay_detected": delayed_report}, ensure_ascii=False, indent=2))
    if context:
        context.cleanup()


if __name__ == "__main__":
    main()

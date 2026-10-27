#!/usr/bin/env python3
"""用已知 PCM 脉冲和对应闪帧测量成片音画偏移。"""
import argparse
import json
import os
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np


def decode_audio(path):
    """转单声道 48 kHz PCM，仅用于时间测量，不回写媒体。"""
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
        "-ac", "1", "-ar", "48000", "-f", "s16le", "-",
    ], capture_output=True, check=True)
    return np.frombuffer(result.stdout, dtype="<i2").astype(np.float64) / 32768, 48000


def decode_luma(path, width=64, height=36):
    """缩小亮度画面用于找闪帧；只读解码，不产生视频。"""
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
        "-vf", f"scale={width}:{height}:flags=area,format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ], capture_output=True, check=True)
    data = np.frombuffer(result.stdout, dtype=np.uint8)
    if len(data) % (width * height):
        raise RuntimeError("incomplete decoded luma frame")
    return data.reshape(-1, height * width).astype(np.float64), width, height


def intervals(mask, rate):
    """布尔序列转非零区间；区间之间没有被忽略的时间。"""
    if not len(mask):
        return []
    indices = np.flatnonzero(np.diff(np.r_[False, mask, False]))
    return [(float(start / rate), float(end / rate)) for start, end in indices.reshape(-1, 2)]


def check(path, fps=None, tolerance_frames=1.0, threshold_db=-40, frame_change=0.12):
    audio, sample_rate = decode_audio(path)
    # 读取用于互相关定位的原始参考脉冲。
    reference_path = Path(os.environ.get("CVR_REFERENCE_PULSE", Path(__file__).resolve().with_name("cvr-reference-pulse.wav")))
    if reference_path == Path(__file__).resolve().with_name("cvr-reference-pulse.wav"):
        # 默认信号从确定参数现场生成，仓库不需要携带二进制音频文件。
        times = np.arange(round(0.04 * sample_rate)) / sample_rate
        reference = 0.65 * np.sin(2 * np.pi * 1000 * times)
        raw = (reference * 32767).astype("<i2").tobytes()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0", "-c:a", "pcm_s16le", str(reference_path)], input=raw, check=True)
    reference, _ = decode_audio(reference_path)
    luma, width, height = decode_luma(path)
    if fps is None:
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate", "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, check=True).stdout.strip()
        fps = Fraction(probe)
    else:
        fps = Fraction(str(fps))
    fps = float(fps)
    # 用 5 ms RMS 窗口寻找脉冲候选。
    window = max(1, round(sample_rate * 0.005))
    usable = len(audio) // window * window
    rms = np.sqrt(np.mean(audio[:usable].reshape(-1, window) ** 2, axis=1) + 1e-30)
    audio_intervals = intervals(rms > 10 ** (threshold_db / 20), sample_rate / window)
    # RMS 只负责找候选窗口，真正位置由下面的原始脉冲互相关给出。
    precise_audio = []
    for start, _ in audio_intervals:
        left = max(0, int(start * sample_rate) - window)
        right = min(len(audio), left + len(reference) + window * 3)
        segment = audio[left:right]
        if len(segment) < len(reference):
            continue
        # 用归一化互相关定位参考脉冲。
        numerator = np.correlate(segment, reference, mode="valid")
        denominator = np.sqrt(np.convolve(segment ** 2, np.ones(len(reference)), mode="valid") * np.sum(reference ** 2) + 1e-30)
        index = int(np.argmax(numerator / denominator))
        correlation = float((numerator / denominator)[index])
        if correlation < 0.5:
            raise RuntimeError(f"calibration pulse does not match reference (correlation={correlation:.3f})")
        precise_audio.append(((left + index) / sample_rate, start, correlation))
    # 用平均亮度差分寻找闪帧。
    delta = np.mean(np.abs(np.diff(luma, axis=0)), axis=1) / 255
    visual_intervals = intervals(delta > frame_change, fps)
    # 将差分索引换算为变化后目标帧的 PTS。
    visual_intervals = [(start + 1 / fps, end + 1 / fps) for start, end in visual_intervals]
    # 校准信号不全时拒绝给出同步结论。
    if not audio_intervals or not visual_intervals:
        raise RuntimeError("no calibration markers found")
    # 每个音频脉冲配对最近的未使用视觉变化。
    used_visual = set()
    offsets = []
    correlations = []
    for audio_start, _, correlation in precise_audio:
        candidates = [(abs(v[0] - audio_start), index, v[0]) for index, v in enumerate(visual_intervals) if index not in used_visual]
        if not candidates:
            raise RuntimeError("visual marker reused or missing")
        _, index, visual_start = min(candidates)
        used_visual.add(index)
        offsets.append(audio_start - visual_start)
        correlations.append(correlation)
    max_offset = max(abs(x) for x in offsets)
    tolerance = tolerance_frames / fps
    # 比较最大偏移与帧级容差。
    passed = max_offset <= tolerance + 1e-9
    return {
        "ok": passed,
        "path": str(path),
        "fps": fps,
        "markers": len(offsets),
        "offsets_seconds": offsets,
        "pulse_correlations": correlations,
        "max_abs_offset_seconds": max_offset,
        "tolerance_seconds": tolerance,
        "audio_threshold_dbfs": threshold_db,
        "visual_mean_luma_change": frame_change,
        "note": "Reference-pulse correlation compared with decoded target-frame PTS",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media")
    parser.add_argument("--fps")
    parser.add_argument("--reference-pulse", help="已知原始 PCM 脉冲；不用于普通未校准视频")
    parser.add_argument("--tolerance-frames", type=float, default=1.0)
    parser.add_argument("--audio-threshold-db", type=float, default=-40)
    parser.add_argument("--visual-change", type=float, default=0.12)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.reference_pulse:
        os.environ["CVR_REFERENCE_PULSE"] = args.reference_pulse
    report = check(args.media, args.fps, tolerance_frames=args.tolerance_frames, threshold_db=args.audio_threshold_db, frame_change=args.visual_change)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else
          f"{'PASS' if report['ok'] else 'FAIL'}: {report['markers']} markers, max offset {report['max_abs_offset_seconds'] * 1000:.2f} ms (limit {report['tolerance_seconds'] * 1000:.2f} ms)")
    if not report["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

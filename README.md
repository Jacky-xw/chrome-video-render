# Chrome Video Render (CVR)：安卓手机上的 Chrome 视频渲染 Skill

**在 Android / proot 环境里，用无头 Chrome 为 Remotion 与 HyperFrames 渲染视频：支持分段并行渲染、PCM 音频拼接与音画同步校验。**

> [English version below](#english)

CVR 是一个给 AI 编程 Agent 用的视频渲染技能（Agent Skill）。它让编程助手和本地视频流水线能够在**安卓手机**、Termux、proot-distro 这类 Linux 环境中直接跑起 Chrome 渲染，而不必把工作目录或浏览器版本写死。它是渲染适配层，不是视频剪辑器，也不是生成式视频模型。

**关键词：** 安卓视频渲染、手机渲染视频、Android 渲染 MP4、proot 渲染、Chrome 无头渲染、Remotion 分段渲染、HyperFrames 渲染、音画同步校验、Claude Code Skill、AI Agent Skill、视频渲染流水线、纯前端视频合成、ffmpeg 拼接。

## 解决什么问题

在手机上渲染视频，难点从来不是"能不能跑起来"，而是这几件事同时成立：

- 安卓的 `/dev/shm` 太小，Chrome 一跑就崩；
- 没有桌面环境，浏览器要自己找、启动参数要自己配；
- 视频一长就要分段，分段之后帧数、音频、时间戳容易错位；
- 手机内存和 CPU 有限，并发多了直接被杀进程。

CVR 把这些都封装成一个可调用的技能，交给 Agent 直接使用。

## 功能特性

- **安卓 / proot 专用无头 Chrome**：私有 `/dev/shm` 挂载命名空间，自动发现浏览器并生成启动器配置。
- **Remotion 分段渲染**：调用官方 `selectComposition()`、`renderMedia()`、`combineChunks()` API；支持小数帧率、JSON props、PCM/MKV 中间产物；分段大小按总帧数自适应，长片不会切成几十个小段。
- **前台逐段渲染，可中断续跑**：每段一个独立进程、一个独立 Chrome，`taskset` 绑到专属大核；段渲完立即写盘并打完成标记，随后进程退出、浏览器关闭。合并单独一个阶段。中途被杀时，已完成的段保留，重跑同一命令自动跳过，只补缺的段；`plan.json` 清单保证改了片子不会误用旧段。
- **HyperFrames 渲染**：保留原生时间线与混音器，支持并行 worker；通过浏览器后端兼容加载器按函数签名发现引擎模块，不依赖某个生成的 chunk 文件名。
- **硬件感知的并行**：按大核数量与可用内存决定 worker 数，核心结构首次探测后缓存复用，并用文件锁防止多个独立任务叠加吃满资源。
- **可测量的音画同步**：用参考 PCM 脉冲互相关对位视觉闪帧时间戳，同时对成片做完整解码与格式校验。
- **可移植的技能结构**：不要求固定在 `/workspace` 下，也不内置任何 composition 或音效设计模板。

## 环境要求

- Linux / Android / proot，且具备 `bash`、`unshare`、`mount`。
- Python 3.10+ 与 NumPy。
- `PATH` 中可用的 FFmpeg 与 ffprobe。
- 一个受支持的本地 Chrome / Chromium，或 Playwright 安装的浏览器。
- Node.js：以 Remotion 自身要求为准；HyperFrames 及其 ESM 加载路径已在 Node 22 验证。
- 你实际使用的那一个引擎：暴露官方 API 的 Remotion 项目，或受支持的 HyperFrames CLI。

CVR 不承诺所有依赖版本或所有安卓设备都兼容，请先阅读[兼容性边界](references/implementation.md)。

## 作为 Skill 安装

把 `chrome-video-render` 放进你的 Agent 技能目录，常见位置是 `~/.claude/skills/chrome-video-render`，或工作区的 `/skills/chrome-video-render`。若宿主需要手动启用，请在 Skills 设置里开启。

从本仓库获取：

```bash
git clone https://github.com/Jacky-xw/chrome-video-render.git
```

如果你装在别处，把下文示例里的 `/skills/chrome-video-render/scripts/` 换成你实际的 `scripts/` 路径。

## 两种启动模式

- `cvr`：轻量默认模式。复用本地启动器，跳过额外的 npm 与浏览器探测，输出简洁结果。
- `rvc`：详细诊断模式。执行完整环境检测、硬件预算明细、逐段校验，以及可选的音画同步测量；结束时打印各阶段耗时，直接指出时间花在哪里。

核心结构只在首次运行时探测，结果缓存到 `node_modules/.cache/cvr-core-profile.json`，之后每次直接复用。`rvc` 会说明本次是 `probed` 还是 `cached`。

## 快速开始

```bash
source /skills/chrome-video-render/scripts/video-browser-env.sh
/skills/chrome-video-render/scripts/render-env.sh --json
```

### Remotion

```bash
python3 /skills/chrome-video-render/scripts/remotion-segmented.py \
  --project /path/to/remotion-project \
  --entry src/index.ts --composition Hello \
  --output /path/to/output.mp4 \
  --workers 2 \
  --input-props '{"title":"Example"}'
```

省略 `--segment-frames` 时会按总帧数自适应分段：短片用 90 帧一段以保证及时反馈，长片自动放大到 300–450 帧，避免切成几十段反复冷启动浏览器。需要固定段长时显式传入即可。

分段产物写在输出同目录的 `<输出名>.segments/`：`NN-video.ts`、`NN-audio.wav`、`NN.json` 完成标记。看已完成段数就知道进度。中断后重跑同一条命令即可续跑，已完成的段会打印 `already done, skipping`。

在专门的校准 composition 上加 `--mode full --sync-check` 可获得详细诊断。

> 给 Agent 的完整操作说明（命令、参数表、故障排查、禁止事项）见 [SKILL.md](SKILL.md)。

### HyperFrames

项目审核通过后：

```bash
python3 /skills/chrome-video-render/scripts/hyperframes-render.py \
  --project /path/to/hyperframes-project \
  --output /path/to/output.mp4 \
  --quality looks --workers 2
```

`--sync-check` 需要配合专门的校准 composition。`check-av-sync.py` 只判断音画是否对得上，不评价音效在审美或语义上是否恰当。

## 测试

```bash
cd /skills/chrome-video-render/scripts
python3 -m unittest -v test_cvr.py test_parse_composition.py
python3 selftest-av-sync.py
```

## 文档

- [技能说明](SKILL.md)
- [实现与版本边界](references/implementation.md)
- [浏览器后端](references/rendering-policy.md)
- [后端说明](references/no-webgl-decision.md)
- [维护指南](MAINTAINING.md)
- [更新日志](CHANGELOG.md)

## 实测基线

已在 Android / proot aarch64 环境验证：Remotion 4.0.538 与 HyperFrames 0.8.198，覆盖小数帧率、两个 worker、参数透传、有声与静音 composition，以及同步校验。另外单独验证了 HyperFrames 0.8.170 的模块发现路径，该版本生成的 chunk 文件名不同。

自适应分段的效果取决于总帧数：以 1200 帧为例，固定 90 帧会切成 14 段，自适应分档后只需 4 段，两者解码帧数都是 1200。这是用来解释档位怎么起作用的例子，不是基准数字。

worker 数由本机探测到的物理大核数决定，不预设任何设备规模。默认路径不测速：首次运行探测物理核心结构（缓存到 `node_modules/.cache/cvr-core-profile.json`，之后复用），按大核数定并行段数，可用内存不足时才下调，然后直接开跑。加 `--measure` 才做测速：按大核数生成候选、逐个多轮实测，短视频与长视频各记一套方案（`cvr-render-plan.json`）；差距小于 5% 视为打平并取较少的 worker，因为并行度越高、调度与内存开销越大。

每 worker 的内存占用不是套公式，而是由真实渲染实测校准：每次渲染采样自身内存峰值，按 `峰值 = 基线 + 斜率 × worker 数` 拟合（而非简单相除，因为系统峰值含一个不随并行度变化的固定基线）。没有历史数据的设备从估算起步，一旦有一次实测就改用实测值。

哪个 worker 数更快取决于具体设备：同一台机器上，绑核生效前后结论就可能相反。所以这里不给出「N 个 worker 更快」的通用数字；需要按速度择优时用 `--measure` 实测一次。直接指定时用 `--workers`，它优先级最高。

`rvc` 额外打印各阶段耗时，让你看清时间花在哪里；需要重测方案时加 `--measure`。典型构成是：环境探测、Node 启动、`selectComposition`、分段渲染、合并、最终编码、校验；前几项是固定开销，其余才是真正的渲染时间。固定开销的大小随设备、浏览器版本和项目依赖而变化，请以本机 `rvc` 的输出为准。

## 许可

Apache-2.0。Remotion、HyperFrames、Chrome、FFmpeg 等上游项目各自保留其许可与商标。CVR 是独立适配层，不是这些项目的官方发行版。

---

<a id="english"></a>

# Chrome Video Render (CVR): Android Chrome Remotion & HyperFrames Skill

**Android/proot video rendering skill for Remotion and HyperFrames: headless Chrome, parallel rendering, PCM audio assembly, and audio/video sync verification.**

CVR helps coding agents and local video pipelines render on Android and other Linux/proot environments without hardcoding a workspace or a particular browser build. It is a rendering adapter, not a video editor or a generative video model.

## Features

- **Headless Chrome for Android/proot:** private `/dev/shm` mount namespace, browser discovery and launcher configuration.
- **Remotion segmented rendering:** official `selectComposition()`, `renderMedia()` and `combineChunks()` APIs; fractional FPS, JSON props and PCM/MKV intermediate output; segment size adapts to the total frame count so long videos are not split into dozens of tiny segments.
- **Foreground per-segment rendering, resumable after interruption:** one process and one Chrome per segment, pinned to its own big core with `taskset`. A finished segment is written to disk with a completion marker before its process exits. Combining is a separate stage. If the run is killed, completed segments survive and re-running the same command skips them; a `plan.json` manifest prevents stale segments from being reused after an edit.
- **HyperFrames rendering:** native timeline and mixer, parallel workers, and a browser-backend compatibility loader that discovers the installed engine module rather than relying on a generated chunk filename.
- **Hardware-aware parallelism:** worker count follows the big-core count and available memory, with the core topology detected once and reused, plus a lock preventing independent jobs from multiplying resource use.
- **Measurable audio/video sync:** reference PCM pulse correlation against visual flash timestamps, plus full media decoding and format checks.
- **Portable skill layout:** no required `/workspace` checkout path and no built-in composition or sound-design templates.

## Requirements

- Linux/Android/proot with `bash`, `unshare` and `mount`.
- Python 3.10+ and NumPy.
- FFmpeg and ffprobe on `PATH`.
- A supported local Chrome/Chromium or Playwright browser installation.
- Node.js: Remotion's own requirement; HyperFrames and its ESM loader path are validated on Node 22.
- A Remotion project exposing the required official APIs, or a supported HyperFrames CLI, only for the engine you use.

CVR does not claim that every dependency version or Android device is compatible. Review [compatibility boundaries](references/implementation.md).

## Install as a skill

Place `chrome-video-render` in your agent's skill directory, commonly `~/.claude/skills/chrome-video-render` or the workspace's `/skills/chrome-video-render`. Enable it in the host's Skills settings if the host requires an explicit enable action.

From this repository:

```bash
git clone https://github.com/Jacky-xw/chrome-video-render.git
```

Use the `scripts/` path in your checkout instead of the examples' `/skills/chrome-video-render/scripts/` if you installed elsewhere.

## Start modes

- `cvr`: lightweight default. Reuses the local launcher, skips extra npm/browser probes and prints a concise result.
- `rvc`: detailed diagnostics. Runs full environment detection, hardware budget detail, per-segment checks and optional audio/video sync measurement, then prints a per-phase timing summary so you can see where the time actually went.

Core topology is detected on the first run only and cached in `node_modules/.cache/cvr-core-profile.json`; every later run reuses it, and `rvc` reports whether it was `probed` or `cached`.

## Quick start

```bash
source /skills/chrome-video-render/scripts/video-browser-env.sh
/skills/chrome-video-render/scripts/render-env.sh --json
```

### Remotion

```bash
python3 /skills/chrome-video-render/scripts/remotion-segmented.py \
  --project /path/to/remotion-project \
  --entry src/index.ts --composition Hello \
  --output /path/to/output.mp4 \
  --workers 2 \
  --input-props '{"title":"Example"}'
```

Omit `--segment-frames` to let CVR size segments from the total frame count: short videos stay at 90 frames for fast feedback, long videos grow to 300-450 frames so they are not split into dozens of segments with a browser relaunch each time. Pass a value explicitly to pin the segment size.

Add `--mode full --sync-check` for detailed diagnostics on a calibration composition.

### HyperFrames

After the project's review and approval:

```bash
python3 /skills/chrome-video-render/scripts/hyperframes-render.py \
  --project /path/to/hyperframes-project \
  --output /path/to/output.mp4 \
  --quality looks --workers 2
```

Use `--sync-check` with a dedicated calibration composition. `check-av-sync.py` does not judge whether sound effects are aesthetically or semantically appropriate.

## Tests

```bash
cd /skills/chrome-video-render/scripts
python3 -m unittest -v test_cvr.py test_parse_composition.py
python3 selftest-av-sync.py
```

## Documentation

- [Skill contract](SKILL.md)
- [Implementation and version boundaries](references/implementation.md)
- [Browser backend](references/rendering-policy.md)
- [Backend notes](references/no-webgl-decision.md)
- [Maintenance](MAINTAINING.md)
- [Changelog](CHANGELOG.md)

## Measured baseline

Validated on Android/proot aarch64 with Remotion 4.0.538 and HyperFrames 0.8.198, including fractional FPS, two workers, parameter forwarding, audio and silent compositions, and sync checks. A separate HyperFrames 0.8.170 module-discovery check covers a different generated chunk filename.

Adaptive segmentation depends on the total frame count: for a 1200-frame composition the fixed 90 frames gives 14 segments while adaptive sizing gives 4, both decoding to exactly 1200 frames. This illustrates how the tiers behave; it is not a benchmark figure.

Worker count follows the detected physical big-core count on the local device; no device size is assumed. The default path does not measure: the first run detects the physical core topology (cached in `node_modules/.cache/cvr-core-profile.json`, then reused), sizes the segment count from the big-core count, trims it only if available memory will not hold it, and starts rendering. `--measure` opts into probing: candidates are derived from the big-core count, each is measured over several rounds, and the result is written to a plan file (`cvr-render-plan.json`) with separate entries for short and long videos. Candidates are compared on the median; gaps under 5% count as a tie and go to the lower worker count, since more parallelism adds scheduling and memory pressure.

Per-worker memory is calibrated from real renders rather than assumed: each render samples its own peak, and the coefficient is fitted as `peak = baseline + slope x workers` rather than a plain division, because the system peak includes a fixed baseline that does not scale with parallelism. A device with no history starts from an estimate and switches to measured values after one real sample.

Which worker count is faster depends on the device: on the same machine the conclusion can flip once core pinning takes effect. So no universal "N workers is faster" figure is given here — use `--measure` once if you want the fastest setting on a given machine. Pass `--workers` to pin a value explicitly; it takes precedence over everything else.

`rvc` prints a per-phase timing summary so you can see where the time went; add `--measure` to re-probe the plans. The phases are environment probe, Node startup, `selectComposition`, segment rendering, combining, the final encode and verification. The first few are fixed overhead and the rest is real render time, but the size of that overhead varies with device, browser build and project dependencies — use your own `rvc` output as the reference.

## License

Apache-2.0. Remotion, HyperFrames, Chrome, FFmpeg and other upstream projects retain their own licenses and trademarks. CVR is an independent adapter, not an official release of those projects.

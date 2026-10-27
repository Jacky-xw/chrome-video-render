---
name: chrome-video-render
description: 视频渲染唯一入口。用户说 cvr / rvc / 渲染 / 出片 / 合成视频 / render 时直接使用本 skill，不要自己写渲染脚本。Android Chrome video rendering adapter for Remotion and HyperFrames. cvr = lightweight render, rvc = full diagnostics. Foreground per-segment processes, one Chrome per segment, adaptive segmentation, resumable on-disk segments, PCM audio assembly, AV sync verification.
compatibility: Requires bash, Python 3.10+, NumPy, FFmpeg/ffprobe, Linux mount namespaces and a local Chrome/Chromium browser. Remotion requires its official Node APIs. HyperFrames compatibility is detected from the installed package. Node 22+ is required for HyperFrames.
---

# Chrome Video Render (CVR)

Android/proot 上的浏览器视频渲染适配层，支持 Remotion 与 HyperFrames。
画面内容、配音、时间轴由视频项目自己负责；**怎么把项目渲成 mp4 由本 skill 负责**。

---

## 0. 给执行者的硬规定（先读这一节，再动手）

1. **渲染只走本 skill 的脚本，不要自己造渲染流程。**
   禁止：自己写 node/python 渲染脚本、直接 `npx remotion render`、自己用
   Playwright/Puppeteer 开浏览器截帧、自己用 ffmpeg 拼图片序列、自己实现分段并行。
   这些路都更慢或更脆，且本 skill 已经解决。
2. **不要重新验证本 skill 已声明的能力。** 第 9 节列了"已解决清单"，
   里面的问题不需要再做实验、写探针、对比基准。直接用人话命令开渲。
3. **不要先做一大轮环境调研。** 不需要先读记忆、翻历史会话、逐个检查
   Chrome/Node/Remotion 版本。直接跑第 1 节的命令；失败了按第 8 节排查表处理，
   排查表能覆盖绝大多数失败。
4. **保持前台。** 两种模式都是前台阻塞执行，独立任务用 flock 串行。
   不要为"稳妥"自己再加一层任务管理、锁、守护进程。
5. **用户说 `cvr` 就用 `--mode quick`，说 `rvc` 就用 `--mode full`。**
   不需要追问"你要哪种渲染方式"。
6. **中断不等于失败重来。** 分段产物已落盘，重跑同一条命令自动续跑（见第 4 节）。
   不要自己再实现一套断点续跑。

---

## 1. 30 秒上手（复制即用）

Remotion 项目（绝大多数情况）：

```bash
cd /path/to/project
source /skills/chrome-video-render/scripts/video-browser-env.sh

# cvr：日常渲染，直接开跑
python3 -u /skills/chrome-video-render/scripts/remotion-segmented.py \
  --project . \
  --composition CvrIntro \
  --output out/video.mp4 \
  --mode quick
```

参数从哪来：

| 参数 | 取值 | 说明 |
|---|---|---|
| `--project` | 项目根目录 | 必须含 `node_modules/remotion`，用项目自己声明的依赖版本 |
| `--composition` | `Composition` 的 `id` | 在 `src/Root.tsx`（或项目对应文件）里找 `<Composition id="..."` |
| `--output` | 任意 mp4 路径 | 父目录会自动创建；分段产物写在同目录的 `<名字>.segments/` |
| `--entry` | 默认 `src/index.ts` | 入口不在默认位置才需要显式传 |
| `--mode` | `quick` = cvr，`full` = rvc | 见第 2 节 |
| `--input-props` | JSON 字符串 | 可选，传给 `selectComposition()` 与所有 `renderMedia()` |
| `--segment-frames` | 整数 ≥5 | 可选，固定段长；省略则自适应（推荐省略） |
| `--workers` | 正整数 | 可选，显式并行段数；省略由机器决定（推荐省略） |

HyperFrames 项目（项目本身已评审并确认使用 HyperFrames 时）：

```bash
python3 -u /skills/chrome-video-render/scripts/hyperframes-render.py \
  --project /path/to/project --output /path/to/output.mp4 \
  --quality looks --workers 2
```

HyperFrames 保留自己的时间轴、混音与 worker 体系，不使用 Remotion 的渲染方案文件。

---

## 2. cvr 与 rvc 的区别

| | `cvr`（`--mode quick`） | `rvc`（`--mode full`） |
|---|---|---|
| 用途 | 日常出片，默认用这个 | 诊断、发布前检查、要看细节 |
| 启动 | 读物理大核数直接开跑，不做测速 | 每次重新测速短/长两套方案并覆盖方案文件（保留 history） |
| 环境探测 | 不跑 `render-env.sh`，不查 npm/浏览器版本 | 跑 `render-env.sh --json`，输出浏览器/引擎/硬件报告 |
| 分段校验 | 只验最终成片 | 逐段解码计数 + 整片完整解码 |
| 音画同步 | `not-measured`，除非显式 `--sync-check` | 支持 `--sync-check` 标记法测量 |
| 结尾输出 | 一行 `RENDER_OK {...}` | 额外打印各阶段耗时汇总 |

**两种模式的渲染调用完全相同**（同一套 bundle/selectComposition/renderMedia/combineChunks），
区别只在环境发现方式、校验深度与报告详略。同一片子两种模式渲染结果一致。

`--measure`：显式开启测速（短/长两类探针 × 候选 worker 数 × 多轮）。
**默认不要加**，它要额外渲染很多次，耗时明显。只有需要重新标定这台机器时才用。

---

## 3. 渲染架构：前台分段渲染（重要，别再用旧印象理解）

每次渲染分三个前台阶段：

**① plan（规划，不渲染）**
一个进程解析 composition，得到 fps/总帧数/分辨率，算出分段范围，
并在分段目录写 `plan.json` 清单（总帧数、fps、宽、高、段长、composition id）。
清单不匹配（改了片长、改了段长、换了 composition）时，旧分段会被判定失效并清除，
**不会把旧片子的段拼进新片子**。

**② segment（逐段渲染）**
**每段一个独立 node 进程，每段一个独立 Chrome。**
流程：开进程 → 起 Chrome → 渲自己那一段 → 视频/音频写盘 → 写 `<NN>.json` 完成标记 → 进程退出 → Chrome 关闭。
段与段之间除了磁盘文件没有任何共享状态，不会跨段累积内存和页面状态。
多段并行，并行数 = worker 数，每段用 `taskset` 绑到专属大核，互不抢核。

**③ combine（合并出片）**
独立进程读取已落盘的全部分段，`combineChunks()` 合成 PCM/MKV，
再一次 ffmpeg 编码成最终 AAC mp4。中间音频始终是 PCM，只有最后一次 AAC，
不会多次有损压缩。

### 段长自适应（省略 `--segment-frames` 时）

| 总帧数 | 段长 |
|---|---|
| ≤ 300 | 90 |
| ≤ 900 | 150 |
| ≤ 3000 | 300 |
| > 3000 | 450 |

长片用更大的段，因为每段都要付一次浏览器启动成本；段切太碎会被启动开销拖慢。

---

## 4. 分段产物、进度与续跑

分段产物目录：**与 `--output` 同目录的 `<输出名去掉扩展名>.segments/`**
（也可用环境变量 `CVR_SEGMENT_DIR` 显式指定）。

例：`--output out/video.mp4` → `out/video.segments/`

```
out/video.segments/
  plan.json          # 分段清单，决定旧段能否复用
  01-video.ts        # 第 1 段视频（H.264 TS）
  01-audio.wav       # 第 1 段音频（PCM）
  01.json            # 第 1 段完成标记（含帧范围、音频布局、路径）
  02-video.ts
  ...
  combined-pcm.mkv   # 合并中间产物
```

**看进度**：数完成标记即可，不需要读日志。

```bash
ls out/video.segments/*.json | grep -v plan.json | wc -l   # 已完成段数
ls out/video.segments/                                     # 看具体完成到哪几段
```

**续跑规则**：
- 某段渲完立即落盘，**之后进程被杀也不会丢这一段**。
- 重跑同一条命令：重新 plan → 已完成段打印 `already done, skipping` → 只补渲缺的段 → 合并。
- 某段渲染失败会停止当前任务，并报出失败的段号与原因；已完成的段保留，下次接着来。
- 想强制全部重渲：删掉 `.segments/` 目录（或改 `--segment-frames` 使清单失效）。

---

## 5. 长任务与"调用方单次命令有时限"的情况

**skill 本身不限时长**，前台阻塞直到出片并验收。限时来自调用方（例如某些 agent
工具单次命令约 600 秒上限），不是 skill 的要求。

遇到时限时按这个顺序处理，**不要改渲染架构**：

1. **直接跑。** 很多片子在时限内就能渲完，先别预设跑不完。
2. **真的被砍了，就重跑同一条命令。** 已完成的段会跳过，等于分段续跑。
   可以反复执行直到 `RENDER_OK`。
3. **想让每次调用都有产出**：把 `--segment-frames` 调小（例如 90/150），
   单段更短、完成标记出现得更密，续跑粒度更细。代价是浏览器启动次数变多。
4. **需要脱离会话长跑时**：用 `setsid ... > out/render.log 2>&1 < /dev/null &`
   让进程独立，然后**只读日志和分段目录**，不要再去杀进程树、不要反复取消重启。
   渲染语义不变，仍是单任务前台分段渲染。
5. **注意工具自身的 timeout 参数**：超过工具上限的值会导致调用直接不执行，
   这不是渲染失败。把 timeout 设在工具允许范围内。

**不要做**：用 `nohup`/`setsid` 起了一堆又不断取消；自己写脚本管理子进程；
因为一次超时就判定 skill 不行然后改写渲染流程。

---

## 6. 并行度、绑核与内存

- **worker 数来自机器，不写死。** 默认 = 芯片物理大核数；有实测方案文件时按方案；
  内存不足时再往下压。`--workers N` 单次显式覆盖，`--max-workers N` 设上限。
- **物理大核数 ≠ 当前可调度大核数。** cgroup 掩码会抖动，所以上限按物理核算，
  绑核用 `taskset` 实际验证；被屏蔽的核由 taskset 静默回退，不会因此失败。
- **每 worker 内存 = 浏览器固定开销 + 与分辨率成正比的帧缓冲。**
  有实测校准数据时用实测（`peak = baseline + slope × workers` 拟合），否则用估算。
- **内存峰值 ≈ 并行段数 × 单段浏览器占用。** 所以 OOM 时第一动作是降 `--workers`。
- **段内 `concurrency` 固定为 1**，不要再调高：CPU 受限设备上多开浏览器页面只会抢核，更慢。

方案与校准文件位置：`<project>/node_modules/.cache/` 下的
`cvr-render-plan.json`（短/长两套方案 + history）、`cvr-core-profile.json`、
`cvr-memory-calibration.json`。删除对应文件即可强制重新探测/标定。

---

## 7. 验收与交付

成功的标志是结尾一行：

```
RENDER_OK {"ok": true, "mode": "quick", "output": ".../video.mp4", "frames": 3960, "fps": "30", "duration": 132}
```

同时生成 `<输出名>.render-report.json`，含帧数、fps、分辨率、时长、音频采样率/声道、
分段数、段长、worker 数、分段目录、各阶段耗时、内存峰值采样等。

需要人工复核时：

```bash
ffprobe -v error -select_streams v:0 \
  -show_entries stream=nb_frames,r_frame_rate,width,height -of default=nw=1 out/video.mp4
ffprobe -v error -select_streams a:0 \
  -show_entries stream=codec_name,sample_rate,channels,duration -of default=nw=1 out/video.mp4
```

**交付时报告**：输出路径 + 帧数/fps/时长 + 是否 `RENDER_OK`。
不必贴大段日志，不必复述渲染原理。

音画同步测量（可选）：`--sync-check` 需要带已知 PCM 脉冲与画面闪光的校准 composition；
`check-av-sync.py` 做参考脉冲相关与闪光时刻检测，`selftest-av-sync.py` 验证检测器本身。
普通内容片不需要做这一步。

---

## 8. 故障排查表（先看表，再决定要不要深入）

| 现象 | 原因 | 处理 |
|---|---|---|
| `CVR_RENDER_FAILED ... "reason":"out_of_memory"`，或退出码 137 / -9 | 内存不足，并行段太多或别的进程占内存 | 降 `--workers`（如 4→2→1），关掉其他大内存进程后重跑；已完成段会保留 |
| `Another CVR job holds ... .foreground.lock` | 同一项目同一 composition 有另一个渲染在跑 | 等它结束；确认已无渲染进程后可删该 `.lock` 文件再跑 |
| `No Chrome detected` | 没 source 浏览器环境，或机器上没有 Chrome/Chromium | 先 `source /skills/chrome-video-render/scripts/video-browser-env.sh`；仍不行用 `--browser` 指定启动器 |
| `Remotion is not installed in target project` | 项目没装依赖 | 在项目里安装依赖（含 `remotion`、`@remotion/cli`），不要用别项目的 node_modules 顶替 |
| `Segments incomplete: N/M ready, missing ...` | 合并时缺段 | 重跑同一命令，会只补缺的段 |
| 某段报 `failed_segments` | 该段渲染失败（浏览器崩溃/OOM/超时） | 看 reason；内存问题降 workers，偶发崩溃直接重跑（已完成的段不重渲） |
| `Segment N timed out` | 单段超过 `CVR_SEGMENT_TIMEOUT_MS`（默认 900s） | 调小 `--segment-frames`，或增大该环境变量 |
| 命令"瞬间结束/没执行"，日志为空 | 调用方 timeout 参数超过工具上限，调用被取消 | 把 timeout 调到工具允许范围内再跑 |
| 外层 `timeout` 导致退出码 124/143 | 被外层限时杀掉，不是渲染逻辑错误 | 重跑同一命令续跑，见第 5 节 |
| 改了片子但结果还是旧内容 | 分段清单仍匹配或 bundle 缓存 | 确认源码已保存；必要时删 `.segments/` 与 `node_modules/.cache/cvr-bundle/` 重渲 |
| 想看详细过程 | quick 模式输出较少 | 用 `--mode full`（rvc）拿逐段与分阶段报告 |

---

## 9. 已解决清单（这些不要再去重新验证/重造）

以下能力已经在本 skill 内实现并测试过。**直接使用，不要写对照实验，不要另起炉灶。**

- Chrome / Headless Shell 启动器，含 `/dev/shm` 的私有 mount namespace 处理
  （Android/proot 必需，不是可选项）。
- 浏览器自动发现（`PLAYWRIGHT_BROWSERS_PATH` / `CVR_BROWSER_ROOT` / 系统浏览器），
  以及 quick/full 两种模式下的启动器选择。
- 物理大核识别（三档频率架构也能正确分类）、`taskset` 绑核、绑不上时的回退。
- worker 数预算：物理核上限 + 内存约束 + 实测内存校准 + 方案文件。
- 短/长视频两套渲染方案的测速、记录与复用（`--measure` 才测）。
- 自适应段长，以及手动固定段长。
- **逐段独立进程 + 独立 Chrome 的前台分段渲染**、分段产物落盘、完成标记、
  `plan.json` 失效保护、中断后续跑。
- 分段 PCM 音频 → `combineChunks()` 无损拼接 → 最终一次 AAC 编码。
- bundle 缓存（按入口源码内容指纹，改动才重新打包）。
- 成片校验（解码帧数、fps、尺寸、音频参数、流起始时间）与可选的音画同步测量。
- 独立任务 flock 串行，避免多个渲染任务叠加打爆机器。

**如果你的需求只是"把项目渲成视频"，上面没有一件事需要你重新实现。**

---

## 10. 典型反例（不要这么做）

- ❌ 用户说"cvr 渲染"，却自己写一套 Playwright 截帧 + ffmpeg 合成的脚本。
- ❌ 直接 `npx remotion render`：没有分段并行、没有绑核、没有内存预算、没有续跑。
- ❌ 渲染前花很长时间读记忆、翻历史会话、做版本与环境普查，迟迟不开渲。
- ❌ 重新做"官方渲染 vs CVR"的基准对比来验证 CVR 是否有用。
- ❌ 因为一次超时/OOM 就改写渲染流程或另起一套实现；按第 8 节处理即可。
- ❌ 默认加 `--measure`：它会做大量额外探针渲染，明显拖慢出片。
- ❌ 把多个渲染任务并行启动"提高速度"：flock 就是用来阻止这种叠加的。
- ❌ 为了提高并行度去改段内 `concurrency`：在 CPU 受限设备上会更慢。

---

## 11. 环境准备（仅在第 8 节指向环境问题时才需要看）

```bash
source /skills/chrome-video-render/scripts/video-browser-env.sh
```

该脚本定位自身安装目录并导出启动器；尊重显式传入的浏览器路径，
否则搜索 `PLAYWRIGHT_BROWSERS_PATH` / `CVR_BROWSER_ROOT` 与系统浏览器。
skill 不在 `/skills` 时，用该 checkout 下的 `scripts/` 目录。

运行要求：bash、Python 3.10+、NumPy、ffmpeg/ffprobe 在 PATH、
Linux mount namespace（`unshare`/`mount`）、本地 Chrome/Chromium、Node（HyperFrames 需 22+）。

实现与兼容性细节见 [references/implementation.md](references/implementation.md)，
浏览器后端见 [references/rendering-policy.md](references/rendering-policy.md)，
后端取舍见 [references/no-webgl-decision.md](references/no-webgl-decision.md)。
**日常渲染不需要读这些文档，第 1 节的命令足够。**

## 12. 验证基线

在 Android/proot aarch64 上，以 Remotion 4.0.538 与 HyperFrames 0.8.198 实测过：
分数帧率、带音频与静音 composition、参数透传、同步检查、逐段前台渲染与中断续跑。
列出版本只是说明"当时试的是什么"，不构成支持版本承诺；
耗时与内存数字只属于产生它们的那台机器，不要当作其他设备的保证。

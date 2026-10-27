> **已废弃。** 本文件是早期版本的实现说明，描述的是单进程整片渲染的旧架构，已不反映当前行为。
> 请阅读 [references/implementation.md](references/implementation.md)，那里记录的是三阶段前台分段渲染的当前实现。

# Implementation and compatibility

## Browser discovery

`video-browser-env.sh` locates its own installation and exports launchers from that directory. It respects explicitly supplied browser paths and otherwise searches `PLAYWRIGHT_BROWSERS_PATH` or `CVR_BROWSER_ROOT`. `cvr_runtime.py` also searches system browser commands and installed Playwright revisions.

Chrome and Headless Shell launchers prepare `/dev/shm` in a private mount namespace. Linux `unshare` and `mount` are therefore runtime requirements, not optional conveniences. On a normal desktop without these Android sandbox restrictions, use the engine's supported local browser path after verifying the same render contract.

## Start modes

`cvr` is the lightweight entry: it uses the installed launcher and project dependencies, skips `render-env.sh` and npm/browser version probes and emits a concise result. `rvc` is the diagnostic entry: full environment JSON, hardware budget detail, per-segment validation and optional sync measurement.

Both engine entries accept the same `--mode`. The start mode never changes the render call itself: the Remotion worker runs the identical `bundle`/`selectComposition`/`renderMedia`/`combineChunks` sequence and the HyperFrames wrapper runs the identical `hyperframes render` command in either mode. Mode only selects how the browser is discovered, how much verification runs and how much is reported. Rendering the same composition in both modes produced byte-identical video in testing.

## Bundle cache

`bundle()` is the slowest fixed cost in a first render (about 8-10s on the measured Android device). The worker hashes the source files next to the entry point and caches the bundle under `<project>/node_modules/.cache/cvr-bundle/<fingerprint>`. A cache hit skips bundling entirely; any content change produces a new fingerprint and rebuilds. Touching a file without changing its content keeps the cache valid, because the fingerprint reads file contents rather than timestamps. Delete that directory to force a rebuild.

## Core profile

Two different numbers matter, and conflating them produced every wrong answer in this area.

**Big-core classification** uses `physical_topology()`, which reads `cpuinfo_max_freq` for every core in `/sys/devices/system/cpu/cpu*` regardless of whether it is currently schedulable. Masked cores still expose their `cpufreq` files, so this yields the chip's true shape: 8 physical cores, 4 of them at or above the performance threshold on the measured device. Classification uses the median frequency rather than a percentage of the fastest core: on a 4x1.8G + 3x2.42G + 1x3.19G chip the fastest-core test wrongly demotes the 2.42G cores to efficiency, while the median keeps them.

**Worker count** is capped by `big_cores_schedulable`: how many of those performance cores the OS will actually let this process run on right now. Cgroup masking is intermittent and biased low. Over a 12-second window `sched_getaffinity()` returned 6 schedulable cores (2 big) 85 times and 7 cores (3 big) only 4 times. `stable_topology()` samples 40 times over ~2s and takes the **minimum**, so the budget never overcommits. Sizing from a lucky maximum is worse than useless: three workers sharing two cores measured 34.6s→42.3s, 18% slower than two workers, with every run actually launching the requested count.

The profile is written to `<project>/node_modules/.cache/cvr-core-profile.json` (falling back to `~/.cache/cvr/`) on first use and read back on every later run, so detection costs one run only. `rvc` reports `probed` or `cached`, plus both core numbers and the resulting worker count.

The cache carries a `profile_version`; a semantics change invalidates old files rather than preserving a stale budget. It does **not** compare the exact core list, since that fluctuates per sample; it only invalidates when the physical core count differs by more than half, which indicates a genuinely different device. Delete the file to force re-detection.

## Segmentation

`--segment-frames` is optional. Without it the worker sizes segments from the composition's real frame count: 90 frames up to 300 frames, 150 up to 900, 300 up to 3000 and 450 beyond, clamped to 30-600. Smaller videos keep small segments so a render shows progress early; long videos use large segments because every segment pays for a fresh browser start. The Python budget helper and the worker must agree on this formula; a unit test asserts both.

## Browser lifecycle

Each Remotion segment creates and releases its own browser. A six-segment job records one launch for composition inspection and six for segments. This avoids memory and page-state accumulation across segments; do not switch to a long-lived shared browser without long-job testing.

## Version boundary

CVR does not pin a project to a single composition, Chrome, Remotion, HyperFrames, Python, npm or Node version. It uses capability detection where possible and fails explicitly when a required API is missing.

- **Composition:** supplied by the caller; there is no built-in composition version.
- **Remotion:** the target project's package must provide official `bundle`, `selectComposition`, `renderMedia` and `combineChunks`, including PCM audio chunks, `separateAudioTo`, frame ranges and the H.264 TS/MKV options used by the adapter. Older versions without those APIs are unsupported until separately tested.
- **HyperFrames:** the CLI must support the flags used by the wrapper. The backend adapter discovers the module by the `readWebGlVendorInfo` and `readWebGlVendorInfoFromCanvas` signatures. A different signature stops the run for review; it is not silently treated as compatible.
- **Chrome/Chromium:** discovered dynamically. Only actual executable paths are accepted. Browser compatibility with the selected engine must be verified after upgrades.
- **Python:** 3.10+ for the runtime code. NumPy is required for sync checks.
- **Node:** Remotion's own version requirements apply. HyperFrames and its ESM loader path are currently validated on Node 22.
- **npm:** project dependencies and the npm cache are discovered rather than tied to `/workspace`. npm itself is only needed when locating a cached HyperFrames CLI.
- **FFmpeg/ffprobe:** required on `PATH`, or through explicit environment configuration.

## Parallel work and audio

`hardware_report()` reads CPU/cgroup and available memory. `worker_budget()` limits workers inside a job, bounded by the big-core count described above. Do not add a `concurrency` increase to "use the cores" — it is measurably slower here (see below). `fcntl.flock` serializes independent jobs on Unix-like systems. These mechanisms are designed for Linux/Android/proot, not a claim of identical behavior on Windows.

Remotion uses the target project's official render APIs. PCM/MKV is the intermediate format; AAC is encoded once for delivery. HyperFrames retains its native timeline and mixer.

## Verification boundary

`verify_video()` checks actual decoded frames, FPS, dimensions, audio parameters and stream starts. `check-av-sync.py` uses reference PCM correlation and visual flash timestamps, not container duration alone. It requires a dedicated calibration composition and cannot infer semantic sound-design correctness.

When `rvc` asks for a full decode, `decode_and_count()` runs one `ffmpeg` pass with `-progress` that both proves the file decodes and returns the video frame count. Counting frames with `ffprobe -count_frames` and then decoding again with `ffmpeg` read the entire output twice for no extra coverage; a corrupt file now reports the decoder's own message instead of an `int()` conversion error. Per-segment frame counting is limited to `rvc`, because `quick` already validates the final whole file and the per-segment pass was duplicate work there.

## Where the time goes

`rvc` records `timings` in the render report and prints a one-line summary. On a 30-frame 1920x1080 composition the phases account for all but about 0.17s of the wall clock, and the shape is consistent: segment rendering dominates, everything else is small.

| Phase | Measured |
|---|---|
| Environment probe (`rvc` only) | ~0.25s |
| Node startup and Remotion module load | ~2.1s |
| Bundle (cache hit) | ~0.01s |
| `selectComposition()` | ~2.0-2.6s |
| Segment rendering | the bulk of the run |
| Combine | ~0.15s |
| Final AAC encode | ~0.2-0.4s |
| Verification (single pass) | ~0.4-0.5s |

Worker scaling on 1200 frames (best of two alternating runs): 1 worker 57.9s, 2 workers 36.4s (1.59x), 3 workers 37.5s (1.54x). Parallel efficiency drops from 80% to 51% at three workers because the third worker is scheduled onto a slow core and because four segments split unevenly across three workers. Two is the sweet spot on this chip.

Two consequences worth recording. First, raising `concurrency` inside `renderMedia()` makes renders *slower* on the measured phone: 300 frames took 24.5s at concurrency 1, 40.1s at 2 and 41.9s at 3, because the device is CPU and thermally bound and extra pages only contend. Keep `concurrency: 1` and spread work across segments instead. Second, wall-clock measurements on this hardware are noisy — the same 1200-frame job was measured anywhere from 66s to 152s — so compare variants in alternating runs and take the minimum rather than trusting a single number.

## Measured baselines

- Android/proot aarch64, Chrome for Testing 156, Headless Shell 131.
- Remotion 4.0.538: 29.97fps, two workers, six segments, props and cross-segment sync.
- HyperFrames 0.8.198: two workers, 29.97fps, 180 frames, audio clips, variables and a separate silent composition.
- HyperFrames 0.8.170 has a different generated chunk filename; release code discovers the backend by function signature instead of assuming that filename.

Segmentation test: a 1200-frame 30fps composition rendered with 14 segments at the fixed 90 frames and 4 segments with adaptive sizing; decoded frame counts were 1200 in both.

Core profile test: the big-core default produced 31-35s on 1200 frames versus 35-41s when three workers were forced, and the profile was `probed` on the first run and `cached` on the next four.

Bundle cache test: a 30-frame composition took 19.3s cold and 12.8s warm on the same device, about 31% saved. A source edit produced a new fingerprint and rebuilt as expected.

These results establish a release baseline for the tested environment, not universal compatibility across all Android devices, dependency versions or long/complex compositions. Test upgrades and long projects before deploying them.

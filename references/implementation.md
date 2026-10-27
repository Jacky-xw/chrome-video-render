# Implementation and compatibility

## Browser discovery

`video-browser-env.sh` locates its own installation and exports launchers from that directory. It respects explicitly supplied browser paths and otherwise searches `PLAYWRIGHT_BROWSERS_PATH` or `CVR_BROWSER_ROOT`. `cvr_runtime.py` also searches system browser commands and installed Playwright revisions.

Chrome and Headless Shell launchers prepare `/dev/shm` in a private mount namespace. Linux `unshare` and `mount` are therefore runtime requirements, not optional conveniences. On a normal desktop without these Android sandbox restrictions, use the engine's supported local browser path after verifying the same render contract.

## Start modes

`cvr` is the lightweight entry: it uses the installed launcher and project dependencies, skips `render-env.sh` and npm/browser version probes and emits a concise result. `rvc` is the diagnostic entry: full environment JSON, hardware budget detail, per-segment validation and optional sync measurement.

Both engine entries accept the same `--mode`. The start mode never changes the render call itself: the Remotion worker runs the identical `bundle`/`selectComposition`/`renderMedia`/`combineChunks` sequence and the HyperFrames wrapper runs the identical `hyperframes render` command in either mode. Mode only selects how the browser is discovered, how much verification runs and how much is reported. Rendering the same composition in both modes produced byte-identical video in testing.

## Render plans

Worker count for Remotion comes from the machine, not from a shipped constant. By default the physical big-core count decides the segment count and rendering starts immediately; `--measure` opts into the probing path described here. Two fixed probe compositions (`CvrProbeShort` and `CvrProbeLong`, both 1080x1920 with heavy per-frame drawing) are rendered at each candidate worker count for `PROBE_ROUNDS` rounds, and the median time is recorded. Probe frames are derived from the candidate count so that every candidate can actually be given distinct segments.

**Two plans, not one.** `classify_video_length()` splits at 300 frames (10s at 30fps, matching the first segmentation boundary). The two length classes can disagree on which worker count is fastest, so a single global number would be wrong for one of them. Which way they disagree depends on the device; the code makes no assumption about the direction.

**Candidates.** `probe_candidates()` returns up to three values derived from the **physical** big-core count: `[1, 2]` when the ceiling is below 3, `[1, 2, 3]` when it is 3, and `{2, 3, ceiling}` above that. The top end is the real ceiling; the fixed lower values keep a mid-range option measured even when the ceiling is high.

Deriving candidates from `big_cores_schedulable` instead is wrong: that number drifts with load, so it can collapse the candidate set and leave higher worker counts never measured at all.

**Scoring.** Each candidate is scored on the median of its rounds, because a single round on a throttling device can deviate sharply and the minimum would report an ideal the device rarely sustains. Round-to-round spread is recorded as a confidence hint but does not reorder candidates: when two candidates differ by only a noise-level margin, demoting one for instability can promote the slower setting. Gaps under `PROBE_TIE_RATIO` count as a tie and go to the lower worker count, since extra parallelism adds scheduling and memory pressure.

**Cost.** The user is told before probing starts. Probe length must be long enough that rendering outweighs per-segment startup; if it is too short, all candidates report the same time and the measurement is worthless.

**Plan file.** `node_modules/.cache/cvr-render-plan.json` (falling back to `~/.cache/cvr/`), holding both plans plus a `history` list of the last 20 writes with previous and new worker counts. It is written only by `--measure`, and read (never written) by ordinary runs. An `--measure` run that finds a different winner overwrites the plan and renders that job with the new numbers, so the change is recorded rather than silent. An explicit `--workers` bypasses all of it and takes precedence.

**Plan precedence.** At render time the recorded plan is used as-is and bounded only by the physical big-core ceiling. It is deliberately not re-clamped by the schedulable core count from the cached profile: that cached figure can be stale, and clamping would silently shrink a measured plan so the probe had no effect. Available memory remains a real constraint and can reduce the count, since running out of memory fails the job outright while a short core list is handled by `taskset` falling back to available cores.

**Probe installation.** Probes live in the skill's `probe/` directory and are copied into the target project's `src/` on first need, along with the `Composition` registration. Existing files are never overwritten, but a registered probe whose frame count no longer matches is corrected in place.

HyperFrames does not use render plans; its worker count comes from the same hardware budget and the same measured memory coefficient, so both engines agree on a given device.

## Bundle cache

`bundle()` is usually the slowest fixed cost in a first render, though how large it is depends on project size and device. The worker hashes the source files next to the entry point and caches the bundle under `<project>/node_modules/.cache/cvr-bundle/<fingerprint>`. A cache hit skips bundling entirely; any content change produces a new fingerprint and rebuilds. Touching a file without changing its content keeps the cache valid, because the fingerprint reads file contents rather than timestamps. `remotion-probe-composition.cjs` reuses the same directory, so reading metadata does not force a second bundle. Delete that directory to force a rebuild.

## Core profile

Two different numbers matter, and conflating them produced every wrong answer in this area.

**Big-core classification** uses `physical_topology()`, which reads `cpuinfo_max_freq` for every core in `/sys/devices/system/cpu/cpu*` regardless of whether it is currently schedulable. Masked cores still expose their `cpufreq` files, so this yields the chip's physical shape, including cores that are not currently schedulable. Classification does not rely on a percentage of the fastest core: on a three-tier chip that test would wrongly demote the mid-tier performance cores to efficiency, while the absolute-plus-relative threshold keeps them.

**The plan probes** are what actually decide worker count. The core profile supplies the search ceiling (`effective_worker_ceiling`) and the memory budget. The schedulable big-core count drifts with load, so it is not used to derive candidates; the physical count is.

The profile is written to `<project>/node_modules/.cache/cvr-core-profile.json` (falling back to `~/.cache/cvr/`) on first use and read back on every later run. `rvc` reports `probed` or `cached`, plus both core numbers.

The cache carries a `profile_version`; a semantics change invalidates old files rather than preserving a stale budget. It does **not** compare the exact core list, since that fluctuates per sample; it only invalidates when the physical core count differs by more than half, which indicates a genuinely different device. Delete the file to force re-detection.

## Segmentation

`--segment-frames` is optional. Without it the worker sizes segments from the composition's real frame count: 90 frames up to 300 frames, 150 up to 900, 300 up to 3000 and 450 beyond, clamped to 30-600. Smaller videos keep small segments so a render shows progress early; long videos use large segments because every segment pays for a fresh browser start. The Python budget helper and the worker must agree on this formula; a unit test asserts both.

## Browser lifecycle and the three-stage render

**Every Remotion render runs as three foreground stages**, and each segment is its own process. This replaced the earlier single-process design, in which one node process rendered every segment and merged them in-process. That design had a fatal interaction with any outer time limit: killing the process also lost the segments it had already rendered, because they lived in a temporary work directory that the process cleaned up. An interrupted render therefore started over from zero.

**Stage 1, `plan`.** One process resolves the composition and prints the segment ranges without rendering. It writes `plan.json` into the segment directory. The manifest is the reuse guard: it holds the total frame count, FPS, width, height, segment size and composition id. When the manifest no longer matches, the stale segments are deleted rather than spliced into the new video. This is what makes "skip completed segments" safe after an edit — reuse is allowed only when the segments still describe the same video.

**Stage 2, `segment`.** One process per segment. Each process starts its own Chrome, renders its own frame range, writes the segment's video and PCM audio to disk, writes a `<NN>.json` completion marker, and exits, which closes the browser. Segments share nothing but the files on disk. Up to `workers` segments run concurrently, each pinned to its own big core with `taskset`, so parallel segments do not fight over the same cores.

**Stage 3, `combine`.** A separate process concatenates the segments that landed on disk and encodes the final delivery file. It refuses to run while segments are missing, and reports which ones.

**Why the browser is per segment.** A segment's browser lives and dies with its process. Nothing accumulates across segments: no memory growth, no page state, no leaked tabs. Do not replace this with a long-lived shared browser without long-job testing.

**Why the segments are on disk, not in a temp directory.** A segment that finishes stays on disk even if the outer process is later killed. Re-running the same command re-plans, prints `already done, skipping` for every completed segment, renders only what is missing, and then combines. Interruption costs at most the one segment that was in flight, not the whole render.

**Where the files are.** `<output directory>/<output name>.segments/` (override with `CVR_SEGMENT_DIR`). Layout: `plan.json`, then per segment `NN-video.ts`, `NN-audio.wav`, `NN.json`, and finally `combined-pcm.mkv`. The completion marker is written last, after both media files exist, so the presence of a marker means the segment is complete and readable.

**Failure behaviour.** A failing segment stops the run and reports which segments failed and why. Rendering further segments would be wasted work, since the combine stage cannot complete without the failed one. Completed segments are kept for the next attempt.

**`all` mode.** The worker also accepts `CVR_STAGE=all`, which renders every segment and combines within one process. It exists for the plan probes, whose clips are short enough that per-process startup would dominate the measurement. Normal renders do not use it.

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

`hardware_report()` reads CPU/cgroup and available memory. `worker_budget()` limits workers inside a job.

Each worker needs a fixed browser overhead plus frame buffers proportional to the frame size, so the estimate is a **sum**, not a max. The earlier `max(baseline, total*0.09, pixels*12)` form let `total*0.09` dominate on a small-memory machine, so every resolution produced the same estimate and high-resolution projects were under-estimated and OOM-killed. The sum therefore grows with output resolution instead of collapsing to a single value.

**The formula is only a starting point.** Every render samples its own peak memory (system used and process RSS, every 250 ms) and stores it in `cvr-memory-calibration.json`. The coefficient is fitted as `peak = baseline + slope × workers` by least squares, not as `peak / workers`: the system peak contains a fixed baseline that does not scale with parallelism, so dividing by the worker count yields a figure that grows as workers shrink, and applying it makes the next run more conservative for no physical reason.

A fit is only accepted when the slope is positive, at least 400 MB per worker (below one headless browser process, the number is measuring something else), and large enough that the change it implies across the observed worker range exceeds the fit residual by 2x. Without these checks a container that caps memory produces near-identical peaks at every worker count, the fitted slope lands wherever the noise puts it, and a spuriously small slope lets the worker count grow on a machine that cannot hold more. Rejected fits fall back to `peak / workers` with a note.

The fitted baseline is subtracted from the available memory before dividing, since the baseline is spent before any worker starts. A 1.3x safety factor is applied to the slope because the fit is a mean and a single run can be higher. The worker's own re-check uses the same measured coefficient, so the two sides agree.

Do not add a `concurrency` increase to "use the cores": on CPU-bound devices extra browser pages contend for the same cores rather than adding throughput. `fcntl.flock` serializes independent jobs on Unix-like systems. These mechanisms are designed for Linux/Android/proot, not a claim of identical behavior on Windows.

Remotion uses the target project's official render APIs. PCM/MKV is the intermediate format; AAC is encoded once for delivery. HyperFrames retains its native timeline and mixer.

## Verification boundary

`verify_video()` checks actual decoded frames, FPS, dimensions, audio parameters and stream starts. `check-av-sync.py` uses reference PCM correlation and visual flash timestamps, not container duration alone. It requires a dedicated calibration composition and cannot infer semantic sound-design correctness.

When `rvc` asks for a full decode, `decode_and_count()` runs one `ffmpeg` pass with `-progress` that both proves the file decodes and returns the video frame count. Counting frames with `ffprobe -count_frames` and then decoding again with `ffmpeg` read the entire output twice for no extra coverage; a corrupt file now reports the decoder's own message instead of an `int()` conversion error. Per-segment frame counting is limited to `rvc`, because `quick` already validates the final whole file and the per-segment pass was duplicate work there.

## Where the time goes

`rvc` records `timings` in the render report and prints a one-line summary, so the breakdown can be read off any run. What the phases are, and how they behave:

| Phase | Nature |
|---|---|
| Environment probe (`rvc` only) | fixed cost, roughly constant per run |
| Node startup and Remotion module load | fixed cost; grows with dependency count |
| Bundle | expensive on first use, near-zero once the cache hits |
| `selectComposition()` | fixed cost; scales with project complexity |
| Segment rendering | dominant term; scales with frames, resolution and per-frame cost |
| Combine | small, grows with segment count |
| Final AAC encode | small, grows with duration |
| Verification (single pass) | small, grows with output length |

Treat the fixed phases as overhead to be paid once per run and segment rendering as the part that responds to tuning. Absolute values are properties of the machine, browser build and project that produced them — read them from your own `rvc` output rather than assuming a figure transfers.

Opting into `--measure` pays for the plan probes, whose duration scales with candidate count and length class; later runs read the recorded plan and skip them.

Two consequences worth recording. First, raising `concurrency` inside `renderMedia()` can make renders *slower* on CPU-bound, thermally limited devices: extra browser pages contend for the same cores instead of adding throughput. The design therefore keeps `concurrency: 1` and parallelises across segments. Second, wall-clock measurements on such hardware are noisy — the same job can vary substantially between runs — so compare variants in alternating runs and rely on a median rather than a single number. This is part of why the probe design uses several rounds.

## Validation scope

The behaviour above was exercised on Android/proot aarch64 with a headless Chrome and is expected to hold on comparable setups; the exact versions and device used for a given check are recorded in the release notes rather than here, because they describe that machine and not a compatibility claim.

Checks that were run, and what each was meant to confirm:

- **Fractional frame rate, props and multi-segment output** on Remotion, including sync across segment boundaries.
- **Audio clips, variables and a silent composition** on HyperFrames.
- **Backend discovery by function signature** rather than by a generated chunk filename, so a different filename between releases does not break loading.
- **Adaptive segmentation** producing fewer segments than a fixed size at the same decoded frame total.
- **Foreground per-segment rendering** — a four-segment job ran as four independent segment processes, each with its own Chrome, followed by a separate combine process; the final file decoded at the expected frame count, FPS and duration with intact audio.
- **Resume after interruption** — with all segments on disk, re-running the same command skipped every segment (`already done, skipping`) and went straight to combine, producing the same output.
- **Manifest invalidation** — the `plan` stage writes `plan.json`, and stale segments are removed when the composition's frame count, FPS, dimensions or segment size change.
- **Plan probing and overwrite** — `rvc` measuring a different winner than a previous run replaces the stored plan and records the change in `history`.
- **Bundle cache** — a cold run versus a cache-hit run, and a content edit invalidating the cache while a metadata-only touch does not.
- **Memory estimate** — the per-worker estimate scales with resolution instead of returning a constant for every size.

None of this is a compatibility guarantee across all Android devices, dependency versions or long/complex compositions. Test upgrades and heavy projects in your own environment before relying on them.

# Changelog

## Unreleased

### Fixed

- **The reported worker count was a combine-stage recomputation, not the real
  parallelism.** The combine stage ran the same worker script, which recomputed
  the budget from the memory available *at that moment*. With memory tight, that
  recomputation collapsed to 1 and overwrote the report, so a render that
  actually ran two or four segments in parallel was recorded as `workers: 1`.
  Memory calibration rejects single-worker samples — the fixed baseline cannot
  be separated from the per-worker increment — so the measured coefficient was
  never stored. The scheduler now passes the segment stage's real concurrency
  through `CVR_EFFECTIVE_WORKERS`, and the combine stage reports that value
  instead of recomputing it.

- **A single observed worker count permanently locked parallelism at 1.** When
  only one worker count had been observed, the coefficient fell back to
  `peak / workers`. That figure includes the baseline that does not scale with
  parallelism: on an 11.3 GiB machine it produced a 2.4 GiB "per-worker" figure
  for a real increment far smaller. Storing it made `memory_limit` come out as
  1, so every later run opened one worker — and single-worker samples are
  rejected, so a second worker count never arrived and the fit could never
  improve. That state now keeps the samples and writes no coefficient, leaving
  the budget on the estimate until two distinct worker counts exist. Fits
  rejected for multiple worker counts still fall back to `peak / workers`, with
  a note saying the baseline is included.

### Changed (rendering)

- **Rendering is now segmented across foreground processes.** A render used to run
  as one node process that rendered every segment and merged them in-process. When
  an outer timeout killed that process, the segments it had already rendered were
  lost with it, so an interrupted render started over from zero. Rendering is now
  three foreground stages: a `plan` stage that resolves the composition and reports
  the segment ranges without rendering, one `segment` process per segment (each
  with its own Chrome, pinned to its own big core with `taskset`, exiting when its
  segment is on disk), and a `combine` stage that merges the segments that landed
  on disk. Segments that finish stay on disk when the run is killed, so re-running
  the same command skips the completed segments and renders only what is missing.
- **Segment reuse is guarded by a manifest.** The `plan` stage writes `plan.json`
  with the frame count, FPS, dimensions and segment size. Stale segments are
  deleted when the manifest no longer matches, so a changed composition cannot
  have old segments spliced into the new video.

### Fixed

- **Probe candidates were capped by the momentary schedulable core count.** `probe_candidates()` derived its ceiling from `big_cores_schedulable`, which drifts with load. A low reading collapsed the candidate set, so higher worker counts were never tested at all. Candidates now come from the physical big-core count: `[1, 2]` below 3, `[1, 2, 3]` at 3, `{2, 3, ceiling}` above.

### Added

- **Per-device render plans measured on first run.** Worker count is no longer inferred from core counts alone. Two fixed probe compositions are rendered at each candidate worker count for several rounds, and the median decides the score; the winner is recorded separately for short videos (<=300 frames) and long videos. Later runs read the plan and start rendering immediately.
- **`rvc` re-measures on every run.** It runs the same two probes, overwrites the shared plan file, and renders that job with the newly measured plan. Every write appends to a `history` list (last 20 entries) recording the previous and new worker count, so a regression is visible rather than silent.
- Probes ship with the skill in `probe/` and are copied into the target project automatically on first need, including the `Composition` registration. Probe length is derived from the candidate count rather than guessed: too short and per-segment startup cost swamps the difference between worker counts, so candidates become indistinguishable.
- Candidates are scored on the median of repeated rounds. Round-to-round spread is reported as a confidence hint but no longer reorders candidates: a small gap between two candidates is noise, and demoting one of them for instability could promote the slower setting. Gaps under 5% count as a tie and go to the lower worker count.
- An explicit `--workers` bypasses probing entirely and takes precedence over the stored plan.
- **No built-in default worker count.** An earlier revision carried per-length constants as a fallback. Those numbers came from one device; shipping them as defaults would apply one machine's measurement to every machine while looking like a deliberate recommendation. The stored plan is the only source.

### Changed (rendering)

- **Worker count no longer assumes a device size.** `--max-workers` defaulted to 4,
  which capped every machine at 4 regardless of how many big cores it has: right on
  the device it was written on, wrong on an 8-big-core phone (half the parallelism)
  and meaningless on a 2-big-core one. It now defaults to the detected physical
  big-core count, and a number still caps when given explicitly.
- **The CPU limit ignores the cgroup mask.** `worker_budget()` took
  `min(physical big cores, schedulable big cores)`, and the schedulable count moves
  while the same machine runs — earlier probes returned 3, 2, 3 in succession. A
  limit built on it makes the worker count differ run to run for no observable
  reason. The physical big-core count comes from each core's `cpufreq` and is fixed.
  taskset drops masked cores silently, so over-asking costs a little queueing, not
  an error.
- **Per-worker memory is measured, not assumed.** The previous figure was a formula
  (768 MiB + 48 frame buffers per pixel) that nothing verified; on the device this
  was tested on it predicted 779 MiB/worker while renders peaked at 1589
  MiB/worker, a 2.1x underestimate that led to an OOM kill. Renders now sample their
  own peak and fit `peak = baseline + slope x workers`, since the system peak
  includes a fixed baseline that does not scale with parallel workers. Fits that are
  statistically significant but physically implausible (below one browser process
  per worker) are rejected rather than used.
- **Result of the NaN in the worker ceiling.** Passing the new `None` default
  through to the worker script put the string `"None"` into `CVR_MAX_WORKERS`, where
  `Number()` turned it into NaN; the segment count came out NaN and the render
  failed while combining audio chunks.
- **Documentation matched to behaviour.** SKILL.md, README.md and
  references/implementation.md described probing on every first run, which stopped
  being true when probing became opt-in via `--measure`.
- HyperFrames rendering now reads the same measured memory coefficient as Remotion
  instead of deciding parallelism by a second, different rule.
- Added tests for the core-count default, mask independence, and both acceptance and
  rejection paths of the memory fit.

- **Short and long videos get separate plans**, split at 300 frames. The two classes can disagree on which worker count is fastest, and which way they disagree depends on the device, so one global number would be wrong for one of the two cases.
- Adaptive Remotion segmentation: when `--segment-frames` is omitted, segment size follows the total frame count (90 up to 300 frames, 150 up to 900, 300 up to 3000, 450 beyond, clamped to 30-600). Longer videos therefore need far fewer segments at the same decoded frame total. Pass `--segment-frames` to pin a fixed size.
- Keep Remotion segment browsers independent: each segment opens and releases its own browser, preventing memory and page-state accumulation across long jobs.
- Keep `cvr` lightweight and `rvc` detailed; browser reuse is not enabled by default because it caused later segments to stall in testing.
- `rvc` verification now decodes once instead of twice: the worker used to run `ffprobe -count_frames` and then a separate `ffmpeg` decode of the same file. A single `ffmpeg` pass with `-progress` now returns the decoded frame count and proves decodability at the same time.
- Per-segment frame counting now runs only in `rvc`, because `quick` already validates the final whole file.
- **Fixed: resolution never affected the worker memory budget.** `per_worker` used `max(baseline, total*0.09, pixels*12)`, and `total*0.09` was always the largest term, so low and high resolutions produced the same estimate and 4K projects would be under-estimated and OOM-killed. Changed to a sum (fixed overhead + frame buffers x pixels), so the estimate now grows with resolution. The worker's own re-check used the same broken formula and was fixed to match.
- Worker count follows the big-core count rather than half the total cores. On a three-tier chip (several efficiency cores, several performance cores, one or two prime cores) a naive half-core rule can place a worker on an efficiency core.
- `render-env.sh` reads the npm cache path from the environment, `~/.npmrc` or the default directory before spawning `npm`, removing a process launch from `rvc` startup.

### Fixed

- Core detection reads every **physical** core, not just schedulable ones, because containers and Android cgroups mask cores intermittently and `sched_getaffinity()` returns different counts from moment to moment. Masked cores still expose their `cpufreq` files, so reading physical cores gives a stable answer.
- The cached core profile carries a `profile_version`, so a semantics change discards old cache files instead of preserving a stale worker count.

## 0.1.0 — 2026-10-26

Initial public release candidate.

- Add portable browser discovery and Android/proot mount-namespace launchers.
- Add hardware-aware foreground parallel rendering with independent-job locking.
- Add Remotion official-API segmented rendering with fractional FPS and JSON props.
- Add PCM/MKV intermediate assembly and one final AAC encode.
- Add HyperFrames native rendering with function-signature-based backend discovery.
- Add decoded media validation and calibration-based audio/video sync checks.
- Add unit tests, portable-checkout render checks and maintenance documentation.

Exercised on Android/proot aarch64 with Remotion 4.0.538 and HyperFrames 0.8.198, plus a separate HyperFrames 0.8.170 module-discovery check. These versions record what was tried; they are not a supported-version claim.

None of this guarantees compatibility with every dependency version or Android device. Upgrade and long-project validation remains the user's responsibility.

# Render probe compositions

Two probe compositions used for first-run worker sizing. They are copied into the
target project's `src/` the first time a probe runs, together with their
`Composition` registration (see `cvr_runtime.install_probes`). Existing files are
never overwritten.

- `CvrProbeShort` — measures the short-video plan, where fixed startup cost is a
  large share of the run.
- `CvrProbeLong` — heavier per-frame workload, measures the long-video plan, where
  segment parallelism dominates.

Both are short in duration and heavy in per-frame work, so that rendering time
outweighs per-segment browser startup. If a probe is too short, every candidate
worker count finishes in about the same time and the measurement cannot tell them
apart.

## Frame counts

The registered frame count is not fixed here: `cvr_runtime.probe_frame_count()`
derives it from the number of candidates, which in turn comes from the detected
big-core count on the machine running the probe. A machine with more big cores
gets more candidates and therefore longer probes, so that each candidate can be
given its own segments rather than sharing one.

`CVR_SEGMENT_FRAMES` is set to `total_frames // candidates`, with a floor of 30
frames. It is computed once from the largest candidate and used for every
candidate, so all candidates do the same amount of work and their timings are
comparable.

## Which values get written

Nothing in this directory records a worker count. The probe only produces timings;
`record_plan()` writes the winner to the plan file in the target project's
`node_modules/.cache/`, per machine. A probe run on one device says nothing about
another, which is why no default count ships with the skill.

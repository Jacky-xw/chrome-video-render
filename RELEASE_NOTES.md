# Chrome Video Render v0.1.0

Android/proot Chrome video rendering skill for Remotion and HyperFrames.

## Highlights

- Headless Chrome browser discovery and Android shared-memory setup.
- Hardware-aware foreground parallel rendering with independent-job locking.
- Remotion official-API segmented rendering with fractional FPS and JSON props.
- PCM/MKV intermediate assembly and one final AAC encode.
- HyperFrames native rendering with backend discovery by function signature, not generated chunk filenames.
- Decoded media checks and calibration-based audio/video sync verification.
- Portable skill layout, validation workflow and maintenance documentation.

## Validated baseline

Tested on Android/proot aarch64 with Remotion 4.0.538 and HyperFrames 0.8.198, including two workers, fractional FPS, parameter forwarding, audio and silent compositions and sync checks. A separate HyperFrames 0.8.170 module-discovery check covers a different generated chunk filename.

## Compatibility scope

This release is a measured baseline, not a guarantee for every dependency version or Android device. Review `references/implementation.md` before upgrading engines, browsers or long/complex compositions.

## Install

Clone `https://github.com/Jacky-xw/chrome-video-render.git` and install it in the host's skill directory. Follow `README.md` and `SKILL.md` for the `scripts/` path, initialization and rendering commands.

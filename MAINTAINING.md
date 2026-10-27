# Maintaining Chrome Video Render

## Repository role

This repository is the public source, release and maintenance home for the `chrome-video-render` skill. Keep the skill name, repository name and directory name identical.

## Update checklist

1. Reproduce issues on a small local composition; do not require private user videos for a bug report.
2. Run shell and Python syntax checks, JavaScript syntax checks, `test_cvr.py`, `test_parse_composition.py` and `selftest-av-sync.py`.
3. Test a real render from a checkout outside `/skills` before changing browser discovery or dependency loading.
4. For Remotion changes, verify fractional FPS, `inputProps`, bounded parallel segments, PCM assembly and sync markers.
5. For HyperFrames changes, discover the installed backend by function signature. Do not reintroduce generated chunk filenames or a hardcoded workspace path.
6. Check that no audio, music or effects are injected into a composition that did not request them.
7. Update `CHANGELOG.md`, the measured-baseline section and any changed requirements.
8. Run a secret scan and full decode of generated test outputs before publishing.

## Release policy

Version tags use `vMAJOR.MINOR.PATCH`. Documentation and test updates use PATCH; backward-compatible capabilities use MINOR; required API or behavior changes use MAJOR. The baseline dependency versions in the README are test evidence, not automatic version pins.

## Compatibility reports

Include the device, architecture, browser, Node, Python, engine and composition type. Distinguish a measured success from an untested assumption. Do not call the adapter universally compatible with every Android device or dependency release.

## Support boundaries

CVR owns browser discovery, launch configuration, bounded foreground rendering and media verification. Remotion and HyperFrames own their composition and audio timeline behavior. Do not move video-content rules or sound-design templates into this infrastructure skill.

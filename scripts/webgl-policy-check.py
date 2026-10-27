#!/usr/bin/env python3
"""按需扫描项目中的 WebGL/Three.js 绘制入口；不随渲染自动执行。"""
import argparse
import json
import re
import sys
from pathlib import Path

CODE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".html", ".htm"}
SKIP_DIRS = {
    ".git", ".agents", ".claude", "node_modules", "dist", "build", "out", "output",
    "renders", "render", "media", ".next", ".cache", ".remotion", "_out", "_run",
}

# 只匹配会实际进入 WebGL/Three 绘制路径的写法；普通 2D Canvas API 不匹配。
PATTERNS = [
    ("three-import", re.compile(r"""(?:from\s*|require\s*\(\s*|import\s*\(\s*)['"](?:three|three/[^'"]+|@react-three/[^'"]+|@remotion/three)['"]""")),
    ("threerenderer", re.compile(r"\b(?:new\s+)?THREE\.WebGLRenderer\b|\bWebGLRenderer\b")),
    ("threecanvas", re.compile(r"\bThreeCanvas\b")),
    ("webgl-context", re.compile(r"\.getContext\s*\(\s*['\"](?:webgl2?|experimental-webgl)['\"]")),
    ("webgl-create", re.compile(r"\b(?:createWebGLContext|WebGL2RenderingContext|WebGLRenderingContext)\b")),
    ("p5-webgl", re.compile(r"\bcreateCanvas\s*\([^)]*\bWEBGL\b|\bcreateCanvas\s*\([^)]*['\"]webgl2?['\"]")),
    ("shader", re.compile(r"\b(?:createShader|shaderSource|compileShader|linkProgram)\b")),
]


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(("//", "#")):
            continue
        lines.append(line)
    return "\n".join(lines)


def scan(project: Path):
    violations = []
    for path in sorted(project.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in CODE_EXTENSIONS:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(project).parts[:-1]):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        code = strip_comments(text)
        for line_no, line in enumerate(code.splitlines(), 1):
            for rule, pattern in PATTERNS:
                if pattern.search(line):
                    violations.append({
                        "file": str(path.relative_to(project)),
                        "line": line_no,
                        "rule": rule,
                        "snippet": line.strip()[:180],
                    })
                    break
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description="禁止 WebGL / Three.js 用于视频绘制")
    parser.add_argument("--project", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"CVR_POLICY_ERROR 项目不存在：{project}", file=sys.stderr)
        return 1
    violations = scan(project)
    report = {
        "ok": not violations,
        "policy": "video rendering must not draw with WebGL or Three.js",
        "allowed_2d_25d": ["Canvas 2D", "p5.js 2D", "Manim (Cairo)"],
        "project": str(project),
        "violations": violations,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif violations:
        print("✗ CVR 永久策略：视频项目禁止 WebGL / Three.js 实际绘制。", file=sys.stderr)
        for v in violations:
            print(f"  {v['file']}:{v['line']} [{v['rule']}] {v['snippet']}", file=sys.stderr)
        print("  2D/2.5D 请改用 Canvas 2D、p5.js 2D 或 Manim（Cairo）。", file=sys.stderr)
    else:
        print("✓ CVR 策略通过：未发现 WebGL / Three.js 绘制入口。")
    return 0 if not violations else 2


if __name__ == "__main__":
    raise SystemExit(main())

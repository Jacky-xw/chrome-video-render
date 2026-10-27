# 图形后端

视频浏览器由统一启动器创建，负责 Android `/dev/shm` 和图形后端配置。

普通 2D/2.5D 项目使用 Canvas 2D、p5.js 2D 或 Manim（Cairo）。预渲染素材可作为视频输入。

`webgl-policy-check.py` 是按需的源码审计工具，不是每次渲染的前置步骤。只有用户明确要求检查图形后端时才运行。

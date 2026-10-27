#!/usr/bin/env python3
"""视频工坊本地浏览器适配：不改上游源码，只把 Playwright Chromium 指向 Android 启动器。"""
import os,runpy,sys
from pathlib import Path

def main():
    root=Path(os.environ['NVS_HOME']) if os.environ.get('NVS_HOME') else Path(__file__).resolve().parents[1] / 'narrated-video-studio'
    if len(sys.argv)<2:
        raise SystemExit('用法：nvs-playwright-launcher.py <build.py|workshop.py|asr.py> [参数…]')
    script=(root/'kit'/sys.argv.pop(1)).resolve()
    if root/'kit' not in script.parents or not script.is_file():
        raise SystemExit('只允许运行视频工坊 kit/ 内的 Python 入口')
    # 使用 CVR 的统一浏览器启动器。
    scripts = Path(__file__).resolve().parent
    os.environ.setdefault('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH', str(scripts / 'headless-shell-no-webgl.sh'))
    from playwright.sync_api import sync_playwright as original_sync
    def configured_sync_playwright():
        manager=original_sync()
        original_start=manager.start
        def start():
            playwright=original_start()
            original_launch=playwright.chromium.launch
            def launch(**kwargs):
                kwargs.setdefault('executable_path',os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'])
                return original_launch(**kwargs)
            playwright.chromium.launch=launch
            return playwright
        manager.start=start
        return manager
    import playwright.sync_api
    playwright.sync_api.sync_playwright=configured_sync_playwright
    sys.path.insert(0,str(script.parent))
    runpy.run_path(str(script),run_name='__main__')
if __name__=='__main__':main()

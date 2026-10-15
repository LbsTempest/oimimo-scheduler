"""Playwright 持久化浏览器会话。平台订单选择器尚未在此实现。"""

import os
import sys
import tempfile
import time
from pathlib import Path


def project_root():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_root():
    if getattr(sys, 'frozen', False) and getattr(sys, '_MEIPASS', None):
        return Path(sys._MEIPASS)
    return project_root()


def configure_project_runtime():
    """把浏览器二进制、Profile、临时目录和 XDG 缓存固定到项目内。"""
    root = project_root()
    resources = resource_root()
    runtime = root / '.runtime'
    paths = {
        'PLAYWRIGHT_BROWSERS_PATH': resources / '.playwright-browsers',
        'TMPDIR': runtime / 'tmp',
        'TMP': runtime / 'tmp',
        'TEMP': runtime / 'tmp',
        'XDG_CACHE_HOME': runtime / 'cache',
        'XDG_CONFIG_HOME': runtime / 'config',
    }
    for name, path in paths.items():
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass
        os.environ[name] = str(path)
    tempfile.tempdir = str(runtime / 'tmp')
    return runtime


def profile_path():
    path = project_root() / '.runtime' / 'mihuashi' / 'browser-profile'
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _launch_context(headless):
    configure_project_runtime()
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path()),
            headless=headless,
        )
    except Exception:
        playwright.stop()
        raise
    return playwright, context


def open_login_window(stop_event):
    """打开人工登录窗口并等待窗口关闭；不读取凭据、页面正文或响应体。"""
    playwright, context = _launch_context(headless=False)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto('https://www.mihuashi.com/', wait_until='domcontentloaded')
        while not stop_event.is_set():
            pages = context.pages
            if not pages or all(page.is_closed() for page in pages):
                break
            time.sleep(0.25)
    finally:
        try:
            try:
                context.close()
            except Exception:
                # 用户关闭持久化 Chromium 窗口后，底层 context 可能已先行关闭。
                pass
        finally:
            playwright.stop()

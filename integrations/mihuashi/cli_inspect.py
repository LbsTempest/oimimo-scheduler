"""开发用米画师浏览器勘察工具；不读取或保存响应体。"""

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .browser import configure_project_runtime
from .profile_lock import ProfileLock


def profile_path() -> Path:
    """返回项目内、被 Git 忽略的私有持久化浏览器 Profile 路径。"""
    project_root = Path(__file__).resolve().parents[2]
    path = project_root / '.runtime' / 'mihuashi' / 'browser-profile'
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def is_target_url(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower()
    return host == 'mihuashi.com' or host.endswith('.mihuashi.com')


def safe_url(url: str) -> str:
    """只打印站点和路径，避免查询参数或片段泄露会话/个人数据。"""
    parts = urlsplit(url)
    host = parts.hostname or ''
    if parts.port:
        host = '{}:{}'.format(host, parts.port)
    safe_segments = []
    for segment in parts.path.split('/'):
        if re.fullmatch(r'\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|[0-9a-fA-F]{24,}', segment):
            safe_segments.append(':id')
        else:
            safe_segments.append(segment)
    safe_path = '/'.join(safe_segments)
    return '{}://{}{}'.format(parts.scheme, host, safe_path)


def run_dry_run(project_root: Path) -> int:
    """采集本人可见订单并只读预览同步动作，不写入订单或外部 ID 映射。"""
    from . import collector
    import db
    from .schemas import ExternalOrder
    from .sync_service import upsert_order

    max_pages = 20
    database_ready = Path(db.DB_PATH).is_file() and Path(db.DB_PATH).stat().st_size > 0
    try:
        with ProfileLock(project_root / '.runtime' / 'mihuashi' / 'profile.lock'):
            orders = collector.collect_orders(
                headless=False,
                max_pages=max_pages,
            )
            counts = {}
            for item in orders:
                if database_ready:
                    result = upsert_order(item, allow_create=True, dry_run=True)
                else:
                    order = item if isinstance(item, ExternalOrder) else ExternalOrder.model_validate(item)
                    fields = ['project_name', 'platform_url', 'page_deadline']
                    if order.quoted_amount is not None:
                        fields.append('quoted_amount')
                    if order.platform_status is not None:
                        fields.append('platform_status')
                    result = {'action': 'would_create', 'fields': fields, 'conflicts': []}
                action = result['action']
                counts[action] = counts.get(action, 0) + 1
                fields = ','.join(result.get('fields', [])) or '无'
                conflicts = ','.join(result.get('conflicts', [])) or '无'
                print('{} fields={} conflicts={}'.format(action, fields, conflicts))
            print('Dry-run 完成：{} 条；汇总 {}'.format(
                len(orders), ', '.join('{}={}'.format(key, value) for key, value in sorted(counts.items())) or '无记录',
            ))
        return 0
    except collector.AdapterNotConfigured as exc:
        print('尚无本人账号实测采集适配器：{}'.format(exc), file=sys.stderr)
        return 5
    except collector.LoginRequired:
        print('登录状态失效，请先运行 login 并由本人重新登录。', file=sys.stderr)
        return 6
    except collector.PlatformAccessBlocked:
        print('平台要求人工检查或限制访问；已停止 dry-run。', file=sys.stderr)
        return 7
    except RuntimeError as exc:
        if 'already locked' in str(exc):
            print('米画师浏览器正在另一项任务中使用，请先关闭当前登录或同步任务。', file=sys.stderr)
            return 3
        raise


def run(mode: str, browser: str = 'chrome') -> int:
    project_root = Path(__file__).resolve().parents[2]
    configure_project_runtime()
    if mode == 'dry-run':
        return run_dry_run(project_root)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('缺少 Playwright。请执行：UV_CACHE_DIR="$PWD/.uv-cache" uv pip install --python .venv/bin/python -r requirements.txt', file=sys.stderr)
        print('安装 Python 包后再执行：python -m playwright install chromium', file=sys.stderr)
        return 2

    profile = profile_path()
    print('浏览器 Profile：{}'.format(profile))
    print('浏览器：{}'.format('系统 Google Chrome' if browser == 'chrome' else '项目内 Playwright Chromium'))
    print('只输出米画师 JSON 响应的请求方法、状态码、路径和 Content-Type；不读取响应体。')
    print('不要在共享日志中记录个人订单信息、Cookie、Authorization 或验证码。')

    try:
        with ProfileLock(project_root / '.runtime' / 'mihuashi' / 'profile.lock'):
            with sync_playwright() as playwright:
                launch_options = {
                    'user_data_dir': str(profile),
                    'headless': False,
                }
                if browser == 'chrome':
                    launch_options['channel'] = 'chrome'
                try:
                    context = playwright.chromium.launch_persistent_context(**launch_options)
                except Exception:
                    if browser == 'chrome':
                        print('无法启动系统 Google Chrome。请确认 Chrome 已安装，或使用 --browser chromium。', file=sys.stderr)
                    else:
                        print('无法启动项目内 Chromium。请在项目目录运行：uv run --no-project --python .venv/bin/python playwright install chromium', file=sys.stderr)
                    return 4

                def on_response(response):
                    try:
                        if not is_target_url(response.url):
                            return
                        content_type = response.headers.get('content-type', '').lower()
                        if 'json' not in content_type:
                            return
                        print('{} {} {} [{}]'.format(
                            response.request.method,
                            response.status,
                            safe_url(response.url),
                            content_type.split(';', 1)[0],
                        ), flush=True)
                    except Exception:
                        # 单个响应元数据读取失败不应中断人工浏览。
                        return

                context.on('response', on_response)
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto('https://www.mihuashi.com/', wait_until='domcontentloaded')
                    if mode == 'login':
                        print('请在打开的浏览器中手动登录；登录凭据和验证步骤不会被脚本读取。')
                    else:
                        print('请手动打开画师端已接订单列表、分页和订单详情进行勘察。')
                    input('完成后回到此终端按回车退出…')
                except (KeyboardInterrupt, EOFError):
                    pass
                finally:
                    context.close()
    except RuntimeError as exc:
        if 'already locked' in str(exc):
            print('米画师浏览器正在另一项任务中使用，请先关闭当前登录或同步任务。', file=sys.stderr)
            return 3
        raise
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='米画师人工登录与请求元数据勘察（开发用）')
    parser.add_argument('command', choices=('login', 'inspect', 'dry-run'))
    parser.add_argument(
        '--browser', choices=('chrome', 'chromium'), default='chrome',
        help='浏览器：chrome 使用系统 Google Chrome（默认）；chromium 使用项目内 Playwright Chromium',
    )
    args = parser.parse_args(argv)
    return run(args.command, args.browser)


if __name__ == '__main__':
    raise SystemExit(main())

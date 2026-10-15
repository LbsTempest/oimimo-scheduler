"""米画师采集适配器入口。

订单页面路径、接口字段和分页规则尚无本人账号实测证据。在调查完成前，
collector 显式停止，不发起猜测请求，也不写入 oimimo 订单。
"""


class AdapterNotConfigured(RuntimeError):
    """没有经过实测确认的米画师订单页面适配器。"""


class LoginRequired(RuntimeError):
    """页面明确显示登录失效；自动采集必须暂停。"""


class PlatformAccessBlocked(RuntimeError):
    """平台返回拒绝、验证或限流信号；必须停止请求并等待人工处理。"""


class TransientCollectionError(RuntimeError):
    """采集过程发生可重试的临时网络错误。"""


COLLECTOR_READY = False


def collect_orders(headless=True, max_pages=20, stop_event=None):
    del headless, max_pages, stop_event
    raise AdapterNotConfigured('等待本人账号完成订单列表、详情和分页勘察')

"""将经实测适配器输出的规范字段映射到 ExternalOrder。"""

from typing import Any, Mapping

from .schemas import ExternalOrder


def map_external_order(data: Mapping[str, Any]) -> ExternalOrder:
    """校验规范化订单；平台原始字段名不得在此猜测或静默丢弃。"""
    if not isinstance(data, Mapping):
        raise TypeError('normalized order must be a mapping')
    return ExternalOrder.model_validate(dict(data))

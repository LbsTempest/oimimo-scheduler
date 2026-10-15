"""米画师订单的内部标准化模型；字段映射必须来自账号实测。"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExternalOrder(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    platform: str = 'mihuashi'
    external_order_id: str = Field(min_length=1, max_length=200)
    project_name: str = Field(min_length=1, max_length=500)
    platform_url: Optional[str] = None
    customer_external_id: Optional[str] = Field(default=None, max_length=200)
    customer_name: Optional[str] = Field(default=None, max_length=200)
    quoted_amount: Optional[Decimal] = None
    currency: str = Field(default='CNY', min_length=3, max_length=3)
    deadline: Optional[date] = None
    platform_status: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = Field(default=None, max_length=20000)
    source_updated_at: Optional[datetime] = None

    @field_validator('platform')
    @classmethod
    def platform_must_be_mihuashi(cls, value):
        if value != 'mihuashi':
            raise ValueError('platform must be mihuashi')
        return value

    @field_validator('platform_url')
    @classmethod
    def platform_url_must_be_mihuashi(cls, value):
        if value is None or value == '':
            return None
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or not (host == 'mihuashi.com' or host.endswith('.mihuashi.com')):
            raise ValueError('platform_url must use an HTTPS mihuashi.com host')
        if parsed.username or parsed.password:
            raise ValueError('platform_url must not contain credentials')
        return value

    @field_validator('quoted_amount', mode='before')
    @classmethod
    def amount_must_be_finite_nonnegative_decimal(cls, value):
        if value is None or value == '':
            return None
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError('quoted_amount must be a decimal amount')
        if not amount.is_finite() or amount < 0:
            raise ValueError('quoted_amount must be finite and nonnegative')
        return amount

    @field_validator('currency')
    @classmethod
    def currency_must_be_uppercase(cls, value):
        normalized = value.upper()
        if normalized != 'CNY':
            raise ValueError('only CNY is supported')
        return normalized

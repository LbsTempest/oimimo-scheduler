# 米画师字段映射

本文件记录同步模型与 oimimo 的权责。米画师来源字段仍待本人账号实测，不能把建议字段名当成接口事实。

| 标准字段 | 米画师来源字段 | oimimo 目标 | 同步规则 |
| --- | --- | --- | --- |
| `external_order_id` | 待实测 | `external_order_links.external_order_id` | 自动 upsert 必须具备稳定 ID；缺失时校验失败，不创建订单 |
| `project_name` | 待实测 | `orders.project_name` | 新单初始化；已有订单只有在本地值仍等于上次同步值时更新，否则报告冲突 |
| `platform_url` | 待实测 | `orders.platform_url` | 仅允许 HTTPS 的 `mihuashi.com` 子域；本地人工修改受保护 |
| `deadline` | 待实测 | `orders.page_deadline` | 只写平台参考 DDL，不写 `scheduled_start/end`；平台未提供值时不清空 |
| `platform_status` | 待实测 | `external_order_links.platform_status` | 保留为平台状态，不覆盖 `orders.current_stage` |
| `quoted_amount` | 待实测 | `external_order_links.quoted_amount` | Decimal 字符串保留为报价元数据；不写入 deposit、income 或收款流水 |
| `customer_external_id/name` | 待实测 | 暂无映射 | 不用同名客户猜测关联；待确认平台 ID/客户模型对应关系 |
| `description` | 待实测 | 暂无映射 | 不写入本地 notes，避免覆盖人工笔记或保存聊天隐私 |
| `source_updated_at` | 待实测 | 暂无映射 | 仅作为模型字段，等待确认平台时间及其时区含义 |

`external_order_links` 保存字段 hash 用于检测本地标题/链接编辑；不保存抓包体或登录凭证。删除本地订单后外部映射保留空 `local_order_id` 墓碑，后续采集进入冲突，不自动重建。

当前采集器故意标记为未就绪。完成实际 endpoint、ID、分页及状态观察后才能启用自动采集和设置页的“立即同步”。

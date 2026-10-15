# oimimo-scheduler API 审计：米画师导入

审计对象：上游 `mimo9708/oimimo-scheduler`，本地 `main` 分支，commit
`ffa5c3994139acc9982c8685ee6855223b321f88`（2026-10-06，v1.5.0）。

本文只记录源码静态审查结果；没有启动或写入用户数据库。

## 现有路由

- `POST /api/import/mihuashi`，定义于 `app.py:3854`。
- 先调用 `_is_local_origin()`。它接受 localhost、米画师网页来源、浏览器扩展来源，也接受 Origin 和 Referer 都缺失的请求；其他明确外站来源返回 403。
- JSON 请求支持 `projectName/project_name`、`url`、`customerName/customer_name`、`customerId/customer_id`、`price`、`category`、`deadline`、`startDate/scheduled_start`、`endDate/scheduled_end`、`fee`、`description/notes` 等字段别名。
- 金额解析为浮点数；无法解析时静默回退为 0。日期仅接受有限的年月日格式，不能识别的值转成空值。
- 客户先按米画师个人主页 URL 匹配，再按名称匹配，最后新建。平台 ID 命中且名称变化时会尝试重命名客户。
- 路由组装 `order_data` 后直接调用 `db.create_order()`。该函数执行订单财务计算和插入事务；路由未通过 `models.OrderCreate` 校验。

## 重复导入与字段权责

- 没有读取或保存米画师外部订单 ID，也没有映射表、更新路径或去重检查。每次成功请求都会新建一个订单。
- `price` 被写入 `deposit`，余额写为 0；它不代表平台已到账，但当前路由也没有保留独立的“平台报价”字段。
- `deadline` 写入 `page_deadline`。`startDate` 和 `endDate` 分别写入 `scheduled_start` 和 `scheduled_end`，会直接创建本地排期。
- `description` 写入 `notes`，会与本地笔记共用同一字段。
- 平台订单状态没有被接收或保存，也不会更新 `current_stage`；新订单阶段固定为 `pending`。
- 因而此路由不能作为自动同步的写入层。集成应增加服务端外部 ID 映射和幂等业务服务，并保护本地排期、阶段、收款及人工笔记。

## 生命周期与依赖

- `launcher.py` 先执行 `db.init_db()`，再在守护线程中启动 Flask；主监听绑定 `127.0.0.1`。启动器有单实例守卫，Flask 使用 `debug=False`（未显式设置 `use_reloader=False`）。
- 托盘关闭行为允许主进程继续运行；真正退出由退出操作处理。正式集成的同步管理器须由启动器统一启动和停止，并避免 GUI/reloader/打包入口重复创建。
- 审计基线 commit 的 `requirements.txt` 没有 Playwright；本次集成已将其加入运行依赖并增加本地浏览器运行时打包步骤，Windows 构建仍须在 Windows 环境实测。
- `settings` 是数据库键值表，可保存同步配置；目前没有外部订单映射、同步运行记录或同步 API。

## 结论

可保留现有 `/api/import/mihuashi` 供当前插件使用，但不能用它实现重复同步安全。下一步先完成人工登录和只打印请求元数据的浏览器勘察；拿到本人账号的稳定订单 ID、列表/详情字段和分页事实后，再实现 parser、映射与内部幂等 upsert。

# oimimo scheduler — 架构文档

> **版本**: v1.5.0 · **基准日期**: 2026-10-06
> 本文档以当前代码事实为准。功能说明见 [PROJECT.md](PROJECT.md)，视觉设计见 [DESIGN.md](DESIGN.md)。
> v1.5.0 是**后端标准化版本**：枚举值全面英文 code 化，用户界面中文保持不变（详见 §十一.15）。

---

## 一、系统概览

面向独立插画师的**本地单用户**接单排单工具。Tkinter 启动器拉起 Flask 守护线程，用户在浏览器中操作；SQLite 单文件持久化，零网络依赖（前端库已全部本地 vendor 化）。

**运行形态**：

```
launcher.py (Tkinter 窗口, 可选 pystray 托盘)
   │  threading.Thread(daemon=True)
   ▼
app.py (Flask 单实例, 模块级 app = Flask(__name__))
   │  Jinja2 服务端渲染 + HTMX 局部刷新 + JSON API
   ▼
db.py (SQLite WAL) ── models.py (Pydantic v2 校验)
   │
   ▼
orders.db · uploads/ · exports/ · logs/ · backups/
```

**代码规模实测**（2026-10-06，v1.5.0）：

| 文件 | 行数 | 内容 |
|---|---|---|
| app.py | 4469 | 126 路由端点（`@app.route` 51 / `@app.post` 66 / `@app.get` 5 / `@app.put` 2 / `@app.delete` 2，运行时 url rule 127 含 static）· 200 顶层函数 · 11 模板过滤器 · 8 模板测试 · 1 上下文处理器 · 1 before_request · 2 errorhandler |
| db.py | 6248 | 235 顶层函数 · 建表/迁移（M0-M19）· CRUD · 统计 · 注册表 · 备份管理 |
| models.py | 481 | 13 个 Pydantic v2 模型（订单/收款/客户/回复模板/价目/小票六组） |
| launcher.py | 463 | Tkinter 窗口 · pystray 托盘 · 重启/日志/端口管理 |
| image_processor.py | 367 | 原图/预览(800px)/缩略图(200×200) 三级产物 |
| static/app.js | 2014 | 83 顶层函数（模态框/抽屉/看板/Toast/ColorPicker/布局/图表主题/备份恢复） |
| static/app.css | 3514 | 61 个静态 CSS 自定义属性 + 服务端 `_build_theme_css()` 运行时注入派生变量 · 组件/视图/响应式样式 |
| templates/ | 50 HTML | 7 页面（base/index/calendar/income/gallery/settings/stats_lab）+ 17 partials + 订单 10 + 客户 3 + 小工具 13 |
| tests/ | 26 文件 · 213 test 函数 | `pytest tests/ -q` = **282 passed**（参数化展开后用例数） |

---

## 二、进程与部署架构

### 2.1 启动链路（launcher.py）

```
main()
 ├─ check_python()        # ≥3.9
 ├─ install_deps()        # pip 安装缺失依赖
 ├─ db.init_db()          # 幂等建表+迁移
 ├─ 端口确定（默认 1096，命令行可覆盖）
 ├─ threading.Thread(target=_start_server, daemon=True)
 ├─ show_postcard(url)    # 340×370 Tkinter 明信片窗口
 └─ 自动打开浏览器
```

- **托盘**：安装 pystray+Pillow 时，关闭窗口 → `hide_to_tray()` 最小化到系统托盘；未安装 → 普通窗口模式
- **窗口按钮**：重启服务、查看日志、清除缓存、停止服务
- **日志重定向**：打包模式 → `logs/app_YYYYMMDD_HHMMSS.log`

### 2.1b 日历订阅第二监听（Spec 20，feed_enabled=1 时）

主端口 1096（127.0.0.1，本机页面）之外，`start_feed_server()` 拉起 `0.0.0.0:1097` 第二监听，仅供局域网手机日历拉取 ICS：

- **双模式拉起**：源码模式（dev）用独立子进程（env 剔除 `WERKZEUG_*`，防 debug reloader 的 fd 复用/env 污染双陷阱；端口被占自重试接管，`atexit` 连带 terminate）；打包模式（frozen exe）降级同进程守护线程
- **端口守卫**：`before_request` 判 `SERVER_PORT=='1097'` 且非 `/api/feed/` 前缀 → 403；页面/表单/静态资源永远到不了局域网
- **生命周期**：`feed_enabled=1` 保存时热启动（幂等），开启即时生效；关闭需重启（不热停）；launcher `_start_server()` 与 `__main__`（reloader 守卫内）各调用一次
- **鉴权**：`?token=` 匹配 settings `feed_token`（32 hex，可经设置页旋转，旧 URL 立即失效）；设置页「日历订阅」卡片提供开关/URL 复制/旋转

### 2.2 路径分离（PyInstaller 兼容）

| 用途 | 函数 | 开发态 | 打包态 |
|---|---|---|---|
| 只读资源 | `app._resource_dir()` / `launcher.resource_path()` | 脚本目录 | `sys._MEIPASS` |
| 可写数据 | `db.data_dir()` | 脚本目录 | exe 同级目录 |

`DB_PATH = data_dir()/orders.db`；备份目录 `get_backup_dir()` 从 settings 表读取（默认 `data_dir()/backups/`）。

### 2.3 tests/ 测试基建（Spec 25 首建，卡 98）

| 文件 | 职责 |
|---|---|
| tests/conftest.py | 会话级临时根目录隔离：导入期重定向 `db.DB_PATH`/`DEFAULT_BACKUP_DIR` + monkeypatch `db.data_dir` 后 import app；client/pricelist_item/autouse 零残留 fixture + PIL 上传辅助（make_png_bytes） |
| tests/test_pricelist_v2.py | 价目表 A-T01~13（16 用例：CRUD/区间/例图上限/Origin 守卫/inf 拒收） |
| tests/test_receipt.py | 小票服务端 R-T01~18+R-T22（19 用例：计算对拍/边界/错误输入/鲁棒） |
| tests/test_security.py | 安全专项 R-T19 XSS 服务端侧 / R-T20 SQLi / R-T21 Origin+CORS（9 用例） |
| tests/test_payments_*.py（7 文件，Spec 26） | migration 4（建表/幂等/级联/孤儿清理）/ stats 7（事件流手算对拍+主页口径同源）/ crud 7（收齐自动结算+只进不退）/ guards 6（白名单/冲突/切换/免收四守卫）/ routes 5（三路由+Origin 守卫+待收口径）/ ui 5（表单开关+时间线+panel 同源）/ receipt_copy 5（小票三数+按笔+文案） |
| tests/snapshot_stats.py | 统计对拍快照工具（Spec 26 卡 103）：metric_* × dashboard × stats_detail × 三图全出口 × 7 范围矩阵，冻结时钟确定性输出，`--compare` 逐位对比（阶段 1 验收门槛） |
| tests/test_spec31_card_view.py | Spec 31 卡片视图（4 用例：双结构 DOM 冒烟 + 网格 CSS + toggle 控件 + 视图记忆） |
| tests/test_cleanup_images.py | Spec 30 孤立图片清理（用例含清理路由 + 路径归一化 + FK 约束） |
| tests/test_spec30_regression.py | Spec 30 回归（图片上限扩容 + 分类导出 + 压缩参数 + 孤立清理端到端） |
| tests/test_feed_etag.py | 日历订阅 ETag/304 + 访问日志 |
| tests/test_stats_lab_nl.py | Spec 27 NL 意图解析器（关键词匹配 + 路由） |
| tests/test_stats_lab_presets.py | Spec 27 预设 CRUD + 数据源扩展 |

隔离口径：全部用例走独立临时 DB/目录，绝不触碰真实 `orders.db`/`uploads/`；autouse fixture 逐用例零残留。运行：`pytest tests/ -q`（当前 162 用例）；build.spec excludes 含 tests（不打包）。

---

## 三、应用分层

| 层 | 文件 | 职责 |
|---|---|---|
| 路由层 | app.py | 请求解析、Pydantic 校验、响应、CORS、来源校验 |
| 校验层 | models.py | OrderCreate/OrderUpdate/CustomerCreate/CustomerUpdate |
| 数据层 | db.py | 连接/事务、建表迁移、CRUD、自动计算、统计、注册表、备份 |
| 图片层 | image_processor.py | 上传 → 原图+预览+缩略图，Pillow 缺失时降级 |
| 模板层 | templates/ | base.html（4 block）+ 页面 + partials |
| 静态层 | static/ | app.css（设计系统）、app.js（交互）、vendor（本地库） |

---

## 四、路由体系

### 4.1 页面路由（GET）

| 路由 | 模板 | 说明 |
|---|---|---|
| `/` | index.html | 统计卡+甘特图+本周排单+逾期 |
| `/income` | income.html | 收入看板（gridstack 自定义布局） |
| `/calendar` | calendar.html | 日历（5 种着色模式） |
| `/gallery` | gallery.html | 瀑布流画廊 |
| `/orders` | orders/list.html | 列表（多维筛选+分页） |
| `/orders/kanban` | orders/kanban.html | 看板（SortableJS 拖拽） |
| `/orders/<id>` | orders/detail.html | 订单详情 |
| `/orders/new` | orders/form_modal.html | 新建（`?template=` 预填） |
| `/orders/<id>/edit` | 三模式分流 | `?inline=1` 抽屉 / `?modal=1` 模态 / 默认完整页 |
| `/customers` | customers/list.html | 客户列表 |
| `/customers/<id>` | customers/detail.html | 客户详情+历史+作品 |
| `/customers/new` · `/customers/<id>/edit` | customers/form.html | 客户表单 |
| `/settings` | settings.html | 设置（5 区域） |
| `/tools` | tools/index.html | 小工具市场（搜索+分组 chip+最近使用） |
| `/tools/reply-templates/` | tools/reply_templates.html | 回复模板（分组侧栏+卡片） |
| `/tools/pricelist/` | tools/pricelist_preview.html | 价目表预览（双视图：长条 #pl-menu + 卡片 #pl-cards 网格，视图切换分段控件 + localStorage 记忆；导出统一 `renderOffscreenToBlob` helper，按分类恒 640px + 单卡下载按钮） |
| `/tools/pricelist/edit` | tools/pricelist.html | 价目表编辑看板（分类分节+拖拽+例图管理） |
| `/tools/pricelist/preview` | → 301 重定向 `/tools/pricelist/` | 旧预览地址兼容 |
| `/tools/receipt/` | tools/receipt.html | 小票预览（默认落地：D15 四按钮 + 打印白底黑字 + PNG 导出） |
| `/tools/receipt/edit` | tools/receipt_edit.html | 小票编辑（六分区编辑器 + 实时预览 + datalist 联动 + 模板管理；方案B 柔和填充控件作用域 `.rc-edit-grid`，主图模式分段选择器 灰度/抖动/彩色） |
| `/stats-lab` | stats_lab.html | 统计实验室（Spec 27：NL 意图解析 + 预设持久化 + 多数据源 + 口径速查） |
| `/customers/batch` | — | 客户批量操作 |
| `/export/open-folder` | — | 打开导出目录（Explorer） |
| `/uploads/<path>` | — | 上传文件静态服务 |

### 4.2 数据 API（GET + POST）

| 路由 | 返回 | 说明 |
|---|---|---|
| `/api/gallery` | JSON | 画廊分页（offset/limit） |
| `/api/stats` | HTML | 统计卡 HTMX 刷新 |
| `/api/stats/detail` | JSON | 小票明细（Spec 26 扩展：expected 附三数列，事件流附 payment_mode） |
| `/orders/<id>/payments/panel` | HTML | 收款卡片局部刷新（详情页 installment 单；simple 防御空串，Spec 26） |
| `/api/orders` | HTML | 订单行 HTMX 刷新 |
| `/api/orders/gantt-data` | JSON | 甘特图数据 |
| `/api/orders/calendar-events` | JSON | 日历事件 |
| `/api/orders/unscheduled` | HTML | 未排期池 HTMX |
| `/api/customers/quick` | HTML | 快速建客户 |
| `/api/income/type-distribution` | JSON | 品类分布（月份筛选） |
| `/api/income/hourly-type-distribution` | JSON | 时薪品类分布 |
| `/api/quote-suggestion` | HTML | 报价建议条 |
| `/api/v1/health` | JSON | 健康检查 `{status, version}` |
| `/api/integrations/mihuashi/status` | JSON | 本机同步状态；仅返回任务元数据 |
| `/api/integrations/mihuashi/settings` | JSON / JSON | 读取 / 更新同步配置；写入校验本机 Host 与同源 Origin |
| `/api/integrations/mihuashi/sync` | JSON | 后台串行排队一次同步任务，立即返回 task ID |
| `/api/integrations/mihuashi/login` | JSON | 后台打开用户可见的持久化 Chromium 登录窗口 |

### 4.3 订单操作（POST）

`/orders` 创建 · `/orders/<id>/edit` 更新 · `/orders/<id>/delete` · `/orders/<id>/archive`（归档确认链）· `/orders/<id>/stage` · `/orders/<id>/reschedule` · `/orders/<id>/color` · `/orders/<id>/unschedule` · `/orders/batch`（阶段/来源/收款/DDL/商用/归档/删除/重算）· `/orders/<id>/upload-image` · `/orders/<id>/remove-image` · `/orders/<id>/work-hours` · `/orders/templates/<id>/delete` · `/orders/<id>/payments`（记一笔 POST，Spec 26）· `/orders/<id>/payments/<pid>`（改/删笔 PUT/DELETE，Spec 26）

### 4.4 设置与系统（POST）

`/settings` · `/settings/reset` · `/settings/theme/import` · `/settings/theme/apply` · `/settings/theme/delete` · `/settings/color-mode` · `/settings/palette/save` · `/settings/palette/apply` · `/settings/palette/update` · `/settings/palette/delete` · `/settings/income-layout` · `/settings/stage-flows` · `/settings/backup-dir` · `/settings/backup` · `/settings/backups` · `/settings/restore/<filename>` · `/settings/backup/delete/<filename>` · `/settings/commission-merge` · `/settings/source-merge` · `/settings/feed-token/rotate` · `/export/orders` · `/api/import/mihuashi` · `/api/shutdown` · `/api/log-error`

### 4.5 小工具平台（Spec 22）

- **注册表**：`TOOL_REGISTRY`（`_TOOL_REGISTRY_RAW` + `_build_tool_registry()` 重复 slug 校验）；`_tool_or_404(slug)` 统一取工具元数据
- **回复模板**：`POST /tools/reply-templates/` 创建 · `<id>/update` · `<id>/delete` · `group/rename` · `group/delete`
- **价目表**：`POST /tools/pricelist/` 创建 · `<id>/update` · `<id>/delete` · `<id>/upload-example`（JSON，多例图上限 3 张） · `images/<img_id>/delete`（JSON，清三件套文件+回填首图） · `images/reorder`（JSON ids → 204） · `reorder`（JSON ids → 204） · `meta`（JSON）· `board`（分类看板页）；2026-08-12 多图化后原 `<id>/remove-example` 移除（由 `images/<img_id>/delete` 取代）
- **小票打印机**（Spec 23）：`POST /tools/receipt/draft`（整体保存，Pydantic 校验 400） · `upload-image`（gray/dither 黑白化 / color 保留原色 + 三级产物） · `remove-image` · `upload-bg`（单文件 1600px 缩放） · `remove-bg` · `templates/` 创建 · `templates/<id>/apply`（只合并 style+文案，不触碰 items/计算参数） · `templates/<id>/update` · `templates/<id>/delete`（引用检查 `receipt_bg_referenced`） · `mult-presets`（Spec 24 单品倍率预设保存）；渲染口径唯一来源 `static/receipt.js`（`rcRender`/`rcCalc`/`rcEditorInit`），四预设 list/retro/hand/mono；**Spec 24 计算模型**：单品倍率（无/商用/买断快捷预设 + 手改）+ 单品/整单折扣双形态（直减金额 / 中文折数×/10），冻结公式 =（单价×量+Σ附加）×单品倍率×单品折扣 → Σ合计 → ×整体倍率 − 整体折扣 − 定金 = 尾款（赠品恒 0 不参与倍率折扣）
- **写响应约定**：成功 → 整区片段 + `HX-Retarget`（#rt-app/#pl-app）+ `HX-Reswap: innerHTML` + `HX-Trigger`（`json.dumps` 默认转义，HTTP 头禁非 latin-1）；校验失败 → 表单片段 + 400（app.js `htmx:responseError` 仅限 `.tool-htmx-form` 手动换入）

---

## 五、数据库设计

### 5.1 表结构（实测 orders.db）

**orders（39 列 + 7 索引，Spec 26 增 payment_mode）**

| 分组 | 列 |
|---|---|
| 标识/关联 | id PK · customer_id FK→customers ON DELETE SET NULL |
| 内容 | project_name · source（Spec 33 存 code，如 mihuashi）· is_commercial · commission_type（code，如 cmt_001）· platform_url · notes · custom_color |
| 流程 | current_stage（Spec 33 存 code，如 pending/completed）· ddl_status（code，如 normal/overdue）· payment_status（code，如 unpaid/settled）· stage_flow（本单阶段快照 JSON，元素亦为 code）|
| 财务 | deposit · balance · income · platform_fee · platform_fee_pct（快照）· actual_received · payment_mode（收款方式 simple 默认/installment 分期，Spec 26）|
| 折扣（Spec19） | discount_pct（订单级折扣快照 %）· discounted_income（折后金额） |
| 归档 | is_archived · completed_at · is_overdue |
| 复购 | is_repeat · repeat_count（查询时计算，列保留作回滚兜底） |
| 排期 | page_deadline · scheduled_start · scheduled_end · sort_order |
| 时薪 | estimated_hours · work_hours · exclude_hourly（不进 `_MONEY_FIELDS`） |
| 图片 | image_url · image_path · has_image |
| 审计 | created_at · updated_at |

**customers（10 列）**：id · name UNIQUE · platform_url · preferences · notes · tags · total_spent · purchase_count · created_at · updated_at

**settings（86 键）**：key PK, value — 详见 §9

**external_order_links**：platform · external_order_id · local_order_id（本地订单删除时置 NULL，作为禁止自动重建的墓碑）· source/本地字段 hash · page_deadline · quoted_amount · platform_status · last_seen_at；UNIQUE(platform, external_order_id) + UNIQUE(platform, local_order_id)

**mihuashi_sync_runs**：run_id · kind · status · summary_json（仅计数/任务结果）· error_code · duration_ms · created_at · started_at · completed_at

**order_images（6 列）**：id · order_id · image_url · image_path · sort_order · created_at

**order_payments（6 列，Spec 26 分期收款流水）**：id · order_id（FK → orders ON DELETE CASCADE）· paid_at（到账日 YYYY-MM-DD）· amount REAL（ge=0）· note（≤500 可空）· created_at；双索引（idx_payments_order / idx_payments_paid_at）；孤儿记录启动时自动清理；写入仅经收款 CRUD 状态机（§6.8），无直接 INSERT 路径

**order_templates（4 列）**：id · name · data_json · created_at

**reply_templates（7 列，Spec 22）**：id · group_name（默认「未分组」，db 层空值兜底） · title · content · sort_order · created_at · updated_at

**pricelist_items（11 列，Spec 22 + Spec 30 例图上限 10）**：id · category（默认「默认」） · name · price REAL · price_max REAL NULL（价格区间上限，可空） · unit · description · example_image_path（首图 preview 相对路径，多图后始终回填首图） · sort_order · created_at · updated_at；例图三级产物落 `uploads/pricelist/<id>/`

**pricelist_images（5 列，2026-08-12 多例图）**：id · item_id（FK → pricelist_items ON DELETE CASCADE） · image_path · sort_order · created_at；每项目最多 10 张（Spec 30 路由层拦截），对齐 `order_images` 多图模式

**receipt_items（12 列，Spec 23 + Spec 24）**：id · name · price REAL（主项单价；附加子行=单项加价金额） · qty REAL · parent_id（自引用 FK ON DELETE CASCADE，附加服务子行挂主项） · is_gift（1=赠品划线计 0） · multiplier REAL（单品倍率默认 1，Spec 24） · mult_label（倍率标签，小票角标，Spec 24） · discount_type（单品折扣形态 none/amount/rate，Spec 24） · discount_value REAL（金额或中文折数，Spec 24） · sort_order · created_at

**receipt_templates（5 列，Spec 23）**：id · name · config_json（只存 style+文案，不存制品/计算参数 D14） · created_at · updated_at；小票草稿另存 settings `receipt_draft` 键（全删全插 CRUD）

**choice_items（10 列，Spec 33 枚举注册表）**：id · kind（stage/ddl/payment/source/commission_type）· code（英文编码 PK 语义）· label（中文展示名）· is_platform（1=平台来源，参与抽成）· default_fee_pct REAL（默认费率 5.0）· sort_order · is_builtin（1=系统内置，不可删）· created_at · updated_at；UNIQUE(kind, code) + 索引 idx_choice_items_kind(kind, sort_order, id)。取代散落的 `platform_sources`/`source_list`/`default_fee_*` settings 键（M11/M12/M13 迁移）；详见 §6.1

### 5.2 事务模型

`db.transaction()` 上下文管理器：成功 commit、异常 rollback、始终 close。业务函数接受 `conn=` 参数复用外部事务。

---

## 六、核心机制

### 6.1 枚举中枢（Spec 33 编码化）

**核心契约（D1/D2）**：5 类枚举（stage/ddl/payment/source/commission_type）的**存储值**全面英文编码化——`orders.current_stage/ddl_status/payment_status/source/commission_type` 与 `settings` 相关键只存 code；**展示层恒中文**（所有跨边界输出经翻译层出 label）。code 是稳定内部标识，label 是可变中文文案。

**三层翻译字典（db.py 唯一真实来源）**：

| 字典 | 方向 | 语义 |
|---|---|---|
| `CODE_TO_LABEL[kind]` | code → label | 一对一反查，展示用（stage 7 值 / ddl 5 / payment 5 / source 6 / commission_type 8） |
| `LEGACY_LABEL_TO_CODE[kind]` | 旧中文写法 → code | 多对一「识别输入」，**永久冻结只增不改不删**；迁移与兼容读都依赖它（如 `逾期`/`🔴逾期`→overdue、幽灵值 `待收款`→unpaid） |
| `RESERVED_ENUM_CODES` | 宽判据保留字集 | 全 kind code ∪ 全部旧标签 ∪ ID 形态 `^(src\|cmt)_\d{3,}$`；新建侧拒绝自定义阶段/来源/类别占用（D17/D18 三层保护） |

**翻译函数**：`enum_code_or_raw(kind, v)` 兼容读——已迁移返回 code、未迁移原样返回旧中文（比较点统一用它，杜绝 code/中文双空间错配）；`enum_label(kind, v, with_emoji)` / `choice_label(kind, code)` 出中文 label；`resolve_choice_code(kind, token)` 任意写法归一到 code。

**统计出口的 code/label 边界（Spec 35 D1-D4）**：分组统计函数**在 db 层单点附 `label`**，原 code 字段保留不动——`_hourly_group_stats` 与 `metric_type_distribution`（`db.py` 末尾）都在返回前用 `{r['code']: r['label'] for r in get_choice_rows(kind)}` 建映射，`label_map.get(code, code)` 未知值原样直通（D3，绝不猜字符集）。API 只新增 `label` 键、原字段与排序不变（D4）。**配色键必须恒为 code**（D2）：`COMMISSION_COLORS` 等前端调色表的键是 `cmt_*` code，`templates/income.html` 因此把「显示标签」与「配色取值」拆成两个数组（`labels` 取 `label`，另建 `var TYPE_CODES` 取 `commission_type` 喂 `seriesColors`）——若把中文标签直接喂给配色查找，全部扇区会退化成 `chartFallbackColor`，等于把显示缺陷修成配色回归。**画布盲区**：Chart.js 的 labels 活在 `<canvas>` 位图里，只扫 DOM 文本的裸 code 门禁会漏检（卡 169 的教训），回归须读 `Chart.getChart(canvas).data.labels`；`tests/test_income_type_labels.py` 以渲染后内联脚本文本为断言面（注意 Jinja `tojson` 会把非 ASCII 转成 `\uXXXX`，断言须两种形态都接受）。

**choice_items 表（Spec 33 新建）**：`kind · code · label · is_platform · default_fee_pct · sort_order · is_builtin`，UNIQUE(kind, code)。系统枚举播种（`seed_choice_items`）+ 用户自定义项落表，取代散落的 `platform_sources`/`source_list`/`default_fee_*` settings 键（M11/M12/M13 迁移）。读路径回退 settings 兜底（播种失败不阻断启动）。

**9 个语义访问器**（签名一字不改，只换返回值空间——改造半径最小化的核心手段，全项目 40+ 调用点自动获益）：`get_paid_statuses()`→`{'settled'}`（现金口径）· `get_archive_paid_statuses()`→`{'settled','waived'}`（归档口径）——**二者必须分离（D7）**：免收订单可归档但不计收入；`is_terminal_stage` / `is_refund_stage` / `get_done_stage` / `get_refund_stage` / `get_terminal_stages` / `get_stage_progress` / `get_ddl_status`。前三个内部先过 `enum_code_or_raw` → 对未迁移值/自定义值/恢复的老备份继续工作。

**get_choices(kind) 合并语义**：系统类（stage/ddl/payment）base = `CODE_TO_LABEL` 的 code 集；用户类（source/commission_type）base = `choice_items` code，auto-discover 追加时**自动补建行**（从「只影响下拉显示」升级为「补登记」）。进程内缓存 `_CHOICES_CACHE`，失效统一走 `_invalidate_choices_cache()`（所有 choice_items 写函数末尾调用）。

**用户枚举 code 空间（卡 168 收口）**：`choice_items` 是来源/类别的唯一事实源。写入侧 `ensure_choice_code(kind, token)`——空 token / 保留字 / 去空白为空则原样返回；命中现有 code 或 label 则归一返回该 code；未命中则同事务内 `_next_choice_code` 补登记一行（`label=token`）并返回 code。`models._normalize_enum_token` 对 source/commission_type 走它，系统三类（stage/ddl/payment）仍走 `enum_code_or_raw`。读侧 `get_user_choice_items(kind) -> [{'code','label'}]` 供界面消费，`get_choices` 用户分支 base 只出 code；`inject_constants` 的 `SOURCE_CHOICES`/`COMMISSION_TYPE_CHOICES`、`_get_existing_values`（datalist）、日历 `filter_options` 全部改用它。改名唯一路径 = 更新 `choice_items.label`（`sync_choice_renames` 已删）；`merge_sources` / `merge_commission_types` 入参出参全 code，费率改写 `choice_items.default_fee_pct` 列。`seed_choice_items` 对内置项也落行、对既有行不改 label/排序/平台标记（用户改名与平台勾选是用户财产）；`_ensure_default_settings` 不再重建 `source_list`/`platform_sources`/`payment_status_list`/`commission_type_list` 四键。

**平台判断必须走 code 空间（卡 169）**：两个访问器出的是不同值空间，不可混用——`get_platform_sources()` 出**标签**集合（给界面文案用），`get_platform_source_codes()` 出 `choice_items.is_platform` 行的 **code** 集合（给判断用）。需要判断「本单来源是不是平台」的地方一律用 code 集合：写入侧 `_calc_financials` 的 `is_platform`、`get_default_fee_for_source`、模板测试 `_test_platform_source`、编辑表单财务块、`detail.html`、`partials/order_row.html`、`export_orders_md` 与 `export/orders.md`。`inject_constants` 因此同时注入 `PLATFORM_SOURCES`（标签，供 JS 比对输入框的中文值）与 `PLATFORM_CODES`（code，供模板判断）。拿 code 去比标签集合的后果不是显示错位那么轻——平台单会被判成非平台，`platform_fee` 静默归零、`actual_received` 虚高，且全程不报错。

**配色键后缀统一 code（卡 169）**：调色板键 `cal_<mid>_<后缀>` 的后缀一律是 **code**（`cal_stage_pending` / `cal_ddl_normal` / `cal_payment_unpaid` / `cal_source_mihuashi` / `cal_commission_cmt_004`），例外只有自定义阶段名（无 code，后缀即中文原名）。四处必须同口径：`_ensure_default_settings` 的默认键、`_build_theme_css` 读链（code 优先、中文键仅作旧值兜底）、设置页着色面板的键生成、M8-M10 迁移的换键结果。中文后缀的残渣键由幂等函数 `_cleanup_legacy_palette_keys` 清除（判据：后缀能被 `resolve_choice_code` 解析成**另一个** code ⇒ 是标签/旧写法；解析不出 ⇒ 自定义名，保留）；其调用点必须在 `init_db` 末尾、迁移引擎之后——`user_version` 由引擎在 init_db 后段才置为 33，且生成型 code（`src_NNN`/`cmt_NNN`）的标签映射由 `seed_choice_items` 写入，两者都就绪判据才准。

**流程快照渲染**：`stage_flow` JSON 的 `name` 迁移后是 code（M6/M7/M15），五个表单模板（`form` / `form_modal` / `form_modal_edit` / `form_inline` / `templates_form`）注入 `STAGE_LABEL_MAP`（= `ENUM_LABELS.stage`）把快照 name 翻成中文再渲染下拉，`option.value` 仍是 code。

**迁移门（D11/D13，全 spec 唯一不可逆数据动作）**：`PRAGMA user_version=33` 为门闸，M0-M19 幂等引擎（`_migrate_enum_codes`）一次性把存量中文值翻成 code；升级前重命名备份 `orders.pre-v1.5.0.db`（永不写入，须永久保留）；M0 预审计（`is_ambiguous_in_column` 严判据）拦截歧义值；迁移失败降级为兼容模式启动（`enum_migration_failed` settings 标记），不阻断使用。M19（`_m19_normalize_enum_columns`）为永久启动守卫，兜住任何残留中文值。

### 6.2 终态元数据 + 阶段流程

`STAGE_META` / `DDL_STATUS_META` 以 **code 为键**显式注册 terminal/kind/progress（DDL 值含 `{kind,label,emoji}`）。`STAGE_FLOWS` 流程预设首尾锚点固定为 `ANCHOR_FIRST_NAME='pending'` / `ANCHOR_LAST_NAME='completed'`（code），快照优先链：本单快照 → STAGE_META → 退单(cancelled)=100 / 未知=0。`validate_stage_flow` 用全 `RESERVED_ENUM_CODES` 拒绝保留字作自定义阶段名。

### 6.3 自动计算链

- **财务** `_calc_financials`：**先折后费**（Spec19）——`discount_pct` 非 NULL → `discounted_income = income × 折扣%`（NULL → 折后=原价）；平台来源 → `platform_fee = 折后 × pct%`，直接来源 → 0；`actual_received = 折后 − 费`。折扣为订单级快照（建单从客户带出、可逐单改，客户改折扣不回溯存量，与费率快照同构）
- **DDL** `_auto_calc_ddl_status`：终态优先 → 满足归档集合时联动 → 按 scheduled_end 与今天比较
- **归档唯一入口**：`set_archived()` / `_apply_archive_to_data()` 写 `completed_at` + `is_overdue` 快照
- **复购查询时计算**：`_apply_repeat_for_rows()`（不落库）
- **days_remaining 读取时注入**：`_apply_days_remaining_for_rows()`（新增返回订单 dict 的函数时必须调用）

### 6.4 update_order 单管线

读旧单 → merge → 费率快照规则 → `_calc_financials` → DDL 重算+条件归档 → UPDATE → recalc_customer_stats。

### 6.5 统计口径字典（Spec 26 事件流 + 口径分离）

**口径分离原则**：工作口径（描述"事"的进展）与现金口径（描述"钱"的到账）自 Spec 26 起分离——工作口径沿用订单快照列；现金口径统一走**收款事件流**。

**事件流构建器 `_payment_events_sql()`**（db.py 唯一真实来源，恒输出 ev_date/ev_amount/ev_mode）：

- 整单（simple）：虚拟事件 `[scheduled_end, actual_received]`——过滤条件逐字沿用旧口径，数学等价（老订单统计逐位不变）
- 分期（installment）：JOIN order_payments 每笔真实事件 `[paid_at, amount]`——到账即计，无状态过滤

| 口径 | 指标 | 数据源 |
|---|---|---|
| 工作口径 | 进行中 / 已完成 / 逾期 / 时薪 / 品类分布 | 订单快照列（不因收款方式变化） |
| 现金口径 | 本期到账（metric_realized_income）/ 月度收入 / 年累进 | 事件流（同源：get_stats_detail income/monthly_income 成对） |
| 现金口径 | 待收金额（metric_expected_income）/ 每月预计 | simple=净额；installment=净额−Σ已到账（下限 0） |
| 工作口径（明确不动） | completed 小票 / 客户 total_spent | 旧口径保留 |

年份推算 `get_available_years()`：scheduled_end ∪ paid_at（跨年尾款年份可见，D11）。对拍验证：`tests/snapshot_stats.py` 全出口 × 7 范围矩阵，改造前后逐位一致（阶段 1 验收门槛，全 simple 数据 CASE 短路等价）。

### 6.6 备份管理

- `get_backup_dir()`：settings 表读取自定义路径，留空回退 `DEFAULT_BACKUP_DIR`（exe 同级 `backups/`）
- `set_backup_dir(path)`：校验存在+可写，保存到 settings
- 5 个备份函数统一使用 `get_backup_dir()`：`_do_backup` / `get_backup_list` / `delete_backup` / `restore_backup` / `check_data_recovery_needed`
- `delete_backup`：basename 校验防路径穿越 + 仅允许 `orders_backup_*.db` 格式
- 启动时不自动备份（仅保留手动入口）

**恢复安全链（卡 172，`db.py:2080` `restore_backup`）**——六步顺序，任一步失败即停止且不覆盖现库：

1. **守卫**：`basename(filename) != filename` 拦路径穿越；文件名必须 `orders_backup_*.db`；源文件必须存在
2. **源预检**：`_validate_sqlite_file(src)`（`db.py:2002`）——失败立即返回，**不备份、不触碰现库**
3. **同目录临时副本 + 复检**：`tempfile.mkstemp(dir=db_dir, prefix='.restore_')` → `copy2` → 再校验一次（防复制中断或源文件在复制期间变化）
4. **保护性备份**：现库存在时先 `_do_backup()`，失败即取消恢复（`protective_path` 记录本次备份供回滚）
5. **原子替换 + 初始化**：`os.replace(tmp_path, DB_PATH)` 避免半个文件；随后 `init_db()`，若抛异常则 `_rollback_to_protective(protective_path, db_dir)`（`db.py:2049`）自动回滚，回滚也失败时返回「请保留现有文件并人工处理」而不静默
6. **仅最终成功后失效缓存**：`_invalidate_choices_cache()` + `_invalidate_stage_flows_cache()`；`finally` 恒执行 `_remove_tmp_and_sidecars(tmp_path)`（`db.py:2036`）清临时主文件与 `-wal`/`-shm` 边车

**原始库保护（D13）**：`orders.pre-v1.5.0.db` 是 v1.5.0 枚举迁移前的一次性重命名备份，**永不写入、永久保留**，任何清理动作不得触碰；与 `backups/` 下的常规备份是两套东西。

### 6.7 订单删除自动清理

`delete_order()` 删除订单后检查 `commission_type` / `current_stage` / `source` 三个字段，无订单使用则自动移除对应 `cal_*` 颜色配置。

### 6.8 收款状态机（Spec 26，只进不退）

- **收齐自动结算**：`add_payment`/`update_payment` 单事务内先模拟校验 Σ笔（≤ 净额+0.01 容差 `PAYMENT_EPSILON`、笔数≤50）→ 写流水 → Σ笔 ≥ actual_received−0.01 且当前 `enum_code_or_raw('payment',…)=='unpaid'`（未收款）→ 经 `update_order` 置 `settled`（已结算）走既有归档链 + 客户重算；杜绝 with-return 半成品提交。免收互斥同样经 `enum_code_or_raw(…)=='waived'` 判定（Spec 33 比较点统一兼容读）
- **只进不退（D4）**：删笔/改笔不回退状态、不撤销归档；不一致由详情页警告条呈现（状态已结算但 Σ笔 < 净额−0.01）
- **金额冲突拦截（D12①）**：update_order 金额改小致净额 < 已到账−0.01 → 拒绝并整单回滚
- **切换双向保护（D12③）**：installment 有收款记录拒绝切回 simple（先清空再切）；simple 已收齐切 installment 同事务自动生成 1 笔初始收款 [有效排期日或今日, actual_received]（0 元跳过留痕）
- **免收互斥**：免收状态与 installment 互斥（切向与置免收双向拦截）
- `update_order` 返回 `(ok, err)` tuple；payment_mode 经 `order_columns` 白名单登记（D10 保持性）

---

## 七、前端架构

### 7.1 页面骨架

侧边栏（7 导航含「小工具」+新建 CTA，240px/56px 折叠态，localStorage 记忆；`hx-preserve="true"` 跨导航保留 DOM）+ 主内容区（`hx-boost="true"` 局部导航，仅 `<main>` AJAX 刷新 + View Transitions 平滑转场）；全局容器：`#center-modal`（680px 居中模态）· `#edit-drawer`（480px 右抽屉）· `#toast-container`（右下角）。

### 7.2 三种表单模式

| 模式 | 触发 | 提交返回 |
|---|---|---|
| 居中模态 | `?modal=1` | modal_success.html |
| 右抽屉 | `?inline=1` | JSON + HX-Trigger |
| 完整页 | 默认 | redirect |

### 7.3 app.js 模块（83 函数）

模态框/抽屉 · 看板（SortableJS） · 回执小票 · Toast · 导出 · 侧边栏 · Lightbox · 图片上传 · 模块定制器 · 错误日志 · ColorPicker（原生零依赖 ~230 行）· 图表主题 helper · gridstack 布局管理 · 小工具桥接（`recordRecentTool` + closeCenterModal/showToast 事件监听 + `.tool-htmx-form` 400 换入）· **页面导航**：`runPageInit()` 三路径统一初始化（DOMContentLoaded / htmx:afterSettle boosted / htmx:historyRestore）· `updateSidebarActive()` 侧边栏高亮 · `onceBind()` 会话级一次性绑定 · 脏数据拦截（form `__formDirty` → `htmx:beforeRequest` boosted GET confirm）

### 7.4 vendor 本地按需加载

HTMX+Lucide+JsBarcode（base.html 全局）；Frappe Gantt / Chart.js / FullCalendar / SortableJS / gridstack（base.html head 全局加载，hx-boost 导航不再重复加载）

---

## 八、安全模型

本地单用户定位，以来源校验替代鉴权：

- **CORS 白名单**：`127.0.0.1/localhost` · `mihuashi.com` · `chrome-extension://` · `moz-extension://`；仅 `/api/*` 生效
- **`_is_local_origin()`**：取第一个非空 Origin/Referer 判定；缺失按同源导航放行
- **`/api/shutdown`**：仅 POST + 来源校验
- **XSS**：`render_notes` 先 `html.escape(quote=True)` 再解析图片标记
- **SQL**：全量 `?` 参数绑定
- **输入校验**：Pydantic ge=0、日期 ISO 归一、跨字段校验

---

## 九、设置系统（settings 表 90+ 键，随 `cal_*` 着色动态增减）

| 分组 | 键模式 | 说明 |
|---|---|---|
| 全局主题 | `theme_*`（11） | bg/surface/sidebar/text/text_secondary/border/accent/link/success/warning/danger |
| 阶段色（旧） | `stage_*`（7） | 仅作读取回退，新值落 `cal_stage_*` |
| 日历调色板 | `cal_{mode}_{label}`（~31） | 5 模式 × 各值独立色 |
| 着色模式 | `calendar_color_mode`（1） | 持久化着色模式选择 |
| 选择列表 | `*_list`（4） | 用户自定义列表 |
| 阶段流程 | `stage_flows`（1） | 流程预设 JSON 数组 |
| 平台/费率 | `platform_sources` · `*_fee_*`（~10） | 平台来源集合与费率 |
| 外观 | `font_size` · `font_family`（2） | 字号/字体 |
| 主题扩展 | `custom_themes` · `palette_presets` 等（4） | 自定义主题+配色预设 |
| 收入布局 | `income_layout`（1） | gridstack 布局 JSON |
| 备份 | `backup_dir`（1） | 自定义备份目录 |
| 时薪 | `hourly_rate_enabled`（1） | 时薪功能全局开关 |
| 小工具 | `pricelist_meta`（1） | 价目表菜单标题/附注 JSON（Spec 22） |
| 小工具 | `receipt_draft`（1） | 小票草稿整体 JSON（Spec 23，键移除回退默认草稿） |
| 小工具 | `receipt_mult_presets`（1） | 单品倍率快捷预设 JSON（Spec 24，损坏/缺失回退默认） |
| 其他 | `api_token` · `shortcuts_json` 等 | API 预留/快捷键/杂项 |

### 9.1 主题引擎（Spec 32）

服务端渲染时由 `_build_theme_css(settings)`（`app.py:353`，经 `base.html` 注入 `<style>`）生成 `:root` 变量块：

- **亮度判深**：`_relative_luminance(theme_bg)` < 0.35 判深色主题（缺键/非法值回退浅色）
- **accent 对比字色**：`db._contrast_text_color(accent)` 注入 `--color-accent-text`，`--color-accent-hover` 向对比极混合 15%；按钮文字统一消费 accent-text，白/黑 accent 均可见
- **深色六项派生**（用户未显式配置时）：text-tertiary / border-strong / hover / active / sidebar-hover / placeholder
- **warning 主题派生**（卡 154）：浅色主题下 `--color-warning` 压暗为 `color-mix(warning 55%, #000)` 深琥珀（亮琥珀黄作文字对浅底 ratio≈1.8 不可见），深色主题保持用户原值
- **color-scheme**：按深色判定注入 `dark`/`light`，原生控件（select/date 弹层）随主题配色
- **预览同步**：settings.html `applyPreset`/单色微调实时同步上述派生（`_relLuminance`/`_syncColorScheme`/`_syncAccentDerived`/`_warningEffective` 等 helper），预览态与保存后一致
- **缓存穿透**：app.css/app.js 引用带 `?v={{ APP_VERSION }}`，改静态资源必升 APP_VERSION（当前 1.5.0，`app.py:102`）

---

## 十、启动方式

| 方式 | 命令 | 端口 |
|---|---|---|
| 启动器（推荐） | `python launcher.py` | 1096 |
| 直接运行 | `python app.py --port 1096` | 1096 |
| 批处理 | `run.bat` | 1096 |
| 打包 | PyInstaller exe（`build.spec`） | 1096 |
| 一键构建（卡 88） | `build.bat`（自动建/复用 `.venv-build` 干净环境 → 锁定版本装依赖 → 出包 → 体积对比追加 `logs/build-size.log`；exe 运行中守卫拒绝覆盖） | — |

---

## 十一、设计决策记录

1. **SQLite + WAL**：本地零配置、单文件便携
2. **HTMX + Jinja2 而非 SPA**：单体工具无前端框架复杂度
3. **中央注册表 + 合并语义**：选择列表去硬编码，标准值永不丢失
4. **终态元数据化**：免疫 auto-discover 追加值对位置约定的破坏
5. **归档唯一入口 + 快照列**：completed_at/is_overdue 归档时写入不再回算
6. **费率快照落库**：历史订单金额与设置页费率脱钩
7. **复购 + days_remaining 查询时计算**：消除写入式派生列的腐烂面
8. **原生 ColorPicker**：零依赖完全可控
9. **来源校验替代鉴权**：本地单用户最小成本阻断跨站读写
10. **vendor 本地化**：离线可用，避免每页 ~1MB JS
11. **备份路径可配置**：用户可自定义备份位置，默认 exe 同级
12. **口径分离 + 事件流（Spec 26）**：工作口径看进展、现金口径看到账；整单虚拟事件数学等价保旧数据逐位不变；收款状态机只进不退
13. **主题引擎派生变量（Spec 32）**：亮度判深（阈值 0.35）+ accent 对比字色 + 深色六项派生 + warning 浅色压暗，服务端注入与设置页预览同口径，双主题对比度 ratio≥3 审计保障
14. **hx-boost 局部导航（卡 156）**：`<body hx-boost>` + `<aside hx-preserve>` 实现类 SPA 导航（仅 `<main>` AJAX 刷新），View Transitions 平滑转场；`runPageInit()` 消费式初始化三路径统一；`HX-Boosted` 请求头分支返回完整页（非 fragment）
15. **枚举编码化（Spec 33，v1.5.0）**：存储值全面英文 code、展示层恒中文——code 是稳定内部标识（不随文案改动而失效），label 可自由改中文。三层翻译字典（`CODE_TO_LABEL` 一对一反查 / `LEGACY_LABEL_TO_CODE` 永久冻结识别输入 / `RESERVED_ENUM_CODES` 宽判据保留字）+ `enum_code_or_raw` 兼容读，使 code 空间与未迁移中文空间可共存。迁移门 `user_version=33` + M0-M19 幂等引擎 + 升级前重命名备份 `orders.pre-v1.5.0.db`（永不写入），失败降级兼容模式启动（全 spec 唯一不可逆数据动作，D11/D13）。`PAID_STATUSES={'settled'}` 与 `ARCHIVE_PAID_STATUSES={'settled','waived'}` 强制分离（D7）：免收可归档不计收入。choice_items 表收编散落的来源/费率 settings 键

---

## 十二、已知约束（反目标）

不 SaaS 化/云同步 · 不引入 SPA 框架 · 不做 AI 自动排期 · 不做多语言 · 不做微服务/消息队列 · 保持 PyInstaller 可打包运行 · 前端库本地 vendor 化。

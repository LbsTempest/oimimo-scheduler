# 米画师集成操作说明

## 开发安装

从仓库目录创建虚拟环境并安装依赖：

```bash
uv venv .venv
UV_CACHE_DIR="$PWD/.uv-cache" uv pip install --python .venv/bin/python -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers" UV_CACHE_DIR="$PWD/.uv-cache" uv run --no-project --python .venv/bin/python playwright install chromium
```

Playwright 浏览器运行时、Profile 和虚拟环境都在仓库内的忽略目录，避免凭证或开发产物进入 Git，也避免写到项目目录之外。

## 人工登录与勘察

开发探测命令：

```bash
.venv/bin/python -m integrations.mihuashi.cli_inspect login
.venv/bin/python -m integrations.mihuashi.cli_inspect inspect
.venv/bin/python -m integrations.mihuashi.cli_inspect dry-run
```

以上命令默认调用系统 Google Chrome，并使用项目内独立 Profile；若要改用项目内 Playwright Chromium，在命令末尾加 `--browser chromium`。
`dry-run` 只预览新增、变更字段和冲突类型，不写入订单或外部 ID 映射。

由本人在可见浏览器中登录并手动浏览已接订单列表、分页和详情。CLI 只打印米画师 JSON 请求的方法、状态码、路径和 Content-Type；不记录查询参数、请求头或响应体。完成后按 `docs/investigation.md` 记录事实，样本中的客户信息必须替换为合成值。

不要同时运行 CLI 和 oimimo 内置登录/同步任务，它们会争用同一个 Profile。出现验证码、验证提示、403 或限流时停止并转由本人处理。

## 当前集成状态

- oimimo 设置页的“系统”区域已包含连接窗口、同步状态和设置入口。
- 设置 API 只接受本机 Host 与精确同源的写入请求；后台任务串行使用 Profile 文件锁。
- 会话失效和平台拒绝/限流会持久化暂停状态；临时错误使用指数退避（约 1 分钟起，最多 1 小时并带抖动），退出重启后继续遵守等待时间。
- 数据库映射表、任务记录、字段权责保护和内部业务 upsert 已实现。
- **采集器尚未配置**：没有经本人账号实测的订单页面 URL、接口字段或分页规则。设置页保持同步禁用；连接窗口可以供人工登录，但 UI 不宣称已经验证登录态。
- 报价单独保存，不当作到账；排期、绘制阶段、实际收款和人工笔记不会由同步服务修改。

在采集器就绪前，调度器不会发出米画师订单请求。打开 oimimo 后 Web 与管理线程由原启动器统一管理；退出进程时管理线程停止并尝试关闭当前浏览器。

## 数据恢复

集成新增的 SQLite 表由 `db.init_db()` 使用 `CREATE TABLE IF NOT EXISTS` 幂等创建。升级前仍按 oimimo 现有备份流程备份 `orders.db` 和 `uploads/`。映射表与同步记录位于同一个 `orders.db`，恢复数据库时一并恢复。

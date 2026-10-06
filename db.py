"""排单工具 — 数据库层 (sqlite3)

连接管理、建表、CRUD 查询函数。
"""

import sqlite3
import os
import re
import sys
import json
import shutil
import logging
import tempfile
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)


def data_dir() -> str:
    """数据目录：orders.db / exports/ 等可写文件所在处。
    PyInstaller 冻结时为 exe 所在目录（持久化）；开发时为 db.py 所在目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


DB_PATH = os.path.join(data_dir(), 'orders.db')

# P22a 数据备份：默认与 orders.db 同目录下的 backups/ 子目录（可通过设置自定义）
DEFAULT_BACKUP_DIR = os.path.join(data_dir(), 'backups')
MAX_BACKUPS = 10


def get_backup_dir() -> str:
    """获取备份目录：用户自定义 > 默认值（exe 同级 backups/）。
    每次调用实时查 settings 表，确保用户切换路径后立即生效。
    """
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'backup_dir'"
        ).fetchone()
        conn.close()
        if row and row['value'] and os.path.isdir(row['value']):
            return row['value']
    except Exception as e:
        logger.error('读取备份目录设置失败: %s', e)
    return DEFAULT_BACKUP_DIR


def set_backup_dir(path: str) -> tuple:
    """设置备份目录：校验路径存在且可写。
    返回 (True, 消息) 或 (False, 错误信息)。
    """
    path = path.strip()
    if not path:
        update_settings({'backup_dir': ''})
        return (True, f'已恢复默认备份目录：{DEFAULT_BACKUP_DIR}')
    if not os.path.isdir(path):
        return (False, '目录不存在')
    if not os.access(path, os.W_OK):
        return (False, '目录无写入权限')
    update_settings({'backup_dir': path})
    return (True, f'备份目录已设置为：{path}')


def get_db() -> sqlite3.Connection:
    """获取数据库连接（row_factory + 外键约束）

    P19-F10：journal_mode=WAL 为库级持久设置（写入 DB header），进程启动 init_db
    一次性设置即可，不再每连接重复执行；foreign_keys 是连接级非持久 pragma，
    必须每连接设置，否则外键约束（SET NULL/CASCADE）静默失效，故保留。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction():
    """写入事务（P19-F5）：单连接，正常退出 commit、异常 rollback、始终 close。

    多步写入（插入+重算、重命名+设置更新、批量操作）必须包在同一事务里，
    中途失败整体回滚，不留半成品。业务函数通过 `conn=None` 参数复用事务连接。
    """
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════
# Spec 33 卡 165：幂等自动迁移引擎（M0-M19）
# 唯一不可逆数据动作——四重兜底：M0 预审计 + 原文件 rename 保留 +
# 失败降级兼容模式 + user_version 自愈
# ═══════════════════════════════════════════════════════════

ENUM_CODE_MIGRATION_VERSION = 33


def _enum_codes_migrated(conn) -> bool:
    """PRAGMA user_version >= 33 即已迁移（D11：不用 settings 标记键）。"""
    return conn.execute('PRAGMA user_version').fetchone()[0] >= ENUM_CODE_MIGRATION_VERSION


def _pre_migration_backup() -> str | None:
    """D13 双重保护：WAL checkpoint → rename 原文件为 orders.pre-v1.5.0.db 永久保留。
    空库/已备份过 → 跳过返回 None。返回备份路径。"""
    import shutil
    db_path = DB_PATH
    if not os.path.exists(db_path):
        return None
    bak = db_path.replace('orders.db', 'orders.pre-v1.5.0.db')
    if os.path.exists(bak):
        return bak  # 已备份过（自愈重跑场景）
    try:
        c = sqlite3.connect(db_path)
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        c.close()
    except Exception as e:
        logger.error('M-backup WAL checkpoint 失败: %s', e)
    shutil.move(db_path, bak)
    logger.info('enum-mig 原文件已 rename 保留: %s', ascii(bak))
    return bak


def _map_stage_flow_text(raw):
    """M6/M7/M15 共用纯函数：JSON 数组里每个 stage 的 name 换 code。
    返回 (新文本, 变更次数)。非 JSON / 非 list 原样返回。"""
    if not raw:
        return raw, 0
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw, 0
    if not isinstance(data, list):
        return raw, 0
    changed = 0
    stage_map = LEGACY_LABEL_TO_CODE.get('stage', {})
    for entry in data:
        stages = entry.get('stages') if isinstance(entry, dict) else None
        targets = stages if isinstance(stages, list) else ([entry] if isinstance(entry, dict) else [])
        for st in targets:
            if not isinstance(st, dict) or 'name' not in st:
                continue
            new = stage_map.get(st['name'], st['name'])
            if new != st['name']:
                st['name'] = new
                changed += 1
    return json.dumps(data, ensure_ascii=False), changed


def _rename_settings_key(conn, prefix: str, mapping: dict) -> int:
    """M8/M9/M10/M17 共用纯函数：把 cal_<kind>_<中文后缀> 换键为 cal_<kind>_<code>。
    只处理命中映射的键；自定义值键不动。返回换键数。"""
    rows = conn.execute(
        'SELECT key, value FROM settings WHERE key LIKE ?', (prefix + '%',)
    ).fetchall()
    n = 0
    for key, value in rows:
        label = key[len(prefix):]
        code = mapping.get(label)
        if not code or code == label:
            continue
        new_key = prefix + code
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                     (new_key, value))
        conn.execute('DELETE FROM settings WHERE key = ?', (key,))
        n += 1
    return n


def _m0_pre_audit(conn) -> list:
    """M0 保留字碰撞预审计（D17 第 3 层）。返回冲突列表；非空即中止迁移。"""
    conflicts = []
    for kind, column in ENUM_FIELD.items():
        legal_codes = set(CODE_TO_LABEL.get(kind, {}))
        legacy_labels = set(LEGACY_LABEL_TO_CODE.get(kind, {}))
        rows = conn.execute(
            f'SELECT {column} AS v, COUNT(*) AS n FROM orders '
            f'WHERE {column} IS NOT NULL GROUP BY {column}'
        ).fetchall()
        for r in rows:
            v = r[0] if isinstance(r, tuple) else r['v']
            if not isinstance(v, str):
                continue
            if is_ambiguous_in_column(kind, v):
                conflicts.append(
                    f'{column}(kind={kind}) 存自定义值 {ascii(v)}，与本列系统 code 同名，'
                    f'转码后无法区分，影响 {r[1] if isinstance(r, tuple) else r["n"]} 行')
    return conflicts


def _migrate_enum_codes(conn) -> None:
    """M0-M18 全项迁移（单事务内调用，不含 M19——M19 是常态守卫独立运行）。
    异常由调用方捕获并走 D12 降级。"""
    # M0
    conflicts = _m0_pre_audit(conn)
    if conflicts:
        raise RuntimeError('M0 保留字碰撞预审计未通过：\n' + '\n'.join(conflicts))

    # 确保 choice_items 表存在
    ensure_choice_items_table(conn)

    # M4/M5 前置：seed 得到 source/commission_type 的完整 label→code 表
    _all_maps = seed_choice_items(conn)
    src_map = _all_maps.get('source', {})
    cmt_map = _all_maps.get('commission_type', {})

    col_maps = {
        'current_stage': LEGACY_LABEL_TO_CODE.get('stage', {}),
        'ddl_status': LEGACY_LABEL_TO_CODE.get('ddl', {}),
        'payment_status': LEGACY_LABEL_TO_CODE.get('payment', {}),
        'source': src_map,
        'commission_type': cmt_map,
    }

    # M1-M5：CASE 批量转码（不带 updated_at，D16）
    for kind, column in ENUM_FIELD.items():
        mapping = {k: v for k, v in col_maps[column].items() if k != v}
        if not mapping:
            continue
        cases = ' '.join(f"WHEN ? THEN ?" for _ in mapping)
        params = []
        for old, code in mapping.items():
            params.extend([old, code])
        olds = list(mapping.keys())
        ph = ','.join('?' * len(olds))
        cur = conn.execute(
            f'UPDATE orders SET {column} = CASE {column} {cases} ELSE {column} END '
            f'WHERE {column} IN ({ph})', params + olds)
        if cur.rowcount:
            logger.info('enum-mig M1-5/%s: %d 行', column, cur.rowcount)

    # M6 orders.stage_flow
    m6 = 0
    for row in conn.execute(
            "SELECT id, stage_flow FROM orders WHERE stage_flow IS NOT NULL AND stage_flow!=''").fetchall():
        oid = row[0] if isinstance(row, tuple) else row['id']
        raw = row[1] if isinstance(row, tuple) else row['stage_flow']
        new, n = _map_stage_flow_text(raw)
        if n:
            conn.execute('UPDATE orders SET stage_flow=? WHERE id=?', (new, oid))
            m6 += n
    if m6:
        logger.info('enum-mig M6/stage_flow: %d 处阶段名', m6)

    # M7 order_templates.data_json（含内层二次字符串化）
    m7 = 0
    try:
        for row in conn.execute(
                'SELECT id, data_json FROM order_templates WHERE data_json IS NOT NULL').fetchall():
            tid = row[0] if isinstance(row, tuple) else row['id']
            dj = row[1] if isinstance(row, tuple) else row['data_json']
            try:
                obj = json.loads(dj)
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            touched = 0
            for field in ('source', 'current_stage', 'payment_status', 'ddl_status', 'commission_type'):
                if field in obj and isinstance(obj[field], str):
                    m = col_maps.get(field, {})
                    nv = m.get(obj[field], obj[field])
                    if nv != obj[field]:
                        obj[field] = nv
                        touched += 1
            inner = obj.get('stage_flow')
            if isinstance(inner, str):
                new_inner, n = _map_stage_flow_text(inner)
                if n:
                    obj['stage_flow'] = new_inner
                    touched += n
            if touched:
                conn.execute('UPDATE order_templates SET data_json=? WHERE id=?',
                             (json.dumps(obj, ensure_ascii=False), tid))
                m7 += touched
    except Exception:
        pass  # order_templates 表可能不存在
    if m7:
        logger.info('enum-mig M7/order_templates: %d 处', m7)

    # M8-M10 settings cal_* 换键
    _CAL_PREFIX = {'cal_stage_': 'current_stage', 'cal_ddl_': 'ddl_status',
                   'cal_payment_': 'payment_status', 'cal_source_': 'source',
                   'cal_commission_': 'commission_type'}
    m8_total = 0
    for prefix, column in _CAL_PREFIX.items():
        m8_total += _rename_settings_key(conn, prefix, col_maps[column])
    if m8_total:
        logger.info('enum-mig M8-10/cal_*: %d 键', m8_total)

    # M11 default_fee_* → choice_items.default_fee_pct 列
    m11 = 0
    for row in conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'default_fee_%'").fetchall():
        key = row[0] if isinstance(row, tuple) else row['key']
        value = row[1] if isinstance(row, tuple) else row['value']
        label = key[len('default_fee_'):]
        code = src_map.get(label)
        if not code:
            continue
        try:
            conn.execute('UPDATE choice_items SET default_fee_pct=? WHERE kind=? AND code=?',
                         (float(value or 0), 'source', code))
            conn.execute('DELETE FROM settings WHERE key=?', (key,))
            m11 += 1
        except (TypeError, ValueError):
            logger.warning('enum-mig M11 跳过非数值费率 %s', ascii(key))
    if m11:
        logger.info('enum-mig M11/default_fee: %d 键搬入列', m11)

    # M12 platform_sources → choice_items.is_platform 列
    plat_row = conn.execute("SELECT value FROM settings WHERE key='platform_sources'").fetchone()
    if plat_row:
        plats = [x.strip() for x in (plat_row[0] or '').split(',') if x.strip()]
        for label in plats:
            code = src_map.get(label)
            if code:
                conn.execute('UPDATE choice_items SET is_platform=1 WHERE kind=? AND code=?',
                             ('source', code))
        conn.execute("DELETE FROM settings WHERE key='platform_sources'")
        logger.info('enum-mig M12/platform_sources: %d 个搬入列', len(plats))

    # M13 source_list / commission_type_list 删键（顺序已进 sort_order）
    for key in ('source_list', 'commission_type_list'):
        conn.execute('DELETE FROM settings WHERE key=?', (key,))

    # M14 payment_status_list 直删
    conn.execute("DELETE FROM settings WHERE key='payment_status_list'")

    # M15 settings.stage_flows JSON
    sf = conn.execute("SELECT value FROM settings WHERE key='stage_flows'").fetchone()
    if sf:
        new, n = _map_stage_flow_text(sf[0])
        if n:
            conn.execute("UPDATE settings SET value=? WHERE key='stage_flows'", (new,))
            logger.info('enum-mig M15/stage_flows: %d 处', n)

    # M16 stats_lab_presets JSON
    sp = conn.execute("SELECT value FROM settings WHERE key='stats_lab_presets'").fetchone()
    if sp:
        m16 = 0
        try:
            presets = json.loads(sp[0])
        except (ValueError, TypeError):
            presets = []
        for p in presets if isinstance(presets, list) else []:
            filters = (p.get('config') or {}).get('filters') or {}
            for field in ENUM_FIELD:
                if field in filters:
                    m = col_maps.get(field, {})
                    nv = m.get(filters[field], filters[field])
                    if nv != filters[field]:
                        filters[field] = nv
                        m16 += 1
        if m16:
            conn.execute("UPDATE settings SET value=? WHERE key='stats_lab_presets'",
                         (json.dumps(presets, ensure_ascii=False),))
            logger.info('enum-mig M16/stats_lab_presets: %d 处', m16)

    # M17 palette_presets colors 字典换键
    pp = conn.execute("SELECT value FROM settings WHERE key='palette_presets'").fetchone()
    if pp:
        m17 = 0
        try:
            presets = json.loads(pp[0])
        except (ValueError, TypeError):
            presets = []
        for preset in presets if isinstance(presets, list) else []:
            colors = preset.get('colors')
            if not isinstance(colors, dict):
                continue
            newcolors = {}
            for k, v in colors.items():
                nk = k
                for prefix, column in _CAL_PREFIX.items():
                    if k.startswith(prefix):
                        label = k[len(prefix):]
                        code = col_maps[column].get(label)
                        if code and code != label:
                            nk = prefix + code
                            m17 += 1
                        break
                newcolors[nk] = v
            if m17:
                preset['colors'] = newcolors
        if m17:
            conn.execute("UPDATE settings SET value=? WHERE key='palette_presets'",
                         (json.dumps(presets, ensure_ascii=False),))
            logger.info('enum-mig M17/palette_presets: %d 键', m17)

    # M18 platform_fee_* 死配置直删
    pf = conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'platform_fee_%'").fetchone()[0]
    if pf:
        conn.execute("DELETE FROM settings WHERE key LIKE 'platform_fee_%'")
        logger.info('enum-mig M18/platform_fee_*: %d 键删除', pf)

    # 版本闸
    conn.execute(f'PRAGMA user_version = {ENUM_CODE_MIGRATION_VERSION}')
    logger.info('enum-mig 完成，user_version = %d', ENUM_CODE_MIGRATION_VERSION)


def _m19_normalize_enum_columns(conn) -> None:
    """Spec33 M19 常态守卫：把 orders 表 3 列中命中冻结表已知旧值的行归一到 code。

    只认 LEGACY_LABEL_TO_CODE 已知旧值，绝不猜测（自定义阶段名/脏值原样保留）。
    天然幂等：已是 code 的值不命中 WHERE IN 子句。
    不带 updated_at（D16：避免 ETag 漂移惊动已订阅日历客户端）。
    """
    for kind, column in (('stage', 'current_stage'), ('ddl', 'ddl_status'), ('payment', 'payment_status')):
        mapping = LEGACY_LABEL_TO_CODE.get(kind, {})
        if not mapping:
            continue
        old_vals = list(mapping.keys())
        placeholders = ','.join('?' * len(old_vals))
        # 构建 CASE 表达式：旧值 → code
        case_parts = []
        params = []
        for old, code in mapping.items():
            case_parts.append(f"WHEN ? THEN ?")
            params.extend([old, code])
        case_sql = f"CASE {column} {' '.join(case_parts)} ELSE {column} END"
        cur = conn.execute(
            f"UPDATE orders SET {column} = {case_sql} WHERE {column} IN ({placeholders})",
            params + old_vals
        )
        if cur.rowcount:
            logger.info('enum-mig M19/%s: %d 行归一', column, cur.rowcount)


def init_db():
    """建表 + 索引（幂等）"""
    conn = get_db()
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS customers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL UNIQUE,
            platform_url    TEXT,
            former_names    TEXT,                             -- 曾用名（逗号分隔，平台 ID 命中且改名时归档旧名）
            preferences     TEXT,
            notes           TEXT,
            tags            TEXT,
            total_spent     REAL    NOT NULL DEFAULT 0.0,
            purchase_count  INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id     INTEGER,
            project_name    TEXT    NOT NULL,
            source          TEXT    NOT NULL DEFAULT '米画师',
            is_commercial   INTEGER NOT NULL DEFAULT 0,
            commission_type TEXT,
            current_stage   TEXT    NOT NULL DEFAULT '待开始',
            ddl_status      TEXT    NOT NULL DEFAULT '正常',
            deposit         REAL    NOT NULL DEFAULT 0.0,
            balance         REAL    NOT NULL DEFAULT 0.0,
            platform_fee    REAL    NOT NULL DEFAULT 0.0,
            platform_fee_pct REAL,                          -- 手续费率快照 %（P19-F9：落库不回算，与设置页费率脱钩）
            income          REAL    NOT NULL DEFAULT 0.0,
            actual_received  REAL   NOT NULL DEFAULT 0.0,
            payment_status  TEXT    NOT NULL DEFAULT '未收款',
            is_archived     INTEGER NOT NULL DEFAULT 0,
            is_repeat       INTEGER NOT NULL DEFAULT 0,
            repeat_count    INTEGER NOT NULL DEFAULT 0,
            notes           TEXT,
            custom_color    TEXT,                             -- 日历自定义颜色 #RRGGBB
            platform_url    TEXT,                             -- 米画师项目链接
            page_deadline   TEXT,                             -- 页面截稿日（参考）
            image_url       TEXT,                             -- 作品预览图 URL（/uploads/orders/<id>/preview.webp）
            image_path      TEXT,                             -- 原图相对路径（orders/<id>/original<ext>）
            has_image       INTEGER NOT NULL DEFAULT 0,       -- 是否有作品图片
            completed_at    TEXT,                             -- 实际完成归档时间（P15a 统计口径）
            is_overdue      INTEGER NOT NULL DEFAULT 0,       -- 是否逾期完成（P15b）
            estimated_hours REAL,                             -- 预估工时（小时，P20b 时薪）
            work_hours      REAL,                             -- 实际工时（小时，P20b 时薪）
            exclude_hourly  INTEGER NOT NULL DEFAULT 0,       -- 不参与时薪统计（P20b 单订单排除）
            scheduled_start TEXT,
            scheduled_end   TEXT,
            sort_order      INTEGER NOT NULL DEFAULT 0,
            stage_flow      TEXT,                             -- 本单阶段流程快照 JSON（Spec12 阶段进度可视化）
            payment_mode    TEXT    NOT NULL DEFAULT 'simple', -- 收款模式 simple=整单/installment=分期（Spec26）
            created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_orders_customer    ON orders(customer_id);
        CREATE INDEX IF NOT EXISTS idx_orders_stage       ON orders(current_stage);
        CREATE INDEX IF NOT EXISTS idx_orders_scheduled   ON orders(scheduled_start, scheduled_end);
        CREATE INDEX IF NOT EXISTS idx_orders_sort        ON orders(sort_order);
        CREATE INDEX IF NOT EXISTS idx_orders_archived    ON orders(is_archived);
        -- P19-F10：统计高频 WHERE 列索引（收入按 payment_status、归档月统计按 completed_at）；
        -- is_archived 已有单列索引，本地行数量级下复合索引收益不明显，取舍不加（注释备案）。
        CREATE INDEX IF NOT EXISTS idx_orders_completed_at   ON orders(completed_at);
        CREATE INDEX IF NOT EXISTS idx_orders_payment_status ON orders(payment_status);

        -- 订单多图表（P15d 一对多；orders 单图三列保留为封面兼容）
        CREATE TABLE IF NOT EXISTS order_images (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id    INTEGER NOT NULL,
            image_url   TEXT,                                 -- 预览图路径 /uploads/orders/<id>/imgs/<key>/preview.webp
            image_path  TEXT,                                 -- 原图相对路径 orders/<id>/imgs/<key>/original<ext>
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_order_images_order ON order_images(order_id, sort_order);

        -- 设置表（键值对存储主题色等配置）
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- 订单模板表（P18-F3：可复用字段快照，不污染 orders 表）
        CREATE TABLE IF NOT EXISTS order_templates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            data_json   TEXT    NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );

        -- 回复模板表（Spec 22 / 002 小工具：类剪贴板话术库，分组管理）
        CREATE TABLE IF NOT EXISTS reply_templates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name  TEXT    NOT NULL DEFAULT '未分组',
            title       TEXT    NOT NULL,
            content     TEXT    NOT NULL,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );

        -- 价目表项目表（Spec 22 / 003 小工具：菜单式价目）
        CREATE TABLE IF NOT EXISTS pricelist_items (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            category           TEXT    NOT NULL DEFAULT '默认',
            name               TEXT    NOT NULL,
            price              REAL    NOT NULL DEFAULT 0,
            price_max          REAL,                          -- 价格上限（2026-08-12 UX 改造：可选，>price 时展示区间）
            unit               TEXT    NOT NULL DEFAULT '',
            description        TEXT    NOT NULL DEFAULT '',
            example_image_path TEXT,                         -- 旧单例图列（已迁移 pricelist_images，仅为迁移源保留）
            sort_order         INTEGER NOT NULL DEFAULT 0,
            created_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );

        -- 价目表例图表（2026-08-12 UX 改造：多例图最多 3 张，对齐 P15d order_images 模式）
        CREATE TABLE IF NOT EXISTS pricelist_images (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     INTEGER NOT NULL,
            image_path  TEXT,                                 -- 预览图相对路径 pricelist/<iid>/preview_<key>.webp
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (item_id) REFERENCES pricelist_items(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pricelist_images_item ON pricelist_images(item_id, sort_order);

        -- 小票制品表（Spec 23 小票打印机：parent_id 自引用主子嵌套；NULL=主项，非空=附加服务子行）
        CREATE TABLE IF NOT EXISTS receipt_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            price      REAL    NOT NULL DEFAULT 0,   -- 主项单价；附加子行=单项加价金额
            qty        REAL    NOT NULL DEFAULT 1,
            parent_id  INTEGER REFERENCES receipt_items(id) ON DELETE CASCADE,
            is_gift    INTEGER NOT NULL DEFAULT 0,   -- 1=赠品（划线价，计 0）
            multiplier     REAL    NOT NULL DEFAULT 1.0,   -- 单品倍率（商用×2/买断×3，Spec 24）
            mult_label     TEXT    NOT NULL DEFAULT '',    -- 倍率标签（商用/买断，小票角标）
            discount_type  TEXT    NOT NULL DEFAULT 'none',-- 单品折扣形态 none/amount/rate
            discount_value REAL    NOT NULL DEFAULT 0,     -- 金额 或 中文折数（8.8 折）
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );

        -- 小票样式模板表（Spec 23：config_json 只存样式与文案，D14 不存制品/计算参数）
        CREATE TABLE IF NOT EXISTS receipt_templates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            config_json TEXT    NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );

        -- 分期收款流水表（Spec 26：1:N 收款事件流，一笔一行；到账即计入月度收入）
        CREATE TABLE IF NOT EXISTS order_payments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id   INTEGER NOT NULL,
            paid_at    TEXT    NOT NULL,                       -- 到账日期 YYYY-MM-DD（月度归属依据）
            amount     REAL    NOT NULL CHECK(amount >= 0),    -- 到卡净额（抽成订单级一次，D1）
            note       TEXT,                                   -- 备注（定金/阶段款/尾款，可选）
            created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_payments_order   ON order_payments(order_id);
        CREATE INDEX IF NOT EXISTS idx_payments_paid_at ON order_payments(paid_at);
    """)

    # Spec 24 小票计算模型重设计：receipt_items 补齐单品倍率/折扣列（新建库已在建表时定义，此处幂等）
    for col, decl in (
        ('multiplier', 'REAL NOT NULL DEFAULT 1.0'),
        ('mult_label', "TEXT NOT NULL DEFAULT ''"),
        ('discount_type', "TEXT NOT NULL DEFAULT 'none'"),
        ('discount_value', 'REAL NOT NULL DEFAULT 0'),
    ):
        try:
            conn.execute(f"ALTER TABLE receipt_items ADD COLUMN {col} {decl}")
        except Exception:
            pass  # 列已存在

    # 写入默认设置（仅当不存在时）
    _ensure_default_settings(conn)

    # Spec 33 卡 159：来源/稿件类别稳定 ID 表 + 幂等播种。
    # 卡 168 起 `source_list`/`commission_type_list` 不再是默认键，只可能以「未迁移库
    # 的用户原序」形式存在，seed 读得到就用、读不到就用 choice_items.sort_order。
    # 失败仅记日志不阻断启动（seed 是增量能力，老路径有 settings 兜底）。
    try:
        seed_choice_items(conn)
        conn.commit()
    except Exception as e:
        logger.error('choice_items 播种失败（不阻断启动，读路径回退 settings）：%s', e)

    # 迁移：为旧数据库补齐后续新增列（新建库已在 CREATE TABLE 中定义，此处幂等）
    for col in ('custom_color', 'platform_url', 'page_deadline', 'image_url', 'image_path'):
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")
        except Exception:
            pass  # 列已存在
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN has_image INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # 列已存在
    # P19-F4 幽灵收款状态归一：历史默认值「待收款」及数字/空串污染 → 锁定值。幂等。
    # 目标值随枚举迁移态切换：未迁移写中文旧标签（留给迁移引擎转码），已迁移写 code。
    # 硬约束：迁移前绝不能写 code——否则旧库被提前改成半 code 状态，迁移闸门 M0 会把
    # 这些 code 误判成「用户自定义值与系统 code 重名」而永久中止迁移（卡 167 实测）。
    _ghost_paid = 'unpaid' if _enum_codes_migrated(conn) else '未收款'
    try:
        conn.execute(
            "UPDATE orders SET payment_status=? WHERE payment_status IN ('待收款','0','') "
            "OR payment_status = 0 OR payment_status IS NULL", (_ghost_paid,))
    except Exception:
        pass
    # Spec33 M19 常态守卫（非一次性）：把命中冻结表已知旧值的行归一到 code。
    # 只认 LEGACY_LABEL_TO_CODE 已知旧值，绝不猜测；天然幂等（code 值不命中 WHERE）。
    # 同理只在 code 空间（user_version>=33）才允许跑：未迁移的老库由迁移引擎 M1-M5 负责转码。
    if _enum_codes_migrated(conn):
        try:
            _m19_normalize_enum_columns(conn)
        except Exception as e:
            logger.error('M19 枚举列归一守卫失败: %s', e)
    # P15a 迁移：完成归档时间与逾期标记
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN completed_at TEXT")
    except Exception:
        pass  # 列已存在
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN is_overdue INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # 列已存在
    # P19-F1 历史回填：已归档但缺完成时间的旧记录，scheduled_end 优先、updated_at 兜底；
    # 并同步回填 is_overdue（完成日晚于排期截止日 → 1）。幂等：仅处理 completed_at IS NULL / is_overdue=0 的行。
    try:
        conn.execute("""
            UPDATE orders
            SET completed_at = COALESCE(NULLIF(scheduled_end, ''), substr(updated_at, 1, 10))
            WHERE is_archived = 1 AND completed_at IS NULL
        """)
        conn.execute("""
            UPDATE orders
            SET is_overdue = 1
            WHERE is_archived = 1 AND is_overdue = 0
              AND scheduled_end IS NOT NULL AND scheduled_end != ''
              AND substr(completed_at, 1, 10) > scheduled_end
        """)
    except Exception:
        pass
    for col in ('tags', 'former_names'):
        try:
            conn.execute(f"ALTER TABLE customers ADD COLUMN {col} TEXT")
        except Exception:
            pass

    # 2026-08-12 价目表 UX 改造：price_max 可选列（价格区间）+ 旧单例图列回填多图表。幂等。
    try:
        conn.execute("ALTER TABLE pricelist_items ADD COLUMN price_max REAL")
    except Exception:
        pass  # 列已存在
    try:
        conn.execute("""
            INSERT INTO pricelist_images (item_id, image_path, sort_order)
            SELECT p.id, p.example_image_path, 0
            FROM pricelist_items p
            WHERE p.example_image_path IS NOT NULL AND p.example_image_path != ''
              AND NOT EXISTS (SELECT 1 FROM pricelist_images pi WHERE pi.item_id = p.id)
        """)
    except Exception:
        pass

    # P15d 回填：已有单图（has_image=1 且有 image_url）但 order_images 尚无记录的订单，
    # 将其封面图作为首图导入多图表（幂等：仅当该订单在 order_images 中无记录时插入）
    try:
        conn.execute("""
            INSERT INTO order_images (order_id, image_url, image_path, sort_order)
            SELECT o.id, o.image_url, o.image_path, 0
            FROM orders o
            WHERE o.has_image = 1 AND o.image_url IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM order_images oi WHERE oi.order_id = o.id)
        """)
    except Exception:
        pass

    # P19-F8 复购派生视图化：读取路径改查询时计算（_apply_repeat_for_rows），写入路径停更；
    # 两列暂留不删（spec 11 §7，两个版本后评估删除），此处按新口径（同客户他单、排除退单
    # 终态、排除本单）幂等回填，防旧版本回滚读到腐烂值。
    try:
        _refund = get_refund_stage()
        conn.execute("""
            UPDATE orders SET
              repeat_count = (SELECT COUNT(*) FROM orders o2
                              WHERE o2.customer_id = orders.customer_id
                                AND o2.id != orders.id AND o2.current_stage != ?),
              is_repeat = CASE WHEN (SELECT COUNT(*) FROM orders o2
                              WHERE o2.customer_id = orders.customer_id
                                AND o2.id != orders.id AND o2.current_stage != ?) > 0
                          THEN 1 ELSE 0 END
        """, (_refund, _refund))
    except Exception:
        pass

    # P19-F9 费率快照：新增 orders.platform_fee_pct 并按当前来源费率回填。
    # 口径：平台来源 → 设置键 default_fee_<source>（缺省 5.0，与订单表单默认值一致）；
    #       直接来源/无来源 → 0（无手续费语义）。幂等：仅处理 platform_fee_pct IS NULL 的行
    #       （列可 NULL，回填一次性覆盖全库；对账 SQL 见 spec 11 §6）。
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN platform_fee_pct REAL")
    except Exception:
        pass  # 列已存在
    try:
        _fee_kv = {
            r[0]: r[1] for r in conn.execute(
                "SELECT key, value FROM settings WHERE key LIKE 'default_fee_%' OR key = 'platform_sources'"
            ).fetchall()
        }
        _plats = {x.strip() for x in (_fee_kv.get('platform_sources') or '米画师,B站工坊,画加').split(',') if x.strip()}
        # 顺序：先按平台来源费率回填 NULL 行，再把剩余 NULL（直接来源/无来源）兜底置 0。
        for _src in _plats:
            try:
                _pct = float(_fee_kv.get(f'default_fee_{_src}', '5') or 5)
            except (TypeError, ValueError):
                _pct = 5.0
            conn.execute(
                "UPDATE orders SET platform_fee_pct = ? WHERE platform_fee_pct IS NULL AND source = ?",
                (_pct, _src)
            )
        conn.execute("UPDATE orders SET platform_fee_pct = 0 WHERE platform_fee_pct IS NULL")
    except Exception:
        pass

    # P20b 时薪迁移：预估/实际工时 + 单订单排除标记（新建库已在 CREATE TABLE 中定义，此处幂等）
    for col in ('estimated_hours REAL', 'work_hours REAL'):
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col}")
        except Exception:
            pass  # 列已存在
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN exclude_hourly INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # 列已存在

    # Spec12 阶段流程快照：orders.stage_flow TEXT（本单阶段流程 JSON，详见 get_stage_flows）
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN stage_flow TEXT")
    except Exception:
        pass  # 列已存在

    # Spec19 VIP 折扣：customers 加 is_vip/discount_pct；orders 加 discount_pct/discounted_income。
    # 口径：discount_pct = 折后应收百分比（88 = 88 折），NULL = 不打折（D1/D2）；
    # discounted_income = 折后金额落库（无折扣 = income），保证折/费可审计（D5）。
    # 回填：存量订单折后 = 原价（一次性覆盖全库 NULL；幂等仅处理 NULL 行）。
    for col in ('is_vip INTEGER NOT NULL DEFAULT 0', 'discount_pct REAL'):
        try:
            conn.execute(f"ALTER TABLE customers ADD COLUMN {col}")
        except Exception:
            pass  # 列已存在
    for col in ('discount_pct REAL', 'discounted_income REAL'):
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col}")
        except Exception:
            pass  # 列已存在
    try:
        conn.execute("UPDATE orders SET discounted_income = income WHERE discounted_income IS NULL")
    except Exception:
        pass

    # Spec26 分期收款：orders 加收款模式列（simple=整单默认 / installment=分期流水）。
    # 老订单全部 simple，行为与统计逐位不变（spec §3.2 语义冻结；新建库建表时已含，老库走此迁移）。
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_mode TEXT NOT NULL DEFAULT 'simple'")
    except Exception:
        pass  # 列已存在

    conn.commit()
    conn.close()

    # ═══ Spec 33 卡 165：幂等自动迁移引擎（M0-M18）═══
    # 插入位置硬约束：_ensure_default_settings + 所有 ALTER TABLE 之后、累计消费重算之前
    _migration_failed = False
    try:
        _chk = sqlite3.connect(DB_PATH)
        _need_mig = not _enum_codes_migrated(_chk)
        _chk.close()
    except Exception:
        _need_mig = False
    if _need_mig:
        import shutil as _shutil
        _bak = DB_PATH.replace('orders.db', 'orders.pre-v1.5.0.db')
        try:
            # D13：WAL checkpoint → rename 原文件永久保留
            _c = sqlite3.connect(DB_PATH)
            _c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            _c.close()
            if not os.path.exists(_bak):
                _shutil.move(DB_PATH, _bak)
                logger.info('enum-mig D13 原文件 rename 保留: %s', ascii(_bak))
            if not os.path.exists(DB_PATH):
                # 工作副本缺失（首次迁移或上次中断）才从备份取一致快照（含 WAL 内容）。
                # 硬约束：绝不能用备份覆盖已存在的工作副本——否则两次启动之间录入的数据
                # 会被静默丢弃（卡 167 实测：72 单被回退成 71 单）。
                _src = sqlite3.connect(f'file:{_bak}?mode=ro', uri=True)
                _dst_new = sqlite3.connect(DB_PATH)
                _src.backup(_dst_new)
                _src.close()
                _dst_new.close()
            # 在工作副本上跑迁移（单事务）
            _dst = sqlite3.connect(DB_PATH)
            _dst.row_factory = sqlite3.Row
            _dst.execute('PRAGMA foreign_keys=ON')
            try:
                _migrate_enum_codes(_dst)
                _dst.commit()
                logger.info('enum-mig 迁移成功，user_version=%d', ENUM_CODE_MIGRATION_VERSION)
            except Exception as _me:
                _dst.rollback()
                _dst.close()
                # D12 降级：放回原文件，以兼容模式正常启动
                logger.error('enum-mig 迁移失败，降级兼容模式: %s', _me, exc_info=True)
                try:
                    os.remove(DB_PATH)
                except OSError:
                    pass
                _shutil.copy2(_bak, DB_PATH)
                _migration_failed = True
                # 落堆栈到日志文件
                try:
                    _logdir = os.path.join(os.path.dirname(DB_PATH), 'logs')
                    os.makedirs(_logdir, exist_ok=True)
                    import traceback
                    with open(os.path.join(_logdir, 'enum-migration-error.log'), 'a', encoding='utf-8') as _lf:
                        _lf.write(f'\n=== {datetime.now().isoformat()} ===\n')
                        traceback.print_exc(file=_lf)
                except Exception:
                    pass
            else:
                _dst.close()
        except Exception as _be:
            logger.error('enum-mig 备份/迁移异常，降级兼容模式: %s', _be, exc_info=True)
            _migration_failed = True
            # 确保 DB_PATH 存在（可能被 rename 走了）
            if not os.path.exists(DB_PATH) and os.path.exists(_bak):
                try:
                    _shutil.copy2(_bak, DB_PATH)
                except Exception:
                    pass
    # 迁移失败标记供设置页提示条读取（D12）
    if _migration_failed:
        try:
            _fc = get_db()
            _fc.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('enum_migration_failed', '1')")
            _fc.commit()
            _fc.close()
        except Exception:
            pass
    else:
        try:
            _fc = get_db()
            _fc.execute("DELETE FROM settings WHERE key='enum_migration_failed'")
            _fc.commit()
            _fc.close()
        except Exception:
            pass

    # 迁移：累计消费口径变更（仅统计已归档+已完成+已结算订单），一次性重算全部客户
    try:
        _mig_conn = get_db()
        _cids = [r[0] for r in _mig_conn.execute("SELECT id FROM customers").fetchall()]
        for _cid in _cids:
            _done_vals = _enum_sql_values('stage', {get_done_stage()})
            _paid_vals = _enum_sql_values('payment', get_paid_statuses())
            _row = _mig_conn.execute(
                f"""SELECT COUNT(*) as cnt, COALESCE(SUM(actual_received), 0) as total
                   FROM orders
                   WHERE customer_id = ?
                     AND is_archived = 1
                     AND current_stage IN ({','.join('?' * len(_done_vals))})
                     AND payment_status IN ({','.join('?' * len(_paid_vals))})""",
                [_cid] + _done_vals + _paid_vals
            ).fetchone()
            _mig_conn.execute(
                "UPDATE customers SET total_spent = ?, purchase_count = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (_row['total'], _row['cnt'], _cid)
            )
        _mig_conn.commit()
        _mig_conn.close()
    except Exception:
        pass
    # P19-F10：迁移可能改写 orders 数据（auto-discover 来源），启动期统一失效 choices 缓存
    _invalidate_choices_cache()
    # Spec12：stage_flows 缓存也随启动失效（_ensure_default_settings 可能已回填默认流程）
    _invalidate_stage_flows_cache()
    # P20-F14：图片引用一致性自愈（用户绕过应用手动删 uploads 文件后，
    # order_images 记录与 orders 封面三列残留 → 画廊失效链接；启动时自动对齐）
    repair_image_consistency()
    # Spec26：清理孤儿收款流水（旧版 exe 删单遗留 / 手动改库防御）
    _cleanup_orphan_payments()
    # 卡 169 D-3：升级后**首启**即清中文后缀配色键。位置必须在迁移引擎之后：引擎在
    # 本函数后段才把 user_version 置为 33，且生成型 code（src_NNN/cmt_NNN）的标签
    # 映射由 seed_choice_items 写入 choice_items —— 两者都就绪判据才准（实测放在
    # 播种点会被 `_enum_codes_migrated` 拦下，升级后首启漏清、第二次启动才生效）。
    _cc = get_db()
    try:
        if _enum_codes_migrated(_cc):
            _cleanup_legacy_palette_keys(_cc)
            _cc.commit()
    except Exception as e:
        logger.error('卡169 D-3 配色键清理失败（不阻断启动）: %s', e)
    finally:
        _cc.close()


def _cleanup_orphan_payments() -> int:
    """Spec 26：清理无有效订单引用的收款流水（孤儿记录），返回清理条数。

    来源场景：旧版 exe（不知 order_payments 表）删单后遗留、或用户绕过应用
    手动改库。启动例行清理；失败记录日志不阻断启动（spec §7 风险登记册）。
    """
    try:
        with transaction() as conn:
            cur = conn.execute(
                "DELETE FROM order_payments WHERE NOT EXISTS "
                "(SELECT 1 FROM orders o WHERE o.id = order_payments.order_id)"
            )
            removed = cur.rowcount or 0
        if removed:
            logger.info('Spec26 孤儿收款记录清理：%d 条', removed)
        return removed
    except Exception as e:
        logger.error('孤儿收款记录清理失败: %s', e)
        return 0


CHOICE_REGISTRY = {
    'stage': {
        'editable': False,  # 系统级，不暴露给设置页
        'default': 'pending,sketch,lineart,detail,finish,completed,cancelled',
        'field': 'current_stage',  # orders 表中的字段名
    },
    'ddl': {
        'editable': False,
        'default': 'normal,due,overdue,done,refund',
        'field': 'ddl_status',
    },
    'payment': {
        'editable': False,  # 系统锁定，不可自定义
        'default': 'deposit_paid,unpaid,settled,debt,waived',
        'field': 'payment_status',
    },
    'source': {
        'editable': True,
        'default': '米画师,B站工坊,画加,微信,QQ,其他',
        'field': 'source',
    },
    'commission_type': {
        'editable': True,
        'default': '单人半身,色稿大头,双人横插,立绘,场景插画,Q版,服设,厚涂头像',
        'field': 'commission_type',
    },
}

# 需要特殊处理的"已收款"状态（用于收入统计 SQL）
# Spec33 卡 160：值空间换 code（D7：两常量分置保留原注释——code 空间下仍拆分）
PAID_STATUSES = {'settled'}

# P19-F1 归档判定的收款状态集合：与收入统计拆分——
# 免收单「要归档但不计收入」，故 PAID_STATUSES 保持收入统计专用不动，归档判定用本集合。
ARCHIVE_PAID_STATUSES = {'settled', 'waived'}

# ═══════════════════════════════════════════════════════════
# Spec 33 枚举编码中枢（卡 158 · 纯新增，零行为变化）
#
# 三层结构，职责严格分离，**不得互相顶替**：
#   CODE_TO_LABEL        —— code → 唯一规范显示名（「反查输出」用）
#   LEGACY_LABEL_TO_CODE —— 任意已知写法 → code（「识别输入」用，一 code 可多别名）
#   RESERVED_ENUM_CODES  —— 保留字，分宽/严两套判定（见两个判定函数注释）
#
# ⚠️ 卡 157 预演实发过两个坑，本节设计正是为规避它们：
#   1. 别名反查歧义：`🔴逾期` 与 `逾期` 同指 overdue，若拿字典翻转当反查源，
#      结果取决于哪个键后写入 —— 会把一次正确迁移误判成数据漂移。
#      故 CODE_TO_LABEL 单独定义，一 code 只有一个显示名，且该值一律等于
#      **当前线上实际显示串**（`🔴逾期` 表情符在前 / `已完成✅` 在后，
#      位置不同是历史事实，照抄才能证明 D2「展示层零退化」）。
#   2. 保留字判据宽严不可混用：M0 迁移预审计用严判据（误拦 = 整个升级被阻断），
#      新建侧拒绝用宽判据（误拒 = 用户换个名）。
#
# 本卡不动 CHOICE_REGISTRY 的 `default` 逗号串（值空间换码属卡 160），
# 因此全项目行为与本卡之前完全一致。
# ═══════════════════════════════════════════════════════════

# kind → orders 列名（与 CHOICE_REGISTRY 的键同源）
ENUM_FIELD = {
    'stage': 'current_stage',
    'ddl': 'ddl_status',
    'payment': 'payment_status',
    'source': 'source',
    'commission_type': 'commission_type',
}
COLUMN_TO_ENUM_KIND = {v: k for k, v in ENUM_FIELD.items()}

# code → 规范显示名（唯一反查源；字典顺序即下拉展示顺序）
CODE_TO_LABEL = {
    'stage': {
        'pending': '待开始', 'sketch': '色稿', 'lineart': '线稿',
        'detail': '细化', 'finish': '收尾', 'completed': '完成',
        'cancelled': '退单',
    },
    'ddl': {
        'normal': '正常', 'due': '即将到期', 'overdue': '\U0001f534逾期',
        'done': '已完成✅', 'refund': '已退单',
    },
    'payment': {
        'deposit_paid': '已收定金', 'unpaid': '未收款', 'settled': '已结算',
        'debt': '欠款', 'waived': '免收',
    },
    'source': {
        'mihuashi': '米画师', 'bili': 'B站工坊', 'huajia': '画加',
        'wechat': '微信', 'qq': 'QQ', 'other': '其他',
    },
    'commission_type': {
        'cmt_001': '单人半身', 'cmt_002': '色稿大头', 'cmt_003': '双人横插',
        'cmt_004': '立绘', 'cmt_005': '场景插画', 'cmt_006': 'Q版',
        'cmt_007': '服设', 'cmt_008': '厚涂头像',
    },
}

# 任意已知写法 → code（「识别输入」用；**永久冻结，只增不改不删**，
# 迁移与兼容读都依赖它。一个 code 可有多个别名，但别名绝不能用作反查。）
LEGACY_LABEL_TO_CODE = {
    'stage': {
        '待开始': 'pending', '色稿': 'sketch', '线稿': 'lineart',
        '细化': 'detail', '收尾': 'finish', '完成': 'completed', '退单': 'cancelled',
    },
    'ddl': {
        '正常': 'normal', '即将到期': 'due',
        '\U0001f534逾期': 'overdue', '逾期': 'overdue',
        '已完成✅': 'done', '已完成': 'done',
        '已退单': 'refund',
    },
    'payment': {
        '已收定金': 'deposit_paid', '未收款': 'unpaid', '已结算': 'settled',
        '欠款': 'debt', '免收': 'waived',
        '待收款': 'unpaid',   # 收编 P19-F4 幽灵值（存量 orders 列默认值仍是它）
    },
    'source': {
        '米画师': 'mihuashi', 'B站工坊': 'bili', '画加': 'huajia',
        '微信': 'wechat', 'QQ': 'qq', '其他': 'other',
    },
    'commission_type': {
        '单人半身': 'cmt_001', '色稿大头': 'cmt_002', '双人横插': 'cmt_003',
        '立绘': 'cmt_004', '场景插画': 'cmt_005', 'Q版': 'cmt_006',
        '服设': 'cmt_007', '厚涂头像': 'cmt_008',
    },
}

# 系统分配 ID 形态 ^(src|cmt)_\d{3,}$：用户自建值不得长这样
_ID_CODE_RE = re.compile(r'^(src|cmt)_\d{3,}$')

# **宽**判据保留字：全 kind code ∪ 全部已知旧标签。仅供新建侧拒绝使用。
RESERVED_ENUM_CODES = frozenset(
    [c for m in CODE_TO_LABEL.values() for c in m]
    + [lab for m in LEGACY_LABEL_TO_CODE.values() for lab in m]
)


def column_codes(kind: str) -> frozenset:
    """本列合法 code 集合（严判据用）。未知 kind → 空集。"""
    return frozenset(CODE_TO_LABEL.get(kind, {}))


def is_reserved_token(value) -> bool:
    """**宽**判定：值是否占用系统保留字（全 kind code ∪ 旧标签 ∪ ID 形态）。

    用于新建侧拒绝自定义阶段名 / 来源名 / 类别名（spec D17 第 2 层、D18）。
    """
    if not isinstance(value, str) or not value:
        return False
    return value in RESERVED_ENUM_CODES or bool(_ID_CODE_RE.match(value))


def is_ambiguous_in_column(kind: str, value) -> bool:
    """**严**判定：值与本列某个系统 code 同名，但并非本列可转换的旧标签。

    此时转码后无法区分它是用户自定义值还是系统枚举值 —— 例如客户把阶段命名成
    `completed`，会被 is_terminal_stage 判真，进而**静默自动归档并计入收入**。
    迁移预审计 M0 只用本函数；宽判据会把所有未迁移老库都拦死（卡 157 实发）。
    """
    if not isinstance(value, str) or not value:
        return False
    if value not in CODE_TO_LABEL.get(kind, {}):
        return False
    return value not in LEGACY_LABEL_TO_CODE.get(kind, {})


def enum_items(kind: str) -> list:
    """系统枚举项清单 [{'code','label'}, ...]，顺序即展示顺序。"""
    return [{'code': c, 'label': l} for c, l in CODE_TO_LABEL.get(kind, {}).items()]


def enum_label(kind: str, value, with_emoji: bool = True) -> str:
    """code → 显示名。**未知值原样返回**（直通自定义阶段名 / 历史脏值 / 空值）。

    直通是关键兼容性质：迁移前后、自定义值、恢复的老备份，调用方永远拿得到
    可显示字符串，既不会看到裸 code 也不会抛错。
    with_emoji=False 仅对 ddl 有意义（剥掉史实位置的表情符）。
    """
    if value is None:
        return ''
    table = CODE_TO_LABEL.get(kind, {})
    label = table.get(value)
    if label is None:
        code = LEGACY_LABEL_TO_CODE.get(kind, {}).get(value)
        label = table[code] if code else str(value)
    if not with_emoji and kind == 'ddl':
        label = re.sub(r'[^\w一-鿿]', '', label)
    return label


def enum_code(kind: str, value):
    """任意已知写法 → code；认不出来返回 None（自定义值无 code）。"""
    if not isinstance(value, str) or not value:
        return None
    codes = CODE_TO_LABEL.get(kind, {})
    if value in codes:                 # 已是 code
        return value
    return LEGACY_LABEL_TO_CODE.get(kind, {}).get(value)


def enum_code_or_raw(kind: str, value):
    """兼容读：能识别成 code 就换，否则**原样返回**。

    ⚠️ 判「是不是 code」靠查表，绝不靠 `[a-z0-9_]` 字符集猜 —— 实测库里有
    自定义阶段名 `111`、来源 `QQ`、类别字符串 `None`，全是合法 ASCII 形态。
    """
    code = enum_code(kind, value)
    return code if code is not None else value


def _enum_sql_values(kind: str, codes) -> list:
    """SQL IN 子句兼容值：code ∪ 映射到这些 code 的旧中文标签。
    使 recalc_customer_stats 等裸 SQL 在未迁移/已迁移库上同样工作。"""
    vals = set(codes)
    for lab, code in LEGACY_LABEL_TO_CODE.get(kind, {}).items():
        if code in vals:
            vals.add(lab)
    return list(vals)


def is_overdue_ddl(value) -> bool:
    """是否逾期（对未迁移的 `🔴逾期` 与已迁移的 `overdue` 同为真）。"""
    return enum_code_or_raw('ddl', value) == 'overdue'


def is_done_stage(value) -> bool:
    """是否完成类终态（兼容 `完成` 与 `completed`）。"""
    return enum_code_or_raw('stage', value) == 'completed'


def is_cancelled_stage(value) -> bool:
    """是否退单类终态（兼容 `退单` 与 `cancelled`）。"""
    return enum_code_or_raw('stage', value) == 'cancelled'


def column_enum_label(column: str, value, with_emoji: bool = True) -> str:
    """按 orders 列名取显示名（模板过滤器 `|stage_label` 的统一后端）。"""
    return enum_label(COLUMN_TO_ENUM_KIND.get(column, column), value, with_emoji)


# ═══════════════════════════════════════════════════════════
# Spec 33 来源/稿件类别稳定 ID 数据层（卡 159）
#
# 动机（spec D8）：痛点是「改一次名要搬 default_fee_ / cal_source_ / platform_fee_
# 三类中文配置键」。只有把每来源的附属属性（is_platform / default_fee_pct）
# 变成**列**才根治 —— settings-JSON 路线只是把中文键换成中文值，改名照样要搬运。
#
# ⚠️ 本卡「零行为变化」的边界：四个读函数改走表，但**入参与返回一律仍是 label
#    （中文显示名）**，值空间换码发生在卡 160/164。故本卡可独立 revert。
#
# ⚠️ sort_order 语义：一律按**用户列表原序**排（顺序是用户资产，spec M13）。
#    内置项只参与「先占码」以保证动态 ID 不抢占内置号段（卡 157 实发缺陷），
#    **不参与定序**。若把内置项排到最前，升级后下拉顺序会被重排 = 行为变化。
# ═══════════════════════════════════════════════════════════

_CHOICE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS choice_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    code            TEXT NOT NULL,
    label           TEXT NOT NULL,
    is_platform     INTEGER NOT NULL DEFAULT 0,
    default_fee_pct REAL    NOT NULL DEFAULT 5.0,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    is_builtin      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (kind, code)
)"""

_CHOICE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_choice_items_kind
    ON choice_items(kind, sort_order, id)"""

# 刻意**不加** UNIQUE(kind, label)：改名「A↔B 对调」会在中间态撞唯一索引而死锁；
# 标签唯一性放到服务层（add/rename 各自查重并返回用户可读错误）。

# 卡 168：迁移前本键存的是中文标签序，是「用户排好的顺序」的唯一来源，
# M13 删键后顺序只认 choice_items.sort_order（seed 不再覆写）。
_CHOICE_LIST_KEY = {'source': 'source_list', 'commission_type': 'commission_type_list'}
_CHOICE_ID_PREFIX = {'source': 'src', 'commission_type': 'cmt'}

# 卡 168：platform_sources 键随 T168.4 一并删除，全新库的平台标记靠本常量播种
# （取自卡 168 之前 `_ensure_default_settings` 里该键的老默认值
# `米画师,B站工坊,画加` —— 少了 mihuashi 会让全新库的平台单手续费静默归零）；
# 既有库靠 choice_items.is_platform 列自身承载。
BUILTIN_PLATFORM_CODES = frozenset({'mihuashi', 'bili', 'huajia'})


def ensure_choice_items_table(conn=None) -> None:
    """建表 + 建索引（幂等）。conn 传入时在既有事务内执行，不自开连接。"""
    own = conn is None
    if own:
        conn = get_db()
    conn.execute(_CHOICE_TABLE_DDL)
    conn.execute(_CHOICE_INDEX_DDL)
    if own:
        conn.commit()
        conn.close()


def _next_choice_code(conn, kind: str) -> str:
    """分配 `src_%03d`/`cmt_%03d`：跳过内置号段，且跳过已存在行。

    ⚠️ 卡 157 预演实发的致命缺陷：原实现只取 MAX+1，当用户自建项先于内置项落库时
    会把内置号段抢走（实测 `半身` 拿走 `cmt_005` = 场景插画的位置），随后内置项
    `INSERT OR IGNORE` 静默跳过 → 两个类别共用一个 code，
    cal_commission_* 颜色键互相覆盖。**客户会在升级后发现类别颜色莫名改变。**
    """
    taken = set(CODE_TO_LABEL.get(kind, {}))
    row = conn.execute(
        'SELECT MAX(CAST(substr(code, 5) AS INTEGER)) FROM choice_items WHERE kind=?',
        (kind,)).fetchone()
    n = (row[0] or 0) + 1
    prefix = _CHOICE_ID_PREFIX[kind]
    while True:
        cand = f'{prefix}_{n:03d}'
        if cand in taken or conn.execute(
                'SELECT 1 FROM choice_items WHERE kind=? AND code=?',
                (kind, cand)).fetchone():
            n += 1
            continue
        return cand


def _upsert_choice_item(conn, kind: str, code: str, label: str,
                        sort_order: int, is_builtin: int,
                        is_platform: int = 0) -> None:
    """仅在 (kind,code) 不存在时按传入值插入；已存在则什么都不改。

    卡 168：旧实现用 ON CONFLICT 覆写 label/sort_order/is_builtin，等于每次建表
    都把用户排好的顺序、改过的名称、勾好的平台标记倒推回注册表默认值（实测
    来源顺序被重排、平台标记反复回滚）。用户侧属性一律归用户，启动只做补行。
    同名 label 冲突（同 code 已属别的 label）仍炸出 —— 改名走 `rename_choice_item`。
    """
    row = conn.execute('SELECT label FROM choice_items WHERE kind=? AND code=?',
                       (kind, code)).fetchone()
    if row is not None:
        if row[0] != label:
            # 不再静默覆盖 —— 颜色键与费率会随 code 归属走，覆盖即数据丢失
            raise RuntimeError(
                f'choice_items 冲突：{kind} 的 {code} 已属 {row[0]!r}，拒绝改判给 {label!r}')
        return
    conn.execute(
        'INSERT INTO choice_items (kind, code, label, sort_order, is_builtin, is_platform) '
        'VALUES (?,?,?,?,?,?)',
        (kind, code, label, sort_order, is_builtin, 1 if is_platform else 0))


def _insert_choice_item_if_missing(conn, kind: str, code: str, label: str,
                                  sort_order: int, is_builtin: int,
                                  is_platform: int = 0) -> bool:
    """仅在 (kind,code) 不存在时插入；既有行的 label / sort_order / 标记一律不动。

    卡 168：seed 专用。既有行代表用户确认过的顺序与显示名（可能已改名），
    每次启动都不该被注册表默认值覆写，也不能因改名而抛冲突异常。
    与 `_upsert_choice_item` 的差别正是「不改名也不炸」。
    """
    row = conn.execute('SELECT 1 FROM choice_items WHERE kind=? AND code=?',
                       (kind, code)).fetchone()
    if row is not None:
        return False
    conn.execute(
        'INSERT INTO choice_items (kind, code, label, sort_order, is_builtin, is_platform) '
        'VALUES (?,?,?,?,?,?)',
        (kind, code, label, sort_order, is_builtin, 1 if is_platform else 0))
    return True


def apply_legacy_source_settings(conn) -> int:
    """把 settings 里**仍残存**的 `platform_sources` / `default_fee_<label>` 落到
    choice_items 的 is_platform / default_fee_pct 列。返回应用的键数。

    ⚠️ 「键存在才应用」是本函数的幂等基石，也是它能同时服务卡 159 与卡 165 的原因：
      - 卡 159-164 阶段：settings 键还在 → 每次 seed 都按 settings 覆盖列（结果恒定）
      - 卡 165 之后：M11/M12 已删这些键 → 本函数无操作，**不会把用户后来在设置页
        改过的列值倒退回历史 settings 值**

    卡 159 实测教训：漏掉这一步会让 is_platform 全为 0，`get_platform_sources()`
    返回空集 → 所有平台单手续费静默归零。

    ⚠️ 只搬 is_platform 门内的东西不等于放过非平台来源的 default_fee_ 惰性值 ——
    实测库里 `default_fee_微信` 之类确实存在，全靠 `get_default_fee_for_source`
    的先查平台门才没生效。这里照搬进列（值保留），门仍在读函数上，语义一字不变。
    """
    applied = 0
    prow = conn.execute("SELECT value FROM settings WHERE key='platform_sources'").fetchone()
    if prow:
        plats = {x.strip() for x in (prow[0] or '').split(',') if x.strip()}
        for row in conn.execute('SELECT code, label FROM choice_items '
                                "WHERE kind='source'").fetchall():
            want = 1 if row['label'] in plats else 0
            cur = conn.execute('SELECT is_platform FROM choice_items WHERE code=?',
                               (row['code'],)).fetchone()
            if cur and cur[0] != want:
                conn.execute('UPDATE choice_items SET is_platform=? WHERE code=?',
                             (want, row['code']))
                applied += 1
        # settings 里有平台名但表内无对应行（新导入的自定义来源）→ 兜底补标
        for lbl in plats:
            fixed = LEGACY_LABEL_TO_CODE['source'].get(lbl)
            if fixed:
                conn.execute('UPDATE choice_items SET is_platform=1 '
                             "WHERE kind='source' AND code=?", (fixed,))
    for srow in conn.execute("SELECT key, value FROM settings "
                             "WHERE key LIKE 'default_fee_%'").fetchall():
        lbl = srow[0][len('default_fee_'):]
        try:
            fee = float(srow[1] or 5)
        except (TypeError, ValueError):
            continue
        crow = conn.execute('SELECT code, default_fee_pct FROM choice_items '
                            "WHERE kind='source' AND label=?", (lbl,)).fetchone()
        if crow and float(crow['default_fee_pct']) != fee:
            conn.execute('UPDATE choice_items SET default_fee_pct=? WHERE code=?',
                         (fee, crow['code']))
            applied += 1
    if applied:
        logger.info('choice_items 应用 settings 残存平台/费率 %d 项', applied)
    return applied


def dedup_choice_items(conn) -> int:
    """同 (kind,label) 多行时保留最小 id 那行，删除其余，并迁移已引用它的数据。

    本卡首次实现的缺陷会在开发库留下重复行，这里做一次自愈清理；
    因 orders.* 此刻仍是中文 label 空间（值空间换码在卡 160/164），
    删多余 code 不影响任何订单数据。
    """
    removed = 0
    for kind in ('source', 'commission_type'):
        groups = conn.execute(
            'SELECT label, COUNT(*) n, MIN(id) keep_id FROM choice_items '
            'WHERE kind=? GROUP BY label HAVING n > 1', (kind,)).fetchall()
        for g in groups:
            dupes = conn.execute(
                'SELECT code FROM choice_items WHERE kind=? AND label=? AND id!=?',
                (kind, g['label'], g['keep_id'])).fetchall()
            for d in dupes:
                prefix = {'source': 'cal_source_',
                          'commission_type': 'cal_commission_'}[kind]
                conn.execute('DELETE FROM settings WHERE key=?', (prefix + d[0],))
                cur = conn.execute('DELETE FROM choice_items WHERE kind=? AND code=?',
                                   (kind, d[0]))
                removed += cur.rowcount
    if removed:
        logger.info('choice_items 去重：删除 %d 个重复行', removed)
    return removed


def purge_code_named_items(conn) -> int:
    """清理「label 恰等于本 kind 另一行 code」的伪行，返回删除数。

    卡 165 迁移后 seed 把 orders 列里的英文码当 label 重新登记，每启动一次多一批
    （实测 source `src_002..src_007` / commission `cmt_017..cmt_024` 共 14 行，
    第二代还出现 `src_007` 的 label = `'src_001'` 这种链式污染）。

    ⚠️ 判据故意收窄到「label 命中已有 code」+「orders 无引用」两条同时成立：
    真实数据里确实存在名叫 `QQ`（11 单）/`None`（2 单）的类别与名叫 `行书` 的来源，
    它们只是「名字长得像代码或怪」，不是伪行 —— 逐值核对 `orders.pre-v1.5.0.db` 确认。
    """
    removed = 0
    for kind, column in (('source', 'source'), ('commission_type', 'commission_type')):
        # 先一次性取全，避免边删边判导致链式伪行（label 指向另一伪行 code）漏网
        rows = conn.execute(
            'SELECT c1.code FROM choice_items c1 WHERE c1.kind=? AND c1.code != c1.label '
            'AND EXISTS (SELECT 1 FROM choice_items c2 WHERE c2.kind=c1.kind '
            'AND c2.code=c1.label)', (kind,)).fetchall()
        prefix = 'cal_source_' if kind == 'source' else 'cal_commission_'
        for r in rows:
            code = r[0]
            ref = conn.execute(
                f'SELECT COUNT(*) FROM orders WHERE {column}=?', (code,)).fetchone()[0]
            if ref:
                logger.warning('伪行 %s/%s 被 %d 个订单引用，保留（防数据丢失）',
                               kind, code, ref)
                continue
            conn.execute('DELETE FROM settings WHERE key=?', (prefix + code,))
            conn.execute('DELETE FROM choice_items WHERE kind=? AND code=?', (kind, code))
            removed += 1
    if removed:
        logger.info('choice_items 清理伪行 %d 个（label 命中本 kind 已知 code）', removed)
    return removed


def seed_choice_items(conn) -> dict:
    """幂等播种两类可编辑枚举，返回 {kind: {label: code}}。

    卡 168 起只做两件事：**补缺失的行** + **补登记 orders 里仍未被认识的用户值**。
    既有行的 label / sort_order / is_platform 一律不碰（旧实现每次启动都按注册表
    默认值覆写，实测把用户排好的来源顺序重排、平台标记回滚，且把 orders 里的英文码
    当成新用户值反复登记，每启动一次多一批假选项）。

    本函数即卡 165 的 M13 复用体，迁移与常态播种共用一份实现（禁止两处各写）：
    迁移期 `settings[source_list]` 还带着用户原序，用它定序；迁移后键已删，
    顺序只认 `choice_items.sort_order`。
    """
    ensure_choice_items_table(conn)
    dedup_choice_items(conn)          # 先自愈历史重复行，再按 label 复用码
    purge_code_named_items(conn)      # 再清「码当名」的伪行（卡 168）
    out = {}
    for kind, column in (('source', 'source'), ('commission_type', 'commission_type')):
        builtin = list(CODE_TO_LABEL.get(kind, {}))          # 注册表内置序
        builtin_labels = [CODE_TO_LABEL[kind][c] for c in builtin]
        fixed = {label: code for code, label in CODE_TO_LABEL[kind].items()}

        # 迁移期用户原序（键已被 M13 删除后为空）
        srow = conn.execute('SELECT value FROM settings WHERE key=?',
                            (_CHOICE_LIST_KEY[kind],)).fetchone()
        listed = [x.strip() for x in (srow[0] or '').split(',') if x.strip()] if srow else []
        try:
            drows = conn.execute(
                f'SELECT DISTINCT {column} FROM orders '
                f"WHERE {column} IS NOT NULL AND {column} != ''").fetchall()
            db_vals = [r[0] for r in drows]
        except Exception:
            db_vals = []

        # 存量行：用户已确认的顺序与显示名，本函数绝不改写
        existing = {r['label']: r['code'] for r in conn.execute(
            'SELECT code, label FROM choice_items WHERE kind=?', (kind,)).fetchall()}
        existing_codes = set(existing.values())
        first_run = not existing              # 首次安装 / 迁移中：表还是空的
        next_sort = (conn.execute('SELECT COALESCE(MAX(sort_order), -1) FROM choice_items '
                                  'WHERE kind=?', (kind,)).fetchone()[0] or -1) + 1

        seq, seen = [], set()

        def _add(lbl):
            if lbl and lbl not in seen:
                seen.add(lbl)
                seq.append(lbl)

        for lbl in listed:                            # ① 迁移期用户原序优先
            _add(lbl)
        if first_run:
            # ② 只在空表时补内置项 —— 否则用户从设置页删掉的来源/类别会被启动复活
            for lbl in builtin_labels:
                _add(lbl)
        for v in db_vals:                             # ③ 库内值：认得的跳过，认不得的登记
            if not v or v in existing_codes or enum_code(kind, v) is not None:
                continue
            _add(v)

        mapping = {}
        for lbl in seq:
            code = fixed.get(lbl) or existing.get(lbl)
            if code is None:
                code = _next_choice_code(conn, kind)
            # 内置项也必须落行（全新库靠这里播种）；既有行不改 label/sort_order/标记
            is_plat = 1 if (kind == 'source' and lbl in
                            {CODE_TO_LABEL['source'].get(c, c)
                             for c in BUILTIN_PLATFORM_CODES}) else 0
            if _insert_choice_item_if_missing(conn, kind, code, lbl, next_sort,
                                              1 if lbl in fixed else 0, is_plat):
                next_sort += 1
            mapping[lbl] = code
        out[kind] = mapping
        logger.info('choice_items seed %s：%d 项（内置 %d）',
                    kind, len(seq), len(fixed))
    # 平台标记与费率必须紧随播种应用，否则 is_platform 全 0 → 手续费静默归零
    apply_legacy_source_settings(conn)
    return out


def get_choice_rows(kind: str) -> list:
    """按 sort_order 读全部行 [{'code','label','is_platform','default_fee_pct',...}]。"""
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT code, label, is_platform, default_fee_pct, sort_order, is_builtin '
            'FROM choice_items WHERE kind=? ORDER BY sort_order, id', (kind,)).fetchall()
    except Exception as e:                       # 表尚未建（老库首次导入期）
        logger.error('get_choice_rows(%s) 失败：%s', kind, e)
        rows = []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_user_choice_items(kind: str) -> list:
    """用户可编辑枚举（source/commission_type）的界面契约：[{'code','label',...}]。

    表为空时回退注册表内置序。界面一律消费本函数的 code+label 双值，
    不再拿裸字符串自己猜是码还是中文。
    """
    rows = get_choice_rows(kind)
    if rows:
        return rows
    return [{'code': c, 'label': l, 'is_platform': 0, 'default_fee_pct': 5.0,
             'sort_order': i, 'is_builtin': 1}
            for i, (c, l) in enumerate(CODE_TO_LABEL.get(kind, {}).items())]


def choice_codes(kind: str) -> list:
    """本 kind 全部 code（按 sort_order）。"""
    return [r['code'] for r in get_user_choice_items(kind)]


def choice_label(kind: str, code) -> str:
    """code → 显示名；查不到（老库未迁移值 / 空）原样返回。"""
    if not code:
        return ''
    for r in get_choice_rows(kind):
        if r['code'] == code:
            return r['label']
    return str(code)


def resolve_choice_code(kind: str, token):
    """入参归一：**code 优先，其次 label**；都认不出返回 None。

    让 URL 书签、旧前端缓存、手工 curl 提交的老中文值都能落到新值空间。
    """
    if not isinstance(token, str) or not token:
        return None
    rows = get_choice_rows(kind)
    for r in rows:
        if r['code'] == token:
            return token
    for r in rows:
        if r['label'] == token:
            return r['code']
    return None


def _ensure_choice_code_conn(conn, kind: str, token):
    """`ensure_choice_code` 的事务内版本：在**调用方连接**上查/建行，不自开事务。

    返回已登记的 code；无法登记（空/非串/保留字）时返回 None。
    """
    if not isinstance(token, str) or not token:
        return None
    code = enum_code(kind, token)
    if code is not None:
        return code
    hit = resolve_choice_code(kind, token)
    if hit is not None:
        return hit
    if is_reserved_token(token):
        return None
    ensure_choice_items_table(conn)
    row = conn.execute('SELECT code FROM choice_items WHERE kind=? AND label=? '
                       'ORDER BY id LIMIT 1', (kind, token)).fetchone()
    if row is not None:
        return row[0]
    code = _next_choice_code(conn, kind)
    srow = conn.execute('SELECT COALESCE(MAX(sort_order), -1) FROM choice_items '
                        'WHERE kind=?', (kind,)).fetchone()
    _upsert_choice_item(conn, kind, code, token, (srow[0] or 0) + 1, 0)
    return code


def ensure_choice_code(kind: str, token) -> str:
    """写入侧用户枚举归一：**认得就返回 code，认不出就登记新行再返回 code**。

    卡 164 只做了被动一半（`enum_code_or_raw` 不认就原样透传），于是用户新填的
    来源/类别以中文原样落进 orders 列，界面按 code 反查不到中文。本函数补上登记
    那一半 —— 使 orders 的用户枚举列**永远只有 code**。

    预留字 / 空值 / 非字符串按卡 164 口径原样返回，交给上层校验拒绝或忽略。
    """
    if not isinstance(token, str) or not token:
        return token
    code = enum_code(kind, token)
    if code is None:
        code = resolve_choice_code(kind, token)
    if code is not None:
        return code
    if is_reserved_token(token):
        return token
    with transaction() as conn:
        code = _ensure_choice_code_conn(conn, kind, token)
    _invalidate_choices_cache()
    return code or token


def add_choice_item(kind: str, label: str) -> dict:
    """新建可编辑项。label 去空白后非空且不与现有 label/code 冲突，否则 ValueError。"""
    label = (label or '').strip()
    if not label:
        raise ValueError('名称不能为空')
    if is_reserved_token(label):
        raise ValueError(f'「{label}」是系统保留名称，请换一个')
    with transaction() as conn:
        ensure_choice_items_table(conn)
        row = conn.execute('SELECT 1 FROM choice_items WHERE kind=? AND label=?',
                           (kind, label)).fetchone()
        if row:
            raise ValueError(f'「{label}」已存在')
        code = _next_choice_code(conn, kind)
        srow = conn.execute('SELECT COALESCE(MAX(sort_order), -1) FROM choice_items '
                            'WHERE kind=?', (kind,)).fetchone()
        _upsert_choice_item(conn, kind, code, label, (srow[0] or 0) + 1, 0)
    _invalidate_choices_cache()
    return {'code': code, 'label': label}


def rename_choice_item(kind: str, code: str, new_label: str) -> int:
    """**只改 label 列** —— 这就是 ID 方案的全部意义：改名零搬运。

    orders 列值、cal_* / default_fee 配置一律不动。返回受影响订单行数（恒为 0）。
    """
    new_label = (new_label or '').strip()
    if not new_label:
        raise ValueError('名称不能为空')
    with transaction() as conn:
        dup = conn.execute('SELECT 1 FROM choice_items WHERE kind=? AND label=? '
                           'AND code!=?', (kind, new_label, code)).fetchone()
        if dup:
            raise ValueError(f'「{new_label}」已存在')
        cur = conn.execute('UPDATE choice_items SET label=?, '
                           "updated_at=datetime('now','localtime') "
                           'WHERE kind=? AND code=?', (new_label, kind, code))
        if cur.rowcount == 0:
            raise ValueError(f'未找到 {kind}/{code}')
    _invalidate_choices_cache()
    return 0


def delete_choice_item(kind: str, code: str) -> None:
    """删除项及其 cal_<mode>_<code> 颜色键（费率/平台标记随行消失，无需搬运）。"""
    with transaction() as conn:
        conn.execute('DELETE FROM choice_items WHERE kind=? AND code=?', (kind, code))
        prefix = {'source': 'cal_source_', 'commission_type': 'cal_commission_'}[kind]
        conn.execute('DELETE FROM settings WHERE key=?', (prefix + code,))
    _invalidate_choices_cache()


_CAL_PREFIX_BY_KIND = {'source': 'cal_source_', 'commission_type': 'cal_commission_'}


def _fuzzy_label_key(text: str) -> str:
    """去非文字非中日韩字符后的比较键（沿用 sync_choice_renames 的老口径）。"""
    return re.sub(r'[^\w一-鿿]', '', text or '')


def reconcile_choice_list(conn, kind: str, tokens, confirmed_deletes=None) -> dict:
    """把 choice_items 对齐到设置页提交的清单 —— **提交清单就是唯一事实源**。

    取代 sync_choice_renames（模糊改名改 orders 值）+ apply_source_deletions 的清单部分：
    卡 168 起用户枚举以 code 存储，改名只该动 `choice_items.label`，订单列一律不搬。
    tokens 允许 code 或 label 混排（新建项在前端还没有 code），提交顺序即 sort_order。
    未被提交的既有行：
      ① 去符号后与某个提交项唯一同名 → 认定是改名，只改 label、code 保留（配置零搬运）；
      ② 否则删行 + 清 `cal_<mode>_<code>`；
      ③ 但被订单引用时先看确认清单 —— 已确认则把 orders 列置空再删，未确认则保留并补回
         清单末尾（D9 防误删，原 apply_source_deletions 的语义）。
    ⚠️ 改名判定必须在「登记新行」**之前**：否则提交「微 信」（库里是「微信」）会先建出
    一行同名新行、再给旧行改名，白得一行重复 label + 一个多余的 src_NNN 码。
    在调用方事务内执行，不 commit。返回 {'codes','renamed','deleted','kept','nulled'}。
    """
    prefix = _CAL_PREFIX_BY_KIND[kind]
    column = CHOICE_REGISTRY[kind]['field']
    tokens = [t.strip() for t in (tokens or []) if isinstance(t, str) and t.strip()]
    confirmed = {x.strip() for x in (confirmed_deletes or []) if isinstance(x, str) and x.strip()}

    rows = [dict(r) for r in conn.execute(
        'SELECT id, code, label FROM choice_items WHERE kind=? ORDER BY sort_order, id',
        (kind,)).fetchall()]
    by_code = {r['code']: r for r in rows}
    by_label = {r['label']: r for r in rows}
    by_fuzzy: dict = {}
    for r in rows:
        by_fuzzy.setdefault(_fuzzy_label_key(r['label']), []).append(r)

    renamed, deleted, kept, nulled = {}, [], [], []
    ordered, seen = [], set()
    for t in tokens:
        r = by_code.get(t) or by_label.get(t)
        if r is None:
            # 先去符号后同名 → 改名（唯一未认领者）；认不出才是真新值
            cand = [c for c in by_fuzzy.get(_fuzzy_label_key(t), []) if c['code'] not in seen]
            if cand:
                r = cand[0]
                if r['label'] != t:
                    conn.execute("UPDATE choice_items SET label=?, "
                                 "updated_at=datetime('now','localtime') "
                                 'WHERE kind=? AND code=?', (t, kind, r['code']))
                    renamed[r['code']] = (r['label'], t)
                    r = dict(r, label=t)
            else:
                code = _ensure_choice_code_conn(conn, kind, t)
                if not code:
                    continue
                r = {'code': code, 'label': t}
        if r['code'] not in seen:
            seen.add(r['code'])
            ordered.append((r['code'], r['label']))

    for r in rows:
        if r['code'] in seen:
            continue
        refs = conn.execute(
            f'SELECT id FROM orders WHERE {column} = ?', (r['code'],)).fetchall()
        if refs:
            if r['code'] in confirmed:
                conn.execute(f'UPDATE orders SET {column} = \'\' WHERE {column} = ?', (r['code'],))
                nulled.append(r['code'])
            else:
                kept.append(r['code'])
                ordered.append((r['code'], r['label']))
                continue
        conn.execute('DELETE FROM settings WHERE key=?', (prefix + r['code'],))
        conn.execute('DELETE FROM choice_items WHERE kind=? AND code=?', (kind, r['code']))
        deleted.append(r['code'])

    for i, (code, _lbl) in enumerate(ordered):
        conn.execute('UPDATE choice_items SET sort_order=? WHERE kind=? AND code=?',
                     (i, kind, code))
    if deleted or renamed or nulled:
        logger.info('choice_items reconcile %s：改 %d / 删 %d / 置空 %d / 保护 %d',
                    kind, len(renamed), len(deleted), len(nulled), len(kept))
    return {'codes': [c for c, _ in ordered], 'renamed': renamed,
            'deleted': deleted, 'kept': kept, 'nulled': nulled}


def set_source_platform_fees(conn, platform_tokens, fee_map) -> int:
    """按设置页提交更新来源的平台标记与默认费率（两族 settings 键的替代物）。

    platform_tokens 未提交（None）时不动标记；提交的是**勾选框 value**，允许 code
    或 label（用 `resolve_choice_code` 归一）。fee_map 的键同理（旧版是
    `default_fee_<中文名>` 表单键，卡 168 起模板改 `default_fee_<code>`，解析器两种都认）。
    未出现的行保持原费率（表单每行都带 default_fee_<x>，正常情况下是全量）。
    在调用方事务内执行，不 commit。返回改动行数。
    """
    changed = 0
    if platform_tokens is not None:
        marks = set()
        for t in platform_tokens:
            code = resolve_choice_code('source', t.strip() if isinstance(t, str) else t)
            if code:
                marks.add(code)
        changed += conn.execute(
            "UPDATE choice_items SET is_platform=CASE WHEN code IN "
            f"({','.join('?' * len(marks)) or 'NULL'}) THEN 1 ELSE 0 END "
            "WHERE kind='source'", tuple(marks)).rowcount
    for token, fee in (fee_map or {}).items():
        code = resolve_choice_code('source', token.strip() if isinstance(token, str) else token)
        if not code:
            continue
        try:
            val = float(fee if fee not in (None, '') else 5.0)
        except (TypeError, ValueError):
            continue
        changed += conn.execute(
            "UPDATE choice_items SET default_fee_pct=?, updated_at=datetime('now','localtime') "
            "WHERE kind='source' AND code=?", (val, code)).rowcount
    return changed


def set_source_flags(code: str, is_platform, fee_pct) -> None:
    """写来源的平台标记与默认费率（取代 platform_sources / default_fee_<中文> 两族键）。"""
    try:
        fee = float(fee_pct if fee_pct not in (None, '') else 5.0)
    except (TypeError, ValueError):
        fee = 5.0
    with transaction() as conn:
        conn.execute('UPDATE choice_items SET is_platform=?, default_fee_pct=?, '
                     "updated_at=datetime('now','localtime') "
                     "WHERE kind='source' AND code=?",
                     (1 if is_platform else 0, fee, code))
    _invalidate_choices_cache()


# Spec 26 收款容差与笔数上限（D12② / §3.2）：
# 3000+2000 类浮点组合的 Σ笔 与应收判定统一按 ±0.01；笔数上限防误操作轰炸。
PAYMENT_EPSILON = 0.01
MAX_PAYMENTS_PER_ORDER = 50


# P19-F10 choices 进程内缓存：一次渲染 inject_constants 连调 5 类 get_choices，
# 每类含 settings 读取 + auto-discover 两条 SQL，列表/看板页逐请求线性放大（诊断 P1）。
# 单用户本地场景模块级 dict 即够：无跨进程竞争；Flask 单进程多线程下最坏情况是
# 并发各自重建一次缓存，结果一致，故无锁取舍（注释备案）。
_CHOICES_CACHE: dict = {}


# ── P22a 数据备份与恢复 ──────────────────────────────────────────


def _count_orders(conn) -> int:
    """内部辅助：统计 orders 行数，异常返回 0。"""
    try:
        return conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
    except Exception:
        return 0


def _do_backup():
    """备份核心：copy2 当前 DB_PATH 到 get_backup_dir()，超额删最旧。
    返回 (True, 备份路径) 或 (False, 错误信息)。
    """
    try:
        backup_dir = get_backup_dir()
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        name = f'orders_backup_{ts}.db'
        dst = os.path.join(backup_dir, name)
        shutil.copy2(DB_PATH, dst)
        # 保留策略：列出所有备份文件，按文件名排序，超额删最旧
        files = sorted(
            f for f in os.listdir(backup_dir)
            if f.startswith('orders_backup_') and f.endswith('.db')
        )
        while len(files) > MAX_BACKUPS:
            oldest = files.pop(0)
            try:
                os.remove(os.path.join(backup_dir, oldest))
            except OSError:
                logger.error('删除最旧备份失败: %s', oldest)
        return (True, dst)
    except Exception as e:
        logger.error('备份失败: %s', e)
        return (False, str(e))


def auto_backup():
    """启动时自动备份：仅当 orders 行数 > 0（空库不备份，避免挤掉有用备份）。
    失败仅记日志，不阻断启动。
    """
    try:
        if not os.path.isfile(DB_PATH):
            return
        conn = get_db()
        try:
            cnt = _count_orders(conn)
        finally:
            conn.close()
        if cnt <= 0:
            return
        _do_backup()
    except Exception as e:
        logger.error('自动备份失败: %s', e)


def check_data_recovery_needed() -> bool:
    """空库检测：orders==0 且备份目录存在且含备份文件 → True。
    新装用户无备份目录不误报；异常返回 False 并记日志。
    """
    try:
        backup_dir = get_backup_dir()
        if not os.path.isdir(backup_dir):
            return False
        files = [
            f for f in os.listdir(backup_dir)
            if f.startswith('orders_backup_') and f.endswith('.db')
        ]
        if not files:
            return False
        conn = get_db()
        try:
            cnt = _count_orders(conn)
        finally:
            conn.close()
        return cnt == 0
    except Exception as e:
        logger.error('空库检测异常: %s', e)
        return False


# 卡 183 D1：备份文件名时间戳 orders_backup_YYYYMMDD_HHMMSS.db（_do_backup 生成）
_BACKUP_TS_RE = re.compile(r'^orders_backup_(\d{8}_\d{6})\.db$')


def get_backup_list() -> list:
    """遍历 get_backup_dir() 内 orders_backup_*.db，按文件名倒序。
    每条: {'name','size','mtime','orders','customers','readable','time','time_source'}。
    time 为显示用备份时间：优先解析文件名时间戳（time_source='filename'），
    不符命名/日期非法回退文件 mtime（time_source='mtime'）；mtime 恒为真实文件修改时间，
    不改写既有备份文件属性。计数读取失败的损坏备份 readable=False 且计数为 None
    （不使用 -1 哨兵），仍保留在列表中由前端标注不可用；无目录返回 []。
    """
    backup_dir = get_backup_dir()
    if not os.path.isdir(backup_dir):
        return []
    result = []
    files = sorted(
        (f for f in os.listdir(backup_dir)
         if f.startswith('orders_backup_') and f.endswith('.db')),
        reverse=True
    )
    for fname in files:
        fpath = os.path.join(backup_dir, fname)
        try:
            stat = os.stat(fpath)
        except OSError as e:
            logger.error('备份文件 %s 不可读: %s', fname, e)
            continue
        size_kb = round(stat.st_size / 1024, 1)
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')
        display_time, time_source = mtime, 'mtime'
        ts_match = _BACKUP_TS_RE.match(fname)
        if ts_match:
            try:
                display_time = datetime.strptime(
                    ts_match.group(1), '%Y%m%d_%H%M%S'
                ).strftime('%Y-%m-%d %H:%M')
                time_source = 'filename'
            except ValueError:
                pass
        # 临时只读连接统计行数；失败 → 显式标记不可读（卡 182 D2）
        orders = customers = None
        readable = True
        ro_conn = None
        try:
            ro_conn = sqlite3.connect(f'file:{fpath}?mode=ro', uri=True)
            ro_conn.row_factory = sqlite3.Row
            orders = ro_conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
            customers = ro_conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
        except Exception as e:
            readable = False
            orders = customers = None
            logger.error('备份文件 %s 计数读取失败: %s', fname, e)
        finally:
            if ro_conn is not None:
                ro_conn.close()
        result.append({
            'name': fname,
            'size': size_kb,
            'mtime': mtime,
            'time': display_time,
            'time_source': time_source,
            'orders': orders,
            'customers': customers,
            'readable': readable,
        })
    return result


def create_manual_backup():
    """用户主动触发备份（无条件执行）。"""
    return _do_backup()


def delete_backup(filename):
    """删除单条备份：basename 校验防路径穿越。
    返回 (True, 消息) 或 (False, 错误信息)。
    """
    try:
        if os.path.basename(filename) != filename:
            return (False, '非法文件名')
        if not filename.startswith('orders_backup_') or not filename.endswith('.db'):
            return (False, '仅允许删除备份文件')
        backup_dir = get_backup_dir()
        fpath = os.path.join(backup_dir, filename)
        if not os.path.isfile(fpath):
            return (False, '文件不存在')
        os.remove(fpath)
        return (True, f'已删除 {filename}')
    except Exception as e:
        logger.error('删除备份失败: %s', e)
        return (False, str(e))


def _validate_sqlite_file(file_path: str) -> tuple[bool, str]:
    """校验文件是否为可正常读取的 SQLite 数据库（只读，不改文件、不建表、不迁移）。

    卡 172 D172.1：前 16 字节须严格等于 `SQLite format 3\\0`，再以只读连接执行
    `PRAGMA quick_check` 且唯一结果为 ok。不检查 orders/settings 等业务表，
    因此其他软件生成的结构完整 SQLite 也会通过——能否被 init_db 补齐由初始化流程决定。
    返回 (True, '') 或 (False, 稳定且可面向用户的错误信息)。
    """
    try:
        with open(file_path, 'rb') as f:
            header = f.read(16)
        if header != b'SQLite format 3\x00':
            return (False, '不是有效的 SQLite 数据库文件')
    except OSError as e:
        logger.error('读取候选文件头失败 %s: %s', file_path, e)
        return (False, '文件无法读取')

    conn = None
    try:
        conn = sqlite3.connect(f'file:{file_path}?mode=ro', uri=True)
        rows = conn.execute('PRAGMA quick_check').fetchall()
        if len(rows) == 1 and str(rows[0][0]).lower() == 'ok':
            return (True, '')
        detail = '; '.join(str(r[0]) for r in rows[:3]) if rows else '未知错误'
        logger.error('完整性校验未通过 %s: %s', file_path, detail)
        return (False, '数据库完整性校验未通过')
    except sqlite3.Error as e:
        logger.error('SQLite 校验失败 %s: %s', file_path, e)
        return (False, '数据库无法打开或已损坏')
    finally:
        if conn is not None:
            conn.close()


def _remove_tmp_and_sidecars(tmp_path: str) -> None:
    """删除临时库文件及其 -wal/-shm 边车（只读校验 WAL 格式库会产生边车；
    主文件被 os.replace 消费后边车仍可能孤儿残留，故三者都尝试删）。"""
    if not tmp_path:
        return
    for p in (tmp_path, tmp_path + '-wal', tmp_path + '-shm'):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError as e:
            logger.error('清理临时文件失败 %s: %s', p, e)


def _rollback_to_protective(protective_path: str, db_dir: str) -> bool:
    """把本次保护性备份复制回 DB_PATH 并重跑 init_db；成功返回 True。

    任一环节（无保护备份 / 复制 / 校验 / 替换 / 初始化）失败均记日志并返回 False，
    调用方据此告知用户人工处理，不得声称数据已恢复。临时文件在 finally 清理。
    """
    if not protective_path or not os.path.isfile(protective_path):
        logger.error('自动回滚失败：无可用保护性备份')
        return False
    tmp_path = ''
    try:
        fd, tmp_path = tempfile.mkstemp(dir=db_dir, prefix='.rollback_', suffix='.db')
        os.close(fd)
        shutil.copy2(protective_path, tmp_path)
        ok, err = _validate_sqlite_file(tmp_path)
        if not ok:
            logger.error('回滚副本校验失败 %s: %s', tmp_path, err)
            return False
        os.replace(tmp_path, DB_PATH)
        # 保留 tmp_path 供 finally 清理 -wal/-shm 边车（主文件已被 replace 消费，exists 判空跳过）
        init_db()
        _invalidate_choices_cache()
        _invalidate_stage_flows_cache()
        return True
    except Exception as e:
        logger.error('自动回滚失败: %s', e)
        return False
    finally:
        _remove_tmp_and_sidecars(tmp_path)


def restore_backup(filename: str) -> tuple[bool, str]:
    """从备份恢复（卡 172 安全顺序）：源预检 → 同目录临时副本复检 → 保护性备份
    → os.replace 原子替换 → init_db；初始化失败自动回滚到本次保护性备份。
    返回 (True, 消息) 或 (False, 错误信息)。
    """
    # 守卫：路径穿越 / 非法文件名 / 不存在
    if os.path.basename(filename) != filename:
        return (False, '非法文件名')
    if not filename.startswith('orders_backup_') or not filename.endswith('.db'):
        return (False, '非法文件名')
    backup_dir = get_backup_dir()
    src = os.path.join(backup_dir, filename)
    if not os.path.isfile(src):
        return (False, '备份文件不存在')

    # 1. 源文件预检：失败立即返回，不备份、不触碰现库
    ok, err = _validate_sqlite_file(src)
    if not ok:
        logger.error('备份文件校验失败，拒绝恢复 %s: %s', src, err)
        return (False, f'备份文件无效：{err}')

    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    tmp_path = ''
    protective_path = ''
    try:
        # 2. 同目录临时副本 + 复检（防复制中断或源文件在复制期间变化）
        fd, tmp_path = tempfile.mkstemp(dir=db_dir, prefix='.restore_', suffix='.db')
        os.close(fd)
        shutil.copy2(src, tmp_path)
        ok, err = _validate_sqlite_file(tmp_path)
        if not ok:
            logger.error('临时副本复检失败 %s: %s', tmp_path, err)
            return (False, f'备份文件无效：{err}')

        # 3. 保护性备份（现库存在时），失败即停止，绝不覆盖现库
        if os.path.isfile(DB_PATH):
            bok, binfo = _do_backup()
            if not bok:
                logger.error('恢复前保护性备份失败，已取消恢复: %s', binfo)
                return (False, f'保护性备份失败，已取消恢复：{binfo}')
            protective_path = binfo

        # 4. 原子替换，避免直接覆盖过程中留下半个文件
        os.replace(tmp_path, DB_PATH)
        # 保留 tmp_path 供 finally 清理 -wal/-shm 边车（主文件已被 replace 消费，exists 判空跳过）

        # 5. 初始化（幂等迁移/补列）；失败自动回滚
        try:
            init_db()
        except Exception as init_exc:
            logger.error('恢复后初始化失败，尝试自动回滚: %s', init_exc)
            if _rollback_to_protective(protective_path, db_dir):
                return (False, '恢复失败，已自动恢复原数据库')
            return (False, '恢复失败且自动回滚失败，请保留现有文件并人工处理')

        # 6. 仅在最终成功后失效缓存
        _invalidate_choices_cache()
        _invalidate_stage_flows_cache()
        return (True, f'已从备份 {filename} 恢复')
    except Exception as e:
        logger.error('恢复备份失败: %s', e)
        return (False, str(e))
    finally:
        _remove_tmp_and_sidecars(tmp_path)


def _invalidate_choices_cache():
    """choices 缓存统一失效入口（P19-F10）。

    挂载点：update_settings（写设置唯一入口）、reconcile_choice_list / 合并函数
    （改 choice_items 行）、init_db（迁移改写数据）、create_order/create_order_with_template/
    update_order（订单写入可能带来 auto-discover 新值）。delete_order 不挂（值残留
    无害，下次任一失效点自然清理）。
    """
    _CHOICES_CACHE.clear()


def get_choices(choice_type):
    """统一读取选择列表，**返回值一律是 code**。

    Spec33 卡 160：系统枚举（stage/ddl/payment）base = CODE_TO_LABEL code 集，
    不再读 *_list settings 键；auto-discover 仍追加库内非注册表值。
    Spec33 卡 168：用户枚举（source/commission_type）base = choice_items.code，
    也不再读 *_list；auto-discover 命中未登记值时**补建行**（从"只影响下拉显示"
    升级为"补登记"），否则界面按 code 反查不到中文、筛选按中文查不到单。
    """
    # 系统枚举：base = CODE_TO_LABEL code 集（不读 *_list）
    if choice_type in ('stage', 'ddl', 'payment'):
        base = list(CODE_TO_LABEL.get(choice_type, {}).keys())
        default_set = set(base)
        # auto-discover：追加库内非注册表值（自定义阶段/脏值）
        field = CHOICE_REGISTRY.get(choice_type, {}).get('field')
        if field:
            try:
                conn = get_db()
                rows = conn.execute(
                    f"SELECT DISTINCT {field} FROM orders WHERE {field} IS NOT NULL AND {field} != ''"
                ).fetchall()
                conn.close()
                for r in rows:
                    v = r[0] if isinstance(r, tuple) else r[field]
                    # 只添加不在 base 中且不是已知 code 的值
                    if v and v not in base and v not in default_set:
                        base.append(v)
            except Exception:
                pass
        # 合并保序去重
        seen, merged = set(), []
        for v in base:
            if v and v not in seen:
                seen.add(v)
                merged.append(v)
        _CHOICES_CACHE[choice_type] = merged
        return list(merged)

    # 用户枚举（source/commission_type）：base = choice_items.code（按 sort_order, id）
    reg = CHOICE_REGISTRY.get(choice_type, {})

    cached = _CHOICES_CACHE.get(choice_type)
    if cached is not None:
        return list(cached)

    base = [r['code'] for r in get_choice_rows(choice_type)]

    # auto-discover：库内出现但未登记的值 **补建行** 后并入（spec §5.1）。
    # 老逻辑只把原值塞进列表（界面显示中文、筛选却传中文），是卡 168 修的那个洞。
    field = reg.get('field')
    if field:
        try:
            conn = get_db()
            rows = conn.execute(
                f"SELECT DISTINCT {field} FROM orders WHERE {field} IS NOT NULL AND {field} != ''"
            ).fetchall()
            conn.close()
            for r in rows:
                v = r[0] if isinstance(r, tuple) else r[field]
                if not v:
                    continue
                code = enum_code(choice_type, v)
                if code is None:
                    code = ensure_choice_code(choice_type, v)
                if code and code not in base:
                    base.append(code)
        except Exception as e:
            logger.error('get_choices(%s) auto-discover 失败：%s', choice_type, e)

    seen, merged = set(), []
    for v in base:
        if v and v not in seen:
            seen.add(v)
            merged.append(v)
    _CHOICES_CACHE[choice_type] = merged
    return list(merged)


def get_paid_statuses():
    """已收款状态（系统锁定）"""
    return PAID_STATUSES


def get_archive_paid_statuses():
    """归档判定的收款状态（系统锁定，P19-F1：含免收；与收入统计集合拆分）"""
    return ARCHIVE_PAID_STATUSES


# P19-F2 终态元数据：位置魔法（stages[-2]/stages[-1]、ddl[0..4]）的确定性替代。
# stage/ddl 注册表均 editable=False（系统锁定，标准值固定）；get_choices auto-discover
# 从 orders 表追加的非标准阶段/DDL 值不在本表注册，一律视为非终态，不影响判定。
STAGE_META = {
    'pending':   {'terminal': False, 'kind': None,     'progress': 0},
    'sketch':    {'terminal': False, 'kind': None,     'progress': 20},
    'lineart':   {'terminal': False, 'kind': None,     'progress': 40},
    'detail':    {'terminal': False, 'kind': None,     'progress': 60},
    'finish':    {'terminal': False, 'kind': None,     'progress': 80},
    'completed': {'terminal': True,  'kind': 'done',   'progress': 100},
    'cancelled': {'terminal': True,  'kind': 'refund', 'progress': 100},
}

DDL_STATUS_META = {
    'normal':  {'kind': 'normal',  'label': '正常',     'emoji': ''},
    'due':     {'kind': 'due',    'label': '即将到期',  'emoji': ''},
    'overdue': {'kind': 'overdue','label': '逾期',      'emoji': '\U0001f534'},
    'done':    {'kind': 'done',   'label': '已完成',    'emoji': '\u2705'},
    'refund':  {'kind': 'refund', 'label': '已退单',    'emoji': ''},
}


def is_terminal_stage(stage: str) -> bool:
    """是否终态阶段（完成/退单）；auto-discover 的非标准阶段一律非终态。
    兼容读：内部先过 enum_code_or_raw，对未迁移的中文值同样工作。"""
    code = enum_code_or_raw('stage', stage)
    return bool(STAGE_META.get(code, {}).get('terminal'))


def is_refund_stage(stage: str) -> bool:
    """是否退单类终态。
    兼容读：内部先过 enum_code_or_raw。"""
    code = enum_code_or_raw('stage', stage)
    return STAGE_META.get(code, {}).get('kind') == 'refund'


def get_done_stage() -> str:
    """完成类终态的 code"""
    for name, m in STAGE_META.items():
        if m.get('kind') == 'done':
            return name
    return 'completed'


def get_refund_stage() -> str:
    """退单类终态的 code"""
    for name, m in STAGE_META.items():
        if m.get('kind') == 'refund':
            return name
    return 'cancelled'


def get_terminal_stages() -> set:
    """终态阶段集合（供 SQL NOT IN / 成员判定用）"""
    return {name for name, m in STAGE_META.items() if m.get('terminal')}


def get_stage_progress(stage: str, order=None) -> int:
    """阶段进度百分比（Spec12：快照优先链）。
    查找顺序：本单快照 stage_flow → STAGE_META → 退单=100 / 未知=0。
    order 可为 dict/Row/dataclass；传 None 时退化为仅查 STAGE_META。
    兼容读：STAGE_META 查找前先过 enum_code_or_raw。
    """
    # 1. 快照命中
    if order is not None:
        snap = get_order_stage_flow(order)
        for s in snap:
            if s['name'] == stage:
                p = _to_int(s.get('progress'))
                if p is not None:
                    return p
    # 2. STAGE_META 兜底（含退单=100、未知=0）
    code = enum_code_or_raw('stage', stage)
    return int(STAGE_META.get(code, {}).get('progress', 0))


def get_ddl_status(kind: str) -> str:
    """按语义 kind 取 DDL 状态 code（normal/due/overdue/done/refund）"""
    for code, m in DDL_STATUS_META.items():
        if m.get('kind') == kind:
            return code
    return 'normal'


# ═══════════════════════════════════════════════════════════
# Spec12 阶段流程预设（stage_flows）
# 锁定三锚点：pending(0)/completed(100) 为每条流程的首尾；cancelled 独立终态，不进流程
# 订单 orders.stage_flow 存本单快照 JSON，改设置预设不影响历史订单
# ═══════════════════════════════════════════════════════════

_STAGE_FLOWS_CACHE = None  # 进程内缓存（None 表示未加载）

# 锁定锚点（不可改名/改百分比，前端/后端强校验）
ANCHOR_FIRST_NAME = 'pending'
ANCHOR_FIRST_PROGRESS = 0
ANCHOR_LAST_NAME = 'completed'
ANCHOR_LAST_PROGRESS = 100


def _invalidate_stage_flows_cache():
    """stage_flows 缓存统一失效入口（挂载点：save_stage_flows / init_db 迁移后）"""
    global _STAGE_FLOWS_CACHE
    _STAGE_FLOWS_CACHE = None


def _default_stage_flows() -> list:
    """默认流程（与 _ensure_default_settings 写入的兜底值保持一致）"""
    return [
        {'name': '默认流程', 'stages': [
            {'name': 'pending',   'progress': ANCHOR_FIRST_PROGRESS},
            {'name': 'sketch',    'progress': 20},
            {'name': 'lineart',   'progress': 40},
            {'name': 'detail',    'progress': 60},
            {'name': 'finish',    'progress': 80},
            {'name': 'completed', 'progress': ANCHOR_LAST_PROGRESS},
        ]},
    ]


def get_stage_flows() -> list:
    """读取全部流程预设；返回 list[{'name':str,'stages':[{'name':str,'progress':int}]}]。
    优先进程内缓存；解析失败/空则回退 _default_stage_flows。
    """
    global _STAGE_FLOWS_CACHE
    if _STAGE_FLOWS_CACHE is not None:
        return [dict(f, stages=[dict(s) for s in f['stages']]) for f in _STAGE_FLOWS_CACHE]
    try:
        settings = get_all_settings()
        raw = settings.get('stage_flows', '')
        flows = json.loads(raw) if raw else []
    except Exception:
        flows = []
    if not flows or not isinstance(flows, list):
        flows = _default_stage_flows()
    # 校验过的才缓存（脏数据也兜底）
    _STAGE_FLOWS_CACHE = flows
    return [dict(f, stages=[dict(s) for s in f['stages']]) for f in flows]


def _normalize_stage_name(raw) -> str:
    """中间阶段名归一：strip → 能识别成 stage code（含「色稿」等全部中文旧标签）就转 code
    → 否则原样返回（自定义名）。复用 enum_code_or_raw，绝不靠字符集猜。
    供 save_stage_flows 校验前统一存储值：先归一再查重，令 sketch 与「色稿」落库同值，
    二者并存会被重复校验拦下。"""
    if not isinstance(raw, str):
        return raw
    return enum_code_or_raw('stage', raw.strip())


def save_stage_flows(flows: list) -> None:
    """保存流程预设（先把中间阶段名归一为 code，再逐条 validate，任一失败抛 ValueError）。
    归一在查重之前，令「色稿」与 sketch 落库同值 → 二者并存触发重复错误。
    成功后写 settings 并统一失效缓存（_invalidate_choices_cache 也会连带触发，
    保证 stage 选择列表 auto-discover 与新流程同步）。
    """
    normalized = []
    for f in flows:
        stages = f.get('stages', [])
        last_idx = len(stages) - 1
        new_stages = []
        for idx, s in enumerate(stages):
            # 仅归一中间阶段名；首尾锚点必须已是 pending/completed 原值
            if isinstance(s, dict) and 0 < idx < last_idx:
                s = dict(s, name=_normalize_stage_name(s.get('name')))
            new_stages.append(s)
        normalized.append(dict(f, stages=new_stages))
    for f in normalized:
        ok, msg = validate_stage_flow(f.get('stages', []))
        if not ok:
            raise ValueError(f"流程「{f.get('name','')}」校验失败：{msg}")
    update_settings({'stage_flows': json.dumps(normalized, ensure_ascii=False)})
    _invalidate_stage_flows_cache()


def validate_stage_flow(stages) -> tuple:
    """校验单条流程的阶段列表。返回 (ok: bool, msg: str)。
    规则：
    - stages 必须是 list，且至少 2 项（首尾锚点）
    - 首项 = {pending, 0}；末项 = {completed, 100}
    - 中间项：0 < progress < 100，严格递增
    - 名称：非空、流程内不重复
    - 中间阶段名（Spec33 D18 范围澄清，卡171）：
      · 允许引用内置阶段 code（sketch/lineart/detail/finish 等非终态 stage code）——
        出厂默认流程本就是这些 code，D18 从未禁止流程引用内置阶段；
      · 终态 stage code（completed/cancelled）不得出现在中间（退单在中间会误触发
        自动归档/退款判断）；
      · 仍拒绝其他类别保留字 / 旧标签（防真自定义阶段名撞 code，D18 对自定义名继续生效）。
    """
    if not isinstance(stages, list) or len(stages) < 2:
        return (False, "至少需要 2 个阶段（首尾锚点）")
    first, last = stages[0], stages[-1]
    if not isinstance(first, dict) or not isinstance(last, dict):
        return (False, "阶段结构非法（需要 {name, progress}）")
    # 锚点校验
    if first.get('name') != ANCHOR_FIRST_NAME:
        return (False, f"首阶段必须为「{enum_label('stage', ANCHOR_FIRST_NAME)}」")
    if _to_int(first.get('progress')) != ANCHOR_FIRST_PROGRESS:
        return (False, f"首阶段进度必须为 {ANCHOR_FIRST_PROGRESS}%")
    if last.get('name') != ANCHOR_LAST_NAME:
        return (False, f"末阶段必须为「{enum_label('stage', ANCHOR_LAST_NAME)}」")
    if _to_int(last.get('progress')) != ANCHOR_LAST_PROGRESS:
        return (False, f"末阶段进度必须为 {ANCHOR_LAST_PROGRESS}%")
    # 遍历校验（Spec33 D18 范围澄清，卡171：中间阶段放行内置 stage code）
    reserved = RESERVED_ENUM_CODES
    stage_codes = CODE_TO_LABEL.get('stage', {})
    seen = {ANCHOR_FIRST_NAME, ANCHOR_LAST_NAME}
    prev = ANCHOR_FIRST_PROGRESS
    for i, s in enumerate(stages):
        if not isinstance(s, dict):
            return (False, f"第 {i+1} 项结构非法")
        name = (s.get('name') or '').strip()
        if not name:
            return (False, f"第 {i+1} 项名称为空")
        progress = _to_int(s.get('progress'))
        if progress is None:
            return (False, f"第 {i+1} 项进度非法")
        # 首末锚点单独校验，中间项走下面这套
        if i != 0 and i != len(stages) - 1:
            if is_terminal_stage(name):
                return (False, f"阶段名「{enum_label('stage', name)}」为终态阶段，不能作为中间阶段")
            if name not in stage_codes and name in reserved:
                return (False, f"阶段名「{enum_label('stage', name)}」为系统保留字，不可用作自定义阶段名")
            if name in seen:
                return (False, f"阶段名「{name}」重复")
            if not (prev < progress < ANCHOR_LAST_PROGRESS):
                return (False, f"第 {i+1} 项进度必须严格递增且在 0-100 之间")
        seen.add(name)
        prev = progress
    return (True, '')


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def get_order_stage_flow(order) -> list:
    """取本单阶段快照（仅 stages 列表）。order 可为 dict/Row/dataclass。
    优先级：orders.stage_flow JSON → 默认流程第一条的 stages。解析失败/损坏回退默认。
    """
    raw = None
    if order is not None:
        try:
            raw = order['stage_flow'] if isinstance(order, dict) else getattr(order, 'stage_flow', None)
        except Exception:
            raw = None
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list) and len(data) >= 2:
                # 返回浅拷贝避免调用方污染
                return [{'name': s.get('name'), 'progress': _to_int(s.get('progress')) or 0}
                        for s in data]
        except Exception:
            logging.error("stage_flow JSON 解析失败，回退默认流程: %r", raw[:200] if isinstance(raw, str) else raw)
    flows = get_stage_flows()
    return [dict(s) for s in flows[0]['stages']] if flows else _default_stage_flows()[0]['stages']


def get_order_stage_names(order) -> list:
    """便捷：本单快照阶段名列表（含锚点，不含退单）。看板快速切换按钮用。"""
    return [s['name'] for s in get_order_stage_flow(order)]


def parse_stage_flow_from_form(raw) -> str | None:
    """表单 stage_flow 字段（JSON 字符串）解析。
    返回解析后的 JSON 字符串（保持字符串形式供 db 层落库），或 None（未提供/空串）。
    解析失败或校验不通过抛 ValueError，调用方应返回 400。
    """
    if raw is None:
        return None
    s = raw if isinstance(raw, str) else str(raw)
    s = s.strip()
    if not s:
        return None
    try:
        data = json.loads(s)
    except Exception as e:
        raise ValueError(f"stage_flow JSON 非法：{e}")
    if not isinstance(data, list):
        raise ValueError("stage_flow 必须是数组")
    ok, msg = validate_stage_flow(data)
    if not ok:
        raise ValueError(msg)
    # 重新序列化为规范 JSON（去多余空格/转义），统一落库
    return json.dumps(data, ensure_ascii=False)





def _paid_status_sql(field='payment_status'):
    """生成已收款状态的 SQL IN 条件 + 参数列表"""
    paid = get_paid_statuses()
    placeholders = ','.join(['?' for _ in paid])
    return f"{field} IN ({placeholders})", list(paid)


def _ensure_default_settings(conn):
    """确保 settings 表中有默认值"""
    defaults = {
        'theme_bg': '#f9f9f7',
        'theme_surface': '#fcfcfb',
        'theme_sidebar': '#f5f4f1',
        'theme_text': '#0b0b0b',
        'theme_text_secondary': '#52514e',
        'theme_border': '#e1e0d9',
        'theme_accent': '#0b0b0b',
        'theme_link': '#2a78d6',
        'theme_success': '#0ca30c',
        'theme_warning': '#fab219',
        'theme_danger': '#d03b3b',
        'stage_pending': '#898781',
        'stage_sketch': '#2a78d6',
        'stage_lineart': '#1baf7a',
        'stage_detail': '#eda100',
        'stage_finish': '#4a3aa7',
        'stage_completed': '#008300',
        'stage_cancelled': '#e34948',
        # 外观设置
        'font_size': '16px',
        'font_family': 'system',
        # ⚠️ 卡 168：`source_list` / `platform_sources` / `payment_status_list` /
        # `commission_type_list` 四个中文键**禁止**再写入默认值。迁移引擎 M12/M13/M14
        # 已把它们删掉（值进了 choice_items.code / is_platform 列），这里每次启动
        # INSERT OR IGNORE 等于把键复活，随即被 apply_legacy_source_settings 当成
        # 用户最新配置回写列 —— 实测米画师平台标记被 0→1 漂移、新建单凭空预填 5% 费率。
        # Calendar color palettes (per mode) — validated against dataviz reference
        # 卡 169 D-3：键后缀一律 code（与 M8-M10 的换键结果、CALENDAR_PALETTES、
        # 设置页着色面板、`_build_theme_css` 读链四处对齐）。此前留中文后缀，导致
        # 每次启动 INSERT OR IGNORE 把 M8 已删的中文键复活成默认色，而取色中文优先
        # → 用户自定的 7 个阶段色被默认值盖掉。
        'cal_source_mihuashi': '#2a78d6',
        'cal_source_bili': '#1baf7a',
        'cal_source_wechat': '#eda100',
        'cal_source_other': '#008300',
        'cal_stage_pending': '#898781',
        'cal_stage_sketch': '#2a78d6',
        'cal_stage_lineart': '#1baf7a',
        'cal_stage_detail': '#eda100',
        'cal_stage_finish': '#4a3aa7',
        'cal_stage_completed': '#008300',
        'cal_stage_cancelled': '#e34948',
        'cal_ddl_normal': '#0ca30c',
        'cal_ddl_due': '#fab219',
        'cal_ddl_overdue': '#d03b3b',
        'cal_ddl_done': '#898781',
        'cal_payment_deposit_paid': '#2a78d6',
        'cal_payment_unpaid': '#fab219',
        'cal_payment_settled': '#0ca30c',
        'cal_payment_debt': '#d03b3b',
        'cal_payment_waived': '#008300',
        # Spec12 阶段流程预设：JSON 数组，每项为一条流程（中间阶段由 STAGE_META 非终态派生）
        'stage_flows': json.dumps([
            {'name': '默认流程', 'stages': [
                {'name': 'pending',   'progress': 0},
                {'name': 'sketch',    'progress': 20},
                {'name': 'lineart',   'progress': 40},
                {'name': 'detail',    'progress': 60},
                {'name': 'finish',    'progress': 80},
                {'name': 'completed', 'progress': 100},
            ]},
        ], ensure_ascii=False),
        # Spec19 VIP 折扣预设：JSON 数组（客户表单折扣输入 datalist 候选值，单位 %）
        'vip_discount_presets': json.dumps([95, 90, 88, 80]),
        # Spec20 日历订阅：默认关闭；token 留空（开启时由设置页生成，卡 86）
        'feed_enabled': '0',
        'feed_token': '',
    }
    for k, v in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )
    # 卡 168 T168.4：清残留（幂等）。M12/M13/M14 已经删过这四个键，此后任何残留
    # 都是历史版本的 `_ensure_default_settings` 复活出来的伪配置 —— 必须抹掉，
    # 否则 `apply_legacy_source_settings`（靠「键存在才应用」判定）会拿它回写
    # is_platform / default_fee_pct 列，实测把米画师的平台标记从 0 漂到 1。
    # ⚠️ 只在**迁移完成后**才敢删：未迁移库的这四个键是用户原序/平台勾选的唯一
    # 来源，迁移引擎后面还要读（M11/M12 搬列、M13/M14 删键由引擎自己负责）。
    if _enum_codes_migrated(conn):
        leftover = conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key IN "
            "('source_list','platform_sources','payment_status_list','commission_type_list')"
        ).fetchone()[0]
        if leftover:
            conn.execute(
                "DELETE FROM settings WHERE key IN "
                "('source_list','platform_sources','payment_status_list','commission_type_list')")
            logger.info('卡168 T168.4: 清理 4 个遗留配置键 %d 行', leftover)


def _cleanup_legacy_palette_keys(conn) -> None:
    """卡 169 D-3：收口历史遗留的中文后缀 `cal_*` 配色键（幂等）。

    判据统一为「后缀能被解析成**另一个** code」（即后缀是标签或旧写法）。解析器按 kind
    分流，与 `models._normalize_enum_token` 同规：系统三类走 `enum_code`（认不出返回
    None），用户两类走 `resolve_choice_code`（读 choice_items，只读不建行）。解析不出
    code 的自定义阶段名（`cal_stage_草稿`）天然不受影响。
    命中后按 code 键是否已存在分两种处理：
      · `cal_<kind>_<code>` 已存在 ⇒ 中文键是旧版 `_ensure_default_settings` 复活出来
        的默认色残渣（留着会盖住用户配色，并被 save_color_preset 的整包快照带走）→ 删。
      · `cal_<kind>_<code>` 不存在 ⇒ 用户色只活在这一把中文键上 → **搬值过去再删**。
        M8-M10 对 source/commission_type 用的是 `seed_choice_items` 返回的**在用例**映射
        （`first_run=False` 时只含被订单用到的值），未被任何订单使用的内置标签不在其中、
        换键被跳过；此处若直接删就等于丢色（卡 169 真实库实测丢 6 个稿件类别色）。

    ⚠️ 调用点必须在 `init_db()` **末尾**：门控用的 `user_version=33` 由迁移引擎在本函数
    之外的后段才写入，放在中段会因门控为 False 整块跳过（卡 169 实测首启漏跑）。
    """
    _cal_kinds = {'cal_stage_': 'stage', 'cal_ddl_': 'ddl', 'cal_payment_': 'payment',
                  'cal_source_': 'source', 'cal_commission_': 'commission_type'}
    _sys_kinds = ('stage', 'ddl', 'payment')
    handled, moved = [], []
    for _key, _value in conn.execute(
            "SELECT key, value FROM settings WHERE key GLOB 'cal_*'").fetchall():
        for _prefix, _kind in _cal_kinds.items():
            if not _key.startswith(_prefix):
                continue
            _suffix = _key[len(_prefix):]
            _code = (enum_code(_kind, _suffix) if _kind in _sys_kinds
                     else resolve_choice_code(_kind, _suffix))
            if _code and _code != _suffix:
                _code_key = _prefix + _code
                if not conn.execute('SELECT 1 FROM settings WHERE key = ?',
                                    (_code_key,)).fetchone():
                    conn.execute('INSERT INTO settings (key, value) VALUES (?, ?)',
                                 (_code_key, _value))
                    moved.append(_code_key)
                conn.execute('DELETE FROM settings WHERE key = ?', (_key,))
                handled.append(_key)
            break
    if handled:
        logger.info('卡169 D-3: 收口中文后缀配色键 %d 个（其中搬值保色 %d 个: %s）: %s',
                    len(handled), len(moved), moved, handled)


# ═══════════════════════════════════════════════════════════
# 设置 CRUD
# ═══════════════════════════════════════════════════════════

def get_all_settings() -> dict:
    """获取所有设置，返回 {key: value}"""
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}


# Spec19 VIP 折扣预设兜底值（settings 缺失/损坏时使用）
DEFAULT_VIP_DISCOUNT_PRESETS = [95, 90, 88, 80]


def get_vip_discount_presets() -> list:
    """Spec19：VIP 折扣预设列表（每项 ∈ (0,100] 的数字）。
    settings 缺失/JSON 损坏/非法项全部剔除后为空时回退默认值。"""
    raw = get_all_settings().get('vip_discount_presets', '')
    try:
        vals = json.loads(raw) if raw else DEFAULT_VIP_DISCOUNT_PRESETS
    except (TypeError, ValueError):
        logging.warning('vip_discount_presets JSON 损坏，回退默认值: %r', raw)
        return list(DEFAULT_VIP_DISCOUNT_PRESETS)
    if not isinstance(vals, list):
        return list(DEFAULT_VIP_DISCOUNT_PRESETS)
    clean = []
    for v in vals:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if 0 < f <= 100:
            clean.append(int(f) if f == int(f) else f)
    return clean if clean else list(DEFAULT_VIP_DISCOUNT_PRESETS)


def update_settings(data: dict, conn=None) -> None:
    """批量更新设置；传入事务连接则不 commit（P19-F5：重命名+设置同事务）。"""
    own = conn is None
    if own:
        conn = get_db()
    for k, v in data.items():
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )
    if own:
        conn.commit()
        conn.close()
    # P19-F10：写设置后统一失效 choices 缓存（source_list/commission_type_list 等即时生效）
    _invalidate_choices_cache()


# ── Spec 27 task-117：统计实验室预设 CRUD（settings 表 JSON 数组存储）──

_STATS_LAB_PRESETS_KEY = 'stats_lab_presets'


def get_stats_lab_presets() -> list:
    """获取统计实验室预设列表。缺失 / JSON 损坏 → 返回 []。"""
    raw = get_all_settings().get(_STATS_LAB_PRESETS_KEY, '')
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except (ValueError, TypeError):
        pass
    return []


def _save_stats_lab_presets(presets: list) -> None:
    """保存预设列表（整体覆盖）。"""
    update_settings({_STATS_LAB_PRESETS_KEY: json.dumps(presets, ensure_ascii=False)})


def merge_commission_types(old_names: list, new_name: str) -> dict:
    """类别合并：把多个旧类别统一改判到目标类别（**全程 code 空间**）。

    卡 168 重写：入参 old_names 允许 code 或中文 label（前端在新建项未落库时只能给
    label），逐个归一为 code；new_name 是目标显示名 —— 已有同名项则并进它的 code，
    没有则当场登记新行。orders 列写 code，改名只动 choice_items.label。
    返回 {'merged': {入参: 单数}, 'total': int}。
    """
    if not old_names or not (new_name or '').strip():
        return {'merged': {}, 'total': 0}
    new_name = new_name.strip()
    merged = {}
    with transaction() as conn:
        ensure_choice_items_table(conn)
        target_code = (_ensure_choice_code_conn(conn, 'commission_type', new_name)
                       or new_name)
        old_codes = []
        for old in old_names:
            code = resolve_choice_code('commission_type', old.strip() if isinstance(old, str) else old)
            if code and code != target_code and code not in old_codes:
                old_codes.append(code)
        for old in old_codes:
            cnt = conn.execute(
                "UPDATE orders SET commission_type=?, updated_at=datetime('now','localtime') "
                "WHERE commission_type=?", (target_code, old)).rowcount
            if cnt > 0:
                merged[old] = cnt
        # 目标项改名（唯一事实源）+ 旧行删除（含颜色键，仅当无订单残留）
        row = conn.execute('SELECT label FROM choice_items WHERE kind=? AND code=?',
                           ('commission_type', target_code)).fetchone()
        if row and row[0] != new_name:
            conn.execute("UPDATE choice_items SET label=?, updated_at=datetime('now','localtime') "
                         'WHERE kind=? AND code=?', (new_name, 'commission_type', target_code))
        # Spec 28 迁移语义：目标无色而旧项有 → 继承第一个旧色（必须早于下面的删键）
        new_color_key = f'cal_commission_{target_code}'
        have_new = conn.execute('SELECT 1 FROM settings WHERE key=?',
                                (new_color_key,)).fetchone()
        if not have_new:
            for old in old_codes:
                old_color = conn.execute('SELECT value FROM settings WHERE key=?',
                                         (f'cal_commission_{old}',)).fetchone()
                if old_color:
                    conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)',
                                 (new_color_key, old_color[0]))
                    break
        # 旧行删除（含颜色键），仅当无订单残留
        for old in old_codes:
            left = conn.execute('SELECT COUNT(*) FROM orders WHERE commission_type=?',
                                (old,)).fetchone()[0]
            if left:
                logger.warning('类别合并：%s 仍有 %d 单残留，保留行', old, left)
                continue
            conn.execute('DELETE FROM choice_items WHERE kind=? AND code=?',
                         ('commission_type', old))
            conn.execute('DELETE FROM settings WHERE key=?', (f'cal_commission_{old}',))
    _invalidate_choices_cache()
    total = sum(merged.values())
    logger.info('类别合并：%s → 「%s」，共更新 %d 单', list(merged.keys()), new_name, total)
    return {'merged': merged, 'total': total}


def get_default_fee_for_source(source: str) -> float:
    """P19-F9：来源默认费率 %（choice_items.default_fee_pct 列，缺省 5.0，与订单表单默认值一致）；
    非平台来源/空来源 → 0.0（无手续费语义）。读已提交设置（自开连接），供写入管线使用。

    卡 169 D-2：入参兼容 code 与中文标签两种写法。写入管线给的是 code，而平台集合
    按 code 存 —— 直接拿 code 去比标签集会判成「非平台」，平台单费率恒为 0
    （实测 `bili` → 0.0 / `B站工坊` → 5.0，同样的钱一个扣费一个不扣）。
    """
    if not source:
        return 0.0
    code = resolve_choice_code('source', source) or source
    if code not in get_platform_source_codes():
        return 0.0
    for r in get_choice_rows('source'):            # Spec 33 卡 159：费率已列化
        if r['code'] == code:
            try:
                return float(r['default_fee_pct'])
            except (TypeError, ValueError):
                return 5.0
    try:
        return float(get_all_settings().get(f"default_fee_{choice_label('source', code)}", '5') or 5)
    except (TypeError, ValueError):
        return 5.0


def get_default_fees_map() -> dict:
    """#43：全部平台来源的默认费率映射 {source: pct}，供前端表单联动填充。
    非平台来源不入表（前端取不到 → 费率区隐藏/不填）。"""
    fees = {}
    try:
        settings = get_all_settings()
    except Exception as e:
        logging.error(f'get_default_fees_map 读取设置失败: {e}')
        settings = {}
    rows = {r['label']: r for r in get_choice_rows('source')}
    for src in get_platform_sources():
        r = rows.get(src)
        if r is not None:                          # Spec 33 卡 159：读列
            try:
                fees[src] = float(r['default_fee_pct'])
            except (TypeError, ValueError):
                fees[src] = 5.0
            continue
        try:
            fees[src] = float(settings.get(f'default_fee_{src}', '5') or 5)
        except (TypeError, ValueError):
            fees[src] = 5.0
    return fees


def resnapshot_fee_for_renamed_source(conn, order_ids: list, new_source: str) -> int:
    """P19-F9 来源重命名/合并级联：受影响订单 pct 快照刷新为新来源默认费率，
    并按快照重算 platform_fee / actual_received（income 不变），涉及客户统计同步重算。
    在传入事务连接上执行（与重命名/设置更新同事务，卡55 单管线）。
    只处理本次重命名实际波及的订单 id——原本就是新名称的订单不动（历史快照不改写）。

    卡 168：平台标记与费率改读 choice_items 列（platform_sources / default_fee_ 两族
    键已随 T168.4 删除）。`new_source` 传 code；兼容传 label（先归一）。
    """
    if not order_ids:
        return 0
    code = resolve_choice_code('source', new_source) or new_source
    row = conn.execute("SELECT is_platform, default_fee_pct FROM choice_items "
                       "WHERE kind='source' AND code=?", (code,)).fetchone()
    pct = 0.0
    if row and row['is_platform']:
        try:
            pct = float(row['default_fee_pct'] if row['default_fee_pct'] is not None else 5.0)
        except (TypeError, ValueError):
            pct = 5.0
    marks = ','.join('?' * len(order_ids))
    rows = conn.execute(
        f"SELECT id, customer_id, deposit, balance, discounted_income FROM orders WHERE id IN ({marks})",
        list(order_ids)
    ).fetchall()
    cids = set()
    for r in rows:
        income = float(r['deposit'] or 0) + float(r['balance'] or 0)
        # Spec19：费用按折后基数重算（D4 先折后费）；旧行 discounted_income 为 NULL 时兜底原价
        base = float(r['discounted_income']) if r['discounted_income'] is not None else income
        fee = round(base * pct / 100, 2) if pct > 0 else 0.0
        conn.execute(
            "UPDATE orders SET platform_fee_pct = ?, platform_fee = ?, actual_received = ?, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (pct, fee, round(base - fee, 2), r['id'])
        )
        if r['customer_id']:
            cids.add(r['customer_id'])
    for cid in cids:
        recalc_customer_stats(cid, conn=conn)
    return len(rows)


# ── Spec 28 phase-14：来源合并后端（task-132；设置页 UI 归 task-133）──

def merge_sources(old_names: list, new_name: str) -> dict:
    """来源合并（D10：参考 merge_commission_types 同结构，不通用化）。

    卡 168 起**全程 code 空间**：old/new 先解析成 code（新名未登记则当场建行），
    orders.source 写目标 code。平台标记与默认费率从「settings 的 platform_sources /
    default_fee_* 键」改为读写 choice_items 的 is_platform / default_fee_pct 列
    （目标未标平台而任一旧名标了 → 继承；目标费率为默认 5.0 而旧名有非默认值 →
    继承第一个）；日历颜色键 cal_source_<code> 同理（目标无色继承第一个旧色）。
    随后删除旧行与旧色键，并对受影响订单重算费率快照
    （resnapshot_fee_for_renamed_source：按目标来源的默认费率重算 pct/fee/
    actual_received，客户统计同步刷新）。old == new_name 的项跳过。
    返回 {'merged': {旧显示名: count}, 'total': int}。
    """
    if not old_names or not (new_name or '').strip():
        return {'merged': {}, 'total': 0}
    new_name = new_name.strip()
    merged = {}
    with transaction() as conn:
        ensure_choice_items_table(conn)
        target_code = _ensure_choice_code_conn(conn, 'source', new_name) or new_name
        by_code = {r['code']: dict(r) for r in conn.execute(
            "SELECT code, label, is_platform, default_fee_pct FROM choice_items "
            "WHERE kind='source'").fetchall()}
        resolved = []
        for old in old_names:
            token = old.strip() if isinstance(old, str) else old
            code = resolve_choice_code('source', token) if token else None
            if code and code != target_code and code not in resolved:
                resolved.append(code)
        affected_ids: list = []
        for old_code in resolved:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM orders WHERE source = ?", (old_code,)).fetchall()]
            cnt = conn.execute(
                "UPDATE orders SET source = ?, updated_at = datetime('now','localtime') WHERE source = ?",
                (target_code, old_code)
            ).rowcount
            if cnt > 0:
                label = (by_code.get(old_code) or {}).get('label', old_code)
                merged[label] = merged.get(label, 0) + cnt
                affected_ids.extend(ids)
        # 目标行：label 归一 + 平台/费率继承（目标缺省时）
        target_row = by_code.get(target_code)
        old_rows = [by_code[c] for c in resolved if c in by_code]
        if target_row is not None:
            sets, params = [], []
            if target_row['label'] != new_name:
                sets.append('label=?')
                params.append(new_name)
            if not target_row['is_platform'] and any(r['is_platform'] for r in old_rows):
                sets.append('is_platform=1')
            if float(target_row['default_fee_pct'] or 5.0) == 5.0:
                for r in old_rows:
                    fee = float(r['default_fee_pct'] or 5.0)
                    if fee != 5.0:
                        sets.append('default_fee_pct=?')
                        params.append(fee)
                        break
            if sets:
                sets.append("updated_at=datetime('now','localtime')")
                conn.execute(f"UPDATE choice_items SET {', '.join(sets)} "
                             'WHERE kind=? AND code=?', (*params, 'source', target_code))
        # 日历颜色：目标无色继承第一个旧色（必须早于删键）
        color_key = f'cal_source_{target_code}'
        if not conn.execute('SELECT 1 FROM settings WHERE key=?', (color_key,)).fetchone():
            for old_code in resolved:
                old_color = conn.execute('SELECT value FROM settings WHERE key=?',
                                         (f'cal_source_{old_code}',)).fetchone()
                if old_color:
                    conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                                 (color_key, old_color[0]))
                    break
        # 旧行 + 旧色键删除（仅当无订单残留）
        for old_code in resolved:
            left = conn.execute('SELECT COUNT(*) FROM orders WHERE source = ?',
                                (old_code,)).fetchone()[0]
            if left:
                logger.warning('来源合并：%s 仍有 %d 单残留，保留行', old_code, left)
                continue
            conn.execute('DELETE FROM choice_items WHERE kind=? AND code=?', ('source', old_code))
            conn.execute('DELETE FROM settings WHERE key=?', (f'cal_source_{old_code}',))
        # 费率快照级联（同事务；平台标记已迁移，重算口径自洽）
        if affected_ids:
            resnapshot_fee_for_renamed_source(conn, affected_ids, target_code)
    _invalidate_choices_cache()
    total = sum(merged.values())
    logger.info('来源合并：%s → 「%s」，共更新 %d 单', list(merged.keys()), new_name, total)
    return {'merged': merged, 'total': total}


# 订单模板可复用字段白名单（P18-F3：排除日期与金额派生）
ORDER_TEMPLATE_FIELDS = (
    'customer_id', 'project_name', 'source', 'is_commercial',
    'commission_type', 'current_stage', 'payment_status', 'payment_mode',
    'platform_url', 'deposit', 'balance', 'platform_fee_pct', 'notes',
    'discount_pct',  # Spec19 订单级折扣快照（模板可携带折扣预填）
    'stage_flow',  # Spec12 阶段流程快照（模板可携带流程）
    'scheduled_start', 'scheduled_end', 'page_deadline',  # 排期字段
    'estimated_hours', 'work_hours', 'exclude_hourly',  # 工时字段
)


def _snapshot_template_data(form: dict) -> dict:
    """从表单提取可复用字段快照（仅保留白名单中非空值）。"""
    snap = {}
    for k in ORDER_TEMPLATE_FIELDS:
        v = form.get(k)
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == '':
            continue
        snap[k] = v
    return snap


def create_order_template(name: str, data: dict) -> int:
    """保存订单模板：name + 可复用字段快照(JSON)。返回模板 ID。"""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO order_templates (name, data_json) VALUES (?, ?)",
        (name, json.dumps(data, ensure_ascii=False))
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _parse_template_row(row) -> dict:
    d = dict(row)
    try:
        d['data'] = json.loads(d.get('data_json') or '{}')
    except Exception:
        d['data'] = {}
    return d


def list_order_templates() -> list[dict]:
    """模板列表（含解析后的 data），按创建时间倒序。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, data_json, created_at FROM order_templates ORDER BY created_at DESC, id DESC"
    ).fetchall()
    conn.close()
    return [_parse_template_row(r) for r in rows]


def get_order_template(template_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT id, name, data_json, created_at FROM order_templates WHERE id = ?",
        (template_id,)
    ).fetchone()
    conn.close()
    return _parse_template_row(row) if row else None


def delete_order_template(template_id: int) -> None:
    conn = get_db()
    conn.execute("DELETE FROM order_templates WHERE id = ?", (template_id,))
    conn.commit()
    conn.close()


def update_order_template(template_id: int, name: str, data: dict) -> None:
    """全量更新模板：名称 + 可复用字段快照(JSON)。"""
    conn = get_db()
    conn.execute(
        "UPDATE order_templates SET name = ?, data_json = ? WHERE id = ?",
        (name, json.dumps(data, ensure_ascii=False), template_id)
    )
    conn.commit()
    conn.close()


def get_source_list() -> list[str]:
    """所有来源显示名（按用户原序）。

    Spec 33 卡 159：数据源从 settings `source_list` 换成 choice_items 表，
    但**返回值仍是 label（中文显示名）** —— 值空间换码属卡 160/164，本卡零行为变化。
    """
    rows = get_choice_rows('source')
    if rows:
        return [r['label'] for r in rows]
    return [CODE_TO_LABEL['source'][c] for c in CODE_TO_LABEL['source']]


def get_platform_sources() -> set[str]:
    """平台来源显示名集合（choice_items.is_platform 列）。

    卡 159 仍返回 label 集合；`get_default_fee_for_source` 的「非平台 → 0.0」
    门控语义一字未改（实测库里 default_fee_ 对非平台来源也有惰性值，全靠这道门
    才没生效，门一撤就会给微信/QQ 单凭空加 5% 手续费）。
    """
    rows = get_choice_rows('source')
    if rows:
        return {r['label'] for r in rows if r['is_platform']}
    legacy = get_all_settings().get('platform_sources', '')
    plats = {x.strip() for x in legacy.split(',') if x.strip()}
    if not plats:
        logger.warning('get_platform_sources: choice_items 与 settings 均无平台来源，返回空集')
    return plats


def get_platform_source_codes() -> set[str]:
    """平台来源 code 集合 —— 模板做「本单来源是不是平台」判断专用。

    卡 169 D-2：`get_platform_sources()` 给的是**标签**（界面文案用），模板里却拿
    `order.source`（code）去比对，永远比不中 → 平台单渲染成「定金/尾款」块。
    code 空间判断必须用本函数。
    """
    rows = get_choice_rows('source')
    if rows:
        return {r['code'] for r in rows if r['is_platform']}
    # 表未播种时的兜底：get_choice_rows 走内置回退，其 is_platform 同样取自该常量
    return set(BUILTIN_PLATFORM_CODES)


def reset_settings() -> None:
    """重置所有设置为默认值"""
    conn = get_db()
    conn.execute("DELETE FROM settings")
    conn.commit()
    _ensure_default_settings(conn)
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════
# 订单 CRUD
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# 归档写入唯一入口（P19-F1）
# 约定：全库禁止裸写 is_archived=1 的 UPDATE 或字典赋值，
#       DB 直写一律经 set_archived()，管线字典一律经 _apply_archive_to_data()。
# completed_at / is_overdue 为事件快照：已归档单不回算（spec 11 §4）。
# ═══════════════════════════════════════════════════════════


def _calc_is_overdue_flag(scheduled_end, completed_at: str) -> int:
    """逾期完成判定：有排期截止日且 completed_at 晚于该日 → 1。"""
    if not scheduled_end or not completed_at:
        return 0
    try:
        return 1 if completed_at[:10] > str(scheduled_end)[:10] else 0
    except (TypeError, IndexError):
        return 0


def _apply_archive_to_data(data: dict, completed_at: str | None = None) -> dict:
    """管线字典级归档（唯一入口的内存形态）。

    completed_at 优先级：data 已有值（已归档单快照不回算）→ 参数（用户确认归属月）→ 今日。
    已归档单（data 已带 completed_at）的 is_overdue 同样保留快照不重算。
    """
    existing_completed = data.get('completed_at')
    final_completed = existing_completed or completed_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    data['is_archived'] = 1
    data['completed_at'] = final_completed
    if existing_completed:
        data['is_overdue'] = int(data.get('is_overdue') or 0)
    else:
        data['is_overdue'] = _calc_is_overdue_flag(data.get('scheduled_end'), final_completed)
    return data


def set_archived(conn, order_id: int, archived: bool, completed_at: str | None = None, is_overdue: int | None = None) -> None:
    """归档写入唯一入口（DB 直写形态）。

    归档：completed_at 参数优先（用户确认归属月）→ 已有快照 → 今日；
         is_overdue 显式传入则用传入值（P15b/P18-F7 确认链），缺省按 scheduled_end 与 completed_at 比较。
    取消归档：completed_at / is_overdue 同步清空。
    """
    if archived:
        row = conn.execute(
            "SELECT scheduled_end, completed_at, is_overdue FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row and row['completed_at'] and completed_at is None:
            # 已归档单：completed_at 快照不回算；is_overdue 缺省同样保留快照
            final_completed = row['completed_at']
            overdue_flag = int(row['is_overdue'] or 0) if is_overdue is None else (1 if is_overdue else 0)
        else:
            final_completed = completed_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            overdue_flag = (1 if is_overdue else 0) if is_overdue is not None \
                else _calc_is_overdue_flag(row['scheduled_end'] if row else None, final_completed)
        conn.execute(
            "UPDATE orders SET is_archived = 1, completed_at = ?, is_overdue = ?, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (final_completed, overdue_flag, order_id))
    else:
        conn.execute(
            "UPDATE orders SET is_archived = 0, completed_at = NULL, is_overdue = 0, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (order_id,))


def _auto_calc_ddl_status(data: dict):
    """根据日期和阶段自动计算 DDL 状态 + 自动归档（动态选择）"""
    from datetime import date as dt_date
    today = dt_date.today()
    stage = data.get('current_stage', '')
    end_str = data.get('scheduled_end', '')
    payment = data.get('payment_status', '')

    paid = get_archive_paid_statuses()  # P19-F1：归档判定集合（含免收），与收入统计拆分

    # P19-F2：DDL/阶段取值改走元数据（取代 ddl[0..4]、stages[-2]/stages[-1] 位置魔法）
    ddl_normal = get_ddl_status('normal')
    ddl_due = get_ddl_status('due')
    ddl_overdue = get_ddl_status('overdue')
    ddl_done = get_ddl_status('done')
    ddl_cancelled = get_ddl_status('refund')
    stage_done = get_done_stage()
    stage_cancelled = get_refund_stage()

    # 完成 + 已结算/免收 → 自动归档（经唯一入口，写 completed_at/is_overdue）
    if stage == stage_done and payment in paid:
        data['ddl_status'] = ddl_done
        return _apply_archive_to_data(data)

    if stage == stage_done:
        data['ddl_status'] = ddl_done
        return data

    # 退单 + 已结算/免收 → 自动归档（免费退单不计入收入但需归档整理）
    if stage == stage_cancelled and payment in paid:
        data['ddl_status'] = ddl_cancelled
        return _apply_archive_to_data(data)

    if stage == stage_cancelled:
        data['ddl_status'] = ddl_cancelled
        return data

    if end_str and end_str.strip():
        try:
            # #40 P1：精确时间模式存的是 'YYYY-MM-DDTHH:MM'，取前 10 位统一按日期解析
            end_date = dt_date.fromisoformat(end_str.strip()[:10])
            diff = (end_date - today).days
            if diff < 0:
                data['ddl_status'] = ddl_overdue
            elif diff <= 3:
                data['ddl_status'] = ddl_due
            else:
                data['ddl_status'] = ddl_normal
        except (ValueError, TypeError):
            logging.warning("_compute_ddl_status: 无法解析 scheduled_end=%r，ddl_status 回退正常", end_str)
            data['ddl_status'] = ddl_normal
    else:
        data['ddl_status'] = ddl_normal

    return data


def _calc_financials(data: dict) -> dict:
    """自动计算 income、discounted_income、platform_fee、actual_received

    支持两种输入：
    - platform_fee_pct: 百分比（如 5 表示 5%），自动计算 platform_fee
    - platform_fee: 直接金额（兼容旧数据）

    直接来源（微信/QQ/其他）无手续费，platform_fee 强制为 0。
    P19-F9：platform_fee_pct 为订单级快照——随单落库、重算只读订单自身快照，
    与设置页当前费率脱钩（修 C5：设置页改费率静默改写历史财务）。
    Spec15：pct 显式非 None（含 0）→ 一律按快照重算，0 → 费用清零（免手续费语义）；
    直填金额兼容路径仅在 pct 为 None 时走（修 Bug A：编辑填 0 旧费用泄漏）。
    Spec19：discount_pct = 折后应收百分比（88 = 88 折），NULL = 不打折（订单级快照，
    不回溯存量，与 pct 快照同构）；**先折后费**（D4）：折后 = round(income×pct/100, 2)，
    手续费按折后抽成，实收 = 折后 − 手续费；直接来源实收 = 折后。
    """
    deposit = float(data.get('deposit', 0) or 0)
    balance = float(data.get('balance', 0) or 0)
    income = round(deposit + balance, 2)  # #40 P4：浮点相加落库前舍入（0.1+0.2 问题）
    data['income'] = income

    # Spec19 折扣段（先折）：discount_pct 非 NULL → 折后；NULL → 折后 = 原价（D4/D5）
    disc = data.get('discount_pct')
    if disc is not None:
        data['discounted_income'] = round(income * float(disc) / 100, 2)
    else:
        data['discounted_income'] = income
    base = data['discounted_income']  # 手续费基数：折后金额（D4 先折后费）

    source = data.get('source', '')
    # 卡 169 D-2：来源在写入管线里是 code，平台集合也按 code 存；拿 code 比标签集
    # 会把每一笔平台单都判成「非平台」→ 手续费静默归零、实收虚高。
    is_platform = (resolve_choice_code('source', source) or source) in get_platform_source_codes()

    if not is_platform:
        # 直接来源：无手续费
        data['platform_fee'] = 0.0
    else:
        # P19-F9：pct 快照保留在 data 中随单落库（不再 pop）。
        pct = data.get('platform_fee_pct')
        if pct is None and not float(data.get('platform_fee', 0) or 0):
            # #42：创建路径 pct 缺省且无直填金额时回填来源默认费率
            #（编辑路径切换来源已在 update_order 兜底，这里补齐新单漏洞）
            pct = get_default_fee_for_source(source)
            data['platform_fee_pct'] = pct
        if pct is not None:
            # Spec15：显式快照（含 0）→ 一律按快照重算；0 → 费用清零（免手续费）。
            # 修 Bug A：原 pct>0 才重算，编辑填 0 时 merged 继承的旧费用金额泄漏。
            # Spec19：基数由 income 换为折后 base（先折后费）。
            data['platform_fee'] = round(base * float(pct) / 100, 2)
        else:
            # pct 为 None 的直填金额兼容路径（旧数据，#40 P4 舍入保持）
            data['platform_fee'] = round(float(data.get('platform_fee', 0) or 0), 2)

    data['actual_received'] = round(base - data['platform_fee'], 2)
    return data


# P19-F8：写入时复购检测 _auto_detect_repeat 已删除（旧口径含退单且写入即腐烂，诊断 A3）。
# 复购标记改为查询时计算，见 _apply_repeat_for_rows（_refresh_ddl_for_rows 旁）；
# 落库两列暂留仅作旧版本回滚兜底，由 init_db 幂等回填维持新口径。



def create_order(data: dict) -> int:
    """创建订单，返回新 ID（P19-F5：财务→DDL/归档→插入→客户重算 单事务；
    P19-F8 起复购改查询时计算，不再写入落库）。"""
    with transaction() as conn:
        data = _calc_financials(data)
        # 自动计算 DDL 状态 + 自动归档
        data = _auto_calc_ddl_status(data)
        columns = [
            'customer_id', 'project_name', 'source', 'is_commercial',
            'commission_type', 'current_stage', 'ddl_status',
            'deposit', 'balance', 'platform_fee', 'platform_fee_pct', 'income', 'actual_received',
            'discount_pct', 'discounted_income',  # Spec19 VIP 折扣（快照 + 折后落库）
            'payment_status', 'is_archived',
            'payment_mode',  # Spec 26：创建路径支持分期（阶段 3；D10 编辑白名单已在 task-107 登记）
            'notes', 'custom_color', 'platform_url', 'page_deadline', 'scheduled_start', 'scheduled_end', 'sort_order',
            'completed_at', 'is_overdue',
            'estimated_hours', 'work_hours', 'exclude_hourly',  # P20b 时薪
            'stage_flow',  # Spec12 阶段流程快照
        ]
        nullable = {'customer_id', 'commission_type', 'notes', 'custom_color',
                    'platform_url', 'page_deadline', 'scheduled_start', 'scheduled_end', 'completed_at',
                    'estimated_hours', 'work_hours', 'stage_flow',
                    'discount_pct'}  # Spec19：NULL = 不打折（D2）
        # P19-F4 文本列缺省写列 DEFAULT/''，禁止写数字 0（修复幽灵值来源之一）
        text_defaults = {'project_name': '', 'source': 'mihuashi', 'current_stage': 'pending',
                         'ddl_status': 'normal', 'payment_status': 'unpaid'}
        row = {}
        for k in columns:
            val = data.get(k)
            if val is None or val == '':
                if k in nullable:
                    val = None
                elif k in text_defaults:
                    val = text_defaults[k]
                else:
                    val = 0
            row[k] = val
        row['is_commercial'] = int(row['is_commercial'] or 0)
        row['is_archived'] = int(row['is_archived'] or 0)
        row['is_overdue'] = int(row['is_overdue'] or 0)
        row['sort_order'] = int(row['sort_order'] or 0)

        placeholders = ', '.join(['?' for _ in columns])
        cols_str = ', '.join(columns)
        cur = conn.execute(f"INSERT INTO orders ({cols_str}) VALUES ({placeholders})",
                           [row[c] for c in columns])
        order_id = cur.lastrowid

        # 更新客户统计（同事务）
        if row['customer_id']:
            recalc_customer_stats(row['customer_id'], conn=conn)

    # P19-F10：新订单可能带来 auto-discover 新值（手打来源/类别），失效缓存
    _invalidate_choices_cache()
    return order_id


def create_order_with_template(data: dict, template_name: str | None = None,
                               template_data: dict | None = None) -> int:
    """创建订单（可选同时保存模板）—— P19-F5 单事务：插入+recalc+模板入库 全成或全败。"""
    if not template_name:
        return create_order(data)
    with transaction() as conn:
        # 与 create_order 同管线，但复用同一事务连接
        data = _calc_financials(data)
        data = _auto_calc_ddl_status(data)
        columns = [
            'customer_id', 'project_name', 'source', 'is_commercial',
            'commission_type', 'current_stage', 'ddl_status',
            'deposit', 'balance', 'platform_fee', 'platform_fee_pct', 'income', 'actual_received',
            'discount_pct', 'discounted_income',  # Spec19 VIP 折扣（快照 + 折后落库）
            'payment_status', 'is_archived',
            'notes', 'custom_color', 'platform_url', 'page_deadline', 'scheduled_start', 'scheduled_end', 'sort_order',
            'completed_at', 'is_overdue',
            'estimated_hours', 'work_hours', 'exclude_hourly',  # P20b 时薪
            'stage_flow',  # Spec12 阶段流程快照
        ]
        nullable = {'customer_id', 'commission_type', 'notes', 'custom_color',
                    'platform_url', 'page_deadline', 'scheduled_start', 'scheduled_end', 'completed_at',
                    'estimated_hours', 'work_hours', 'stage_flow',
                    'discount_pct'}  # Spec19：NULL = 不打折（D2）
        text_defaults = {'project_name': '', 'source': 'mihuashi', 'current_stage': 'pending',
                         'ddl_status': 'normal', 'payment_status': 'unpaid'}
        row = {}
        for k in columns:
            val = data.get(k)
            if val is None or val == '':
                if k in nullable:
                    val = None
                elif k in text_defaults:
                    val = text_defaults[k]
                else:
                    val = 0
            row[k] = val
        row['is_commercial'] = int(row['is_commercial'] or 0)
        row['is_archived'] = int(row['is_archived'] or 0)
        row['is_overdue'] = int(row['is_overdue'] or 0)
        row['sort_order'] = int(row['sort_order'] or 0)
        placeholders = ', '.join(['?' for _ in columns])
        cols_str = ', '.join(columns)
        cur = conn.execute(f"INSERT INTO orders ({cols_str}) VALUES ({placeholders})",
                           [row[c] for c in columns])
        order_id = cur.lastrowid
        if row['customer_id']:
            recalc_customer_stats(row['customer_id'], conn=conn)
        # 模板快照同事务入库
        conn.execute(
            "INSERT INTO order_templates (name, data_json) VALUES (?, ?)",
            (template_name, json.dumps(template_data or {}, ensure_ascii=False))
        )
    # P19-F10：新订单可能带来 auto-discover 新值，失效缓存
    _invalidate_choices_cache()
    return order_id


def get_order(order_id: int, conn=None) -> dict | None:
    """获取单个订单；传入事务连接则复用（P19-F5）。复购标记查询时计算（P19-F8）。"""
    own = conn is None
    if own:
        conn = get_db()
    row = conn.execute(
        """SELECT o.*, c.name as customer_name
           FROM orders o
           LEFT JOIN customers c ON o.customer_id = c.id
           WHERE o.id = ?""",
        (order_id,)
    ).fetchone()
    result = _apply_repeat_for_rows([dict(row)], conn=conn)[0] if row else None
    if own:
        conn.close()
    return result


# update_order 金额/财务触发字段（P19-F5 单管线判定）
_MONEY_FIELDS = {'deposit', 'balance', 'platform_fee', 'platform_fee_pct', 'source', 'discount_pct'}
_DDL_TRIGGER_FIELDS = {'current_stage', 'scheduled_end', 'payment_status'}


def update_order(order_id: int, data: dict, conn=None) -> tuple:
    """更新订单（P19-F5 单管线 + 单事务）。返回 (True, '') / (False, 原因)。

    流程：读旧单 → merge → financials（金额/来源/客户变化时）
    → ddl/archive（阶段/日期/收款变化或财务已重算时，归档经 _auto_calc_ddl_status 唯一入口）
    → Spec 26 防错守卫（免收互斥 / D12① 金额冲突 / D12③ 切回拦截，拒绝即整单回滚）
    → 单次 UPDATE（D10 白名单含 payment_mode；D12③ 切分期自动生成初始收款）
    → 对 {新客户, 旧客户} 去重 recalc。全部在同一事务连接上。
    消除旧版 money 分支与 customer 分支的重复执行；修 A5（换客户旧客户不刷新）。
    P19-F8：repeat 环节移除，复购标记改查询时计算（_apply_repeat_for_rows），两列不再写入。
    Spec 26：conn= 复用外部事务（收款收齐自动结算在单事务内调用）；不传则自开，行为不变。
    """
    if not data:
        return (False, '无变更字段')

    keys = set(data.keys())
    cust_changed = 'customer_id' in keys
    needs_financials = bool(_MONEY_FIELDS & keys) or cust_changed
    needs_ddl = bool(_DDL_TRIGGER_FIELDS & keys) or needs_financials

    own = conn is None
    with (transaction() if own else nullcontext(conn)) as conn:
        existing = get_order(order_id, conn=conn)
        if not existing:
            return (False, '订单不存在')
        old_cid = existing.get('customer_id')

        merged = {**existing, **data}
        if needs_financials:
            # P19-F9 快照规则：显式提交 platform_fee_pct → 用之并随单落库；
            # 切换来源且合并后 pct 为 None（未指定）→ 按新来源默认费率刷新快照；
            # 否则 → merged 继承 existing 快照原样保留（未显式变更不改写）。
            # #40 P3：编辑路由总会传 platform_fee_pct 键（留空为 None），
            # 改用值判断而非键存在性，修复切回平台来源默认费率不生效。
            if 'source' in keys and merged.get('platform_fee_pct') is None:
                merged['platform_fee_pct'] = get_default_fee_for_source(merged.get('source') or '')
            merged = _calc_financials(merged)
        if needs_ddl:
            # DDL 重算 + 「终态+已结算/免收」自动归档（内部经 _apply_archive_to_data）
            merged = _auto_calc_ddl_status(merged)

        # ── Spec 26 防错守卫（拒绝 = 整单事务回滚，路由层按 (ok, err) 转提示）──
        target_mode = merged.get('payment_mode') or 'simple'
        mode_switch = ('payment_mode' in keys
                       and (data.get('payment_mode') or 'simple')
                       != (existing.get('payment_mode') or 'simple'))
        if mode_switch and target_mode == 'simple':
            # D12③：installment→simple 有收款记录 → 拒绝（先清空再切）
            cnt = conn.execute(
                "SELECT COUNT(*) FROM order_payments WHERE order_id = ?", (order_id,)
            ).fetchone()[0]
            if cnt:
                return (False, f'本单已有 {cnt} 笔收款记录，清空后才能切回整单收款')
        if target_mode == 'installment':
            # 免收互斥（spec §3.2：免收走 simple 归档语义；覆盖切向与置免收双向组合）
            if enum_code_or_raw('payment', merged.get('payment_status')) == 'waived':
                return (False, '免收订单与分期收款互斥（免收按整单归档语义处理）')
            # D12①：金额改小 → 重算后净额不得低于已到账（容差 0.01）
            if needs_financials:
                received = get_received_amount(order_id, conn=conn)
                new_net = float(merged.get('actual_received') or 0)
                if received > 0 and new_net < received - PAYMENT_EPSILON:
                    return (False, f'应收净额 {new_net:.2f} 已低于已到账 {received:.2f}，'
                                   '请先核减收款记录再修改金额')

        merged['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 过滤掉非 orders 表的列（如 JOIN 带来的 customer_name、查询时计算的 repeat 两列）
        order_columns = {
            'id', 'customer_id', 'project_name', 'source', 'is_commercial',
            'commission_type', 'current_stage', 'ddl_status',
            'deposit', 'balance', 'platform_fee', 'platform_fee_pct', 'income', 'actual_received',
            'discount_pct', 'discounted_income',  # Spec19 VIP 折扣（快照 + 折后落库）
            'payment_status', 'is_archived',
            'payment_mode',  # Spec 26 D10：收款模式白名单登记（保持性：切换保存后重开不丢）
            'notes', 'custom_color', 'platform_url', 'page_deadline', 'scheduled_start', 'scheduled_end',
            'image_url', 'image_path', 'has_image',
            'completed_at', 'is_overdue',
            'estimated_hours', 'work_hours', 'exclude_hourly',  # P20b 时薪三列（不进 _MONEY_FIELDS，不触发财务重算）
            'stage_flow',  # Spec12 阶段流程快照
            'sort_order', 'created_at', 'updated_at'
        }
        # 未触发任何管线时仅写传入字段（部分更新语义保留）；触发过管线则写全 merged
        payload = {k: v for k, v in merged.items() if k in order_columns} \
            if (needs_financials or needs_ddl) else {k: v for k, v in data.items() if k in order_columns}
        payload['updated_at'] = merged['updated_at']
        payload.pop('id', None)

        set_clause = ', '.join([f"{k} = ?" for k in payload.keys()])
        conn.execute(f"UPDATE orders SET {set_clause} WHERE id = ?",
                     list(payload.values()) + [order_id])

        # D12③：simple→installment 且已收齐 → 同事务自动生成 1 笔初始收款
        # （保数字连续性：否则已结算单切分期后已到账瞬间归零）；paid_at 取
        # 有效排期日，无/非法排期退今日；0 元已结算单无款可搬，跳过并留痕。
        if (mode_switch and target_mode == 'installment'
                and merged.get('payment_status') in get_paid_statuses()):
            amount = round(float(merged.get('actual_received') or 0), 2)
            if amount > 0:
                d = _safe_iso(merged.get('scheduled_end'))
                paid_at = d.isoformat() if d else date.today().isoformat()
                conn.execute(
                    "INSERT INTO order_payments (order_id, paid_at, amount, note) "
                    "VALUES (?, ?, ?, ?)",
                    (order_id, paid_at, amount, '切换分期自动生成（原整单已结算）'))
                logger.info('Spec26 D12③ 订单 %s 切分期：自动生成初始收款 %s ¥%.2f',
                            order_id, paid_at, amount)

        # 客户统计：新旧客户集去重 recalc（修 A5：换客户旧客户也要刷新）
        new_cid = payload.get('customer_id', old_cid)
        for cid in {old_cid, new_cid}:
            if cid:
                recalc_customer_stats(cid, conn=conn)

    # P19-F10：订单更新可能带来 auto-discover 新值，失效缓存
    _invalidate_choices_cache()
    return (True, '')


def recompute_order(order_id: int) -> bool:
    """P17a 按现有字段用最新算法重算并保存派生状态。

    场景：历史订单按录入当时算法落库，阶段算法演进后出现不一致
    （应归档未归档、应逾期未标）。本函数取订单现有字段重跑
    `_calc_financials` → `_auto_calc_ddl_status`
    管线，只回写派生字段（财务/DDL/归档），
    **不改用户手填内容**（阶段、金额、日期、来源等原样保留）。
    归档沿用非破坏性约定（只置 1 从不置 0）。
    P19-F5：读+重算+写+recalc 收拢为单事务。
    P19-F8：复购不再回写（查询时计算，_apply_repeat_for_rows）。
    """
    with transaction() as conn:
        existing = get_order(order_id, conn=conn)
        if not existing:
            return False

        # 以现有字段为输入重跑管线（P19-F9：platform_fee_pct 快照随单落库，手续费按订单自身快照重算）
        data = dict(existing)
        data = _calc_financials(data)
        data = _auto_calc_ddl_status(data)

        # 只回写派生字段，用户手填内容保持不变
        derived = {
            'ddl_status': data.get('ddl_status'),
            'income': float(data.get('income') or 0),
            'discounted_income': float(data.get('discounted_income') or 0),  # Spec19：折后随财务管线重算
            'platform_fee': float(data.get('platform_fee') or 0),
            'actual_received': float(data.get('actual_received') or 0),
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        # 归档非破坏性：仅在算法判定为归档时置 1，从不取消现有归档；
        # completed_at/is_overdue 经管线唯一入口（已归档单保留快照，缺失才回填）
        if int(data.get('is_archived') or 0) == 1:
            derived['is_archived'] = 1
            derived['completed_at'] = data.get('completed_at')
            derived['is_overdue'] = int(data.get('is_overdue') or 0)

        set_clause = ', '.join([f"{k} = ?" for k in derived.keys()])
        conn.execute(f"UPDATE orders SET {set_clause} WHERE id = ?",
                     list(derived.values()) + [order_id])
        if existing.get('customer_id'):
            recalc_customer_stats(existing['customer_id'], conn=conn)
    return True


def delete_order(order_id: int) -> bool:
    """删除订单（P19-F5：删除+客户重算单事务）。
    删除后自动清理无用的类别/阶段/来源颜色配置（cal_commission_*/cal_stage_*/cal_source_*）。
    """
    with transaction() as conn:
        row = conn.execute(
            "SELECT customer_id, commission_type, current_stage, source FROM orders WHERE id = ?",
            (order_id,)
        ).fetchone()
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        if row and row['customer_id']:
            recalc_customer_stats(row['customer_id'], conn=conn)
        # 自动清理：检查类别/阶段/来源是否还有订单使用，无则移除对应颜色配置
        _cleanup_unused_color_settings(conn, row)
    return True


def _cleanup_unused_color_settings(conn, deleted_row) -> None:
    """删除订单后检查三个字段（commission_type/current_stage/source），
    如果某值不再被任何订单使用，则从 settings 表移除对应 cal_* 颜色配置。
    """
    if not deleted_row:
        return
    # (字段名, 颜色前缀, 是否允许清理)
    checks = [
        ('commission_type', 'cal_commission_', True),
        ('current_stage', 'cal_stage_', True),
        ('source', 'cal_source_', True),
    ]
    for field, prefix, _ in checks:
        value = deleted_row[field]
        if not value:
            continue
        still_used = conn.execute(
            f"SELECT COUNT(*) FROM orders WHERE {field} = ?",
            (value,)
        ).fetchone()[0]
        if still_used == 0:
            color_key = f'{prefix}{value}'
            conn.execute("DELETE FROM settings WHERE key = ?", (color_key,))
            logger.info('字段 %s 值「%s」已无订单使用，自动清理颜色配置 %s', field, value, color_key)


# ── Spec 26 分期收款流水（order_payments CRUD + 收齐自动结算，路由归 task-108）──

def list_payments(order_id: int, conn=None) -> list[dict]:
    """收款流水列表（到账日升序，同日按录入顺序）。"""
    c, close = _metric_conn(conn)
    rows = c.execute(
        "SELECT id, order_id, paid_at, amount, note, created_at "
        "FROM order_payments WHERE order_id = ? ORDER BY paid_at ASC, id ASC",
        (order_id,)
    ).fetchall()
    if close:
        c.close()
    return [dict(r) for r in rows]


def get_received_amount(order_id: int, conn=None) -> float:
    """已到账合计（Σ笔，round 2；详情页待收 = actual_received − 本值）。"""
    c, close = _metric_conn(conn)
    row = c.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM order_payments WHERE order_id = ?",
        (order_id,)
    ).fetchone()
    if close:
        c.close()
    return round(row[0], 2)


def _payment_write_ctx(conn):
    """内部：收款写操作事务上下文 —— 传入连接则复用（不 commit/close），否则自开。"""
    return nullcontext(conn) if conn is not None else transaction()


def _settle_if_received_full(order: dict, received: float, conn) -> bool:
    """收齐自动结算（spec §3.4 状态机）：Σ笔 ≥ actual_received−ε 且当前'未收款'
    → 经 update_order 置'已结算'（payment_status ∈ _DDL_TRIGGER_FIELDS，内部走
    _auto_calc_ddl_status 唯一归档入口 + recalc_customer_stats，不重复算客户）。
    他态不自动改写 —— 只进不退（D4）。order 为写流水前读取的订单快照
    （payment_status 判定用旧值）。返回是否触发了结算。
    """
    target = float(order.get('actual_received') or 0)
    if enum_code_or_raw('payment', order.get('payment_status')) == 'unpaid' and received >= target - PAYMENT_EPSILON:
        update_order(order['id'], {'payment_status': 'settled'}, conn=conn)
        return True
    return False


def add_payment(order_id: int, data: dict, conn=None) -> tuple:
    """新增一笔收款（Spec 26 §3.4 单事务状态机）。

    流程：订单存在 → 笔数 ≤ MAX_PAYMENTS_PER_ORDER → 模拟 Σ笔 ≤
    discounted_income+ε（先校验后写，with 内 return 不留半成品提交）→
    INSERT 流水 → 收齐判定（_settle_if_received_full）。返回 (True, 消息) /
    (False, 原因)。字段完整性校验在路由层 PaymentRecord（task-108）。
    """
    with _payment_write_ctx(conn) as c:
        order = get_order(order_id, conn=c)
        if not order:
            return (False, '订单不存在')
        cnt = c.execute(
            "SELECT COUNT(*) FROM order_payments WHERE order_id = ?", (order_id,)
        ).fetchone()[0]
        if cnt >= MAX_PAYMENTS_PER_ORDER:
            return (False, f'单笔订单收款记录最多 {MAX_PAYMENTS_PER_ORDER} 笔')
        try:
            amount = round(float(data.get('amount') or 0), 2)
        except (TypeError, ValueError):
            return (False, '收款金额无效')
        if not amount >= 0:  # 负值/NaN 落入 False 分支（not (NaN >= 0) 为 True）
            return (False, '收款金额无效')
        new_total = round(get_received_amount(order_id, conn=c) + amount, 2)
        cap = float(order.get('discounted_income') or 0)
        if new_total > cap + PAYMENT_EPSILON:
            return (False, f'累计收款将达 {new_total:.2f}，超过折后应收 {cap:.2f}')
        c.execute(
            "INSERT INTO order_payments (order_id, paid_at, amount, note) VALUES (?, ?, ?, ?)",
            (order_id, data.get('paid_at'), amount, data.get('note')))
        settled = _settle_if_received_full(order, new_total, c)
    return (True, '已收齐，自动结算' if settled else '')


def update_payment(payment_id: int, data: dict, conn=None) -> tuple:
    """修改一笔收款（同 add_payment 事务模型：先校验模拟 Σ → UPDATE → 收齐判定）。"""
    with _payment_write_ctx(conn) as c:
        row = c.execute(
            "SELECT * FROM order_payments WHERE id = ?", (payment_id,)
        ).fetchone()
        if not row:
            return (False, '收款记录不存在')
        order = get_order(row['order_id'], conn=c)
        if not order:
            return (False, '订单不存在')
        if 'amount' in data:
            try:
                new_amount = round(float(data['amount'] or 0), 2)
            except (TypeError, ValueError):
                return (False, '收款金额无效')
            if not new_amount >= 0:
                return (False, '收款金额无效')
        else:
            new_amount = round(float(row['amount']), 2)
        # 模拟 Σ：其余笔合计 + 新本笔（替换口径）
        others = round(get_received_amount(row['order_id'], conn=c) - float(row['amount']), 2)
        new_total = round(others + new_amount, 2)
        cap = float(order.get('discounted_income') or 0)
        if new_total > cap + PAYMENT_EPSILON:
            return (False, f'累计收款将达 {new_total:.2f}，超过折后应收 {cap:.2f}')
        c.execute(
            "UPDATE order_payments SET paid_at = ?, amount = ?, note = ? WHERE id = ?",
            (data.get('paid_at', row['paid_at']), new_amount,
             data.get('note', row['note']), payment_id))
        settled = _settle_if_received_full(order, new_total, c)
    return (True, '已收齐，自动结算' if settled else '')


def delete_payment(payment_id: int, conn=None) -> tuple:
    """删除一笔收款（D4 只进不退：不回退 payment_status、不撤销归档；
    Σ笔与状态的不一致由详情页提示兜底，阶段 3 实现）。"""
    with _payment_write_ctx(conn) as c:
        row = c.execute(
            "SELECT id FROM order_payments WHERE id = ?", (payment_id,)
        ).fetchone()
        if not row:
            return (False, '收款记录不存在')
        c.execute("DELETE FROM order_payments WHERE id = ?", (payment_id,))
    return (True, '')


# ═══════════════════════════════════════════════════════════
# 订单多图（P15d order_images 一对多）
# ═══════════════════════════════════════════════════════════

def get_order_images(order_id: int) -> list[dict]:
    """获取订单全部图片，按 sort_order/id 升序。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM order_images WHERE order_id = ? ORDER BY sort_order, id",
        (order_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_order_image(order_id: int, image_url: str, image_path: str) -> int:
    """追加一条订单图片记录，sort_order 取当前最大值+1，返回新记录 id。"""
    conn = get_db()
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM order_images WHERE order_id = ?",
        (order_id,)).fetchone()
    next_sort = row['next'] if row else 0
    cur = conn.execute(
        "INSERT INTO order_images (order_id, image_url, image_path, sort_order) VALUES (?, ?, ?, ?)",
        (order_id, image_url, image_path, next_sort))
    conn.commit()
    image_id = cur.lastrowid
    conn.close()
    return image_id


def get_order_image(image_id: int) -> dict | None:
    """按 id 获取单条订单图片记录。"""
    conn = get_db()
    row = conn.execute("SELECT * FROM order_images WHERE id = ?", (image_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def remove_order_image(image_id: int) -> dict | None:
    """删除一条订单图片记录，返回被删记录（供调用方清理磁盘文件）。"""
    conn = get_db()
    row = conn.execute("SELECT * FROM order_images WHERE id = ?", (image_id,)).fetchone()
    if row:
        conn.execute("DELETE FROM order_images WHERE id = ?", (image_id,))
        conn.commit()
    conn.close()
    return dict(row) if row else None


def repair_image_consistency() -> dict:
    """图片引用一致性自愈（P20-F14）：启动时调用，幂等。

    场景：用户绕过应用直接删 uploads 下文件，或历史脏标记残留时，
    画廊仅凭 orders 封面三列出图，会展示失效链接。此处：
    1. 删除 order_images 中文件已缺失（或 url/path 双空）的记录；
    2. 封面候选订单重算封面三列——多图表有有效记录取首图；
       无记录则仅当自身 url/path 仍指向有效目标时保留，否则清空。
    返回 {'removed_images', 'cleared_covers', 'fixed_covers'} 计数供日志。
    """
    upload_dir = os.path.join(data_dir(), 'uploads')

    def _url_file_exists(url: str) -> bool:
        if url and url.startswith('/uploads/'):
            return os.path.isfile(os.path.join(upload_dir, url[len('/uploads/'):]))
        return False

    stats = {'removed_images': 0, 'cleared_covers': 0, 'fixed_covers': 0}
    with transaction() as conn:
        # 1. 清理失效 order_images 记录（path 非空但文件缺失；或 url/path 双空）
        dead_ids, affected = [], set()
        for r in conn.execute("SELECT id, order_id, image_url, image_path FROM order_images").fetchall():
            url, path = r['image_url'] or '', r['image_path'] or ''
            if path:
                if not os.path.isfile(os.path.join(upload_dir, path)):
                    dead_ids.append(r['id'])
                    affected.add(r['order_id'])
            elif not url:
                dead_ids.append(r['id'])
                affected.add(r['order_id'])
            # path 空但 url 非空：无法校验（外链或旧数据），保留
        for iid in dead_ids:
            conn.execute("DELETE FROM order_images WHERE id = ?", (iid,))
        stats['removed_images'] = len(dead_ids)

        # 2. 封面候选订单重算（含被删记录波及的订单）
        candidates = {r['id'] for r in conn.execute(
            "SELECT id FROM orders WHERE has_image = 1 "
            "OR (image_url IS NOT NULL AND image_url != '') "
            "OR (image_path IS NOT NULL AND image_path != '')").fetchall()}
        candidates |= affected
        for oid in candidates:
            first = conn.execute(
                "SELECT image_url, image_path FROM order_images WHERE order_id = ? "
                "ORDER BY sort_order, id LIMIT 1", (oid,)).fetchone()
            if first:
                conn.execute(
                    "UPDATE orders SET image_url = ?, image_path = ?, has_image = 1 WHERE id = ?",
                    (first['image_url'], first['image_path'], oid))
                stats['fixed_covers'] += 1
                continue
            # 多图表无记录：自身封面仍指向有效目标才保留，否则清空三列
            row = conn.execute(
                "SELECT image_url, image_path FROM orders WHERE id = ?", (oid,)).fetchone()
            if not row:
                continue
            url, path = row['image_url'] or '', row['image_path'] or ''
            keep = False
            if path and os.path.isfile(os.path.join(upload_dir, path)):
                keep = True
            elif url and not url.startswith('/uploads/'):
                keep = True  # 外部链接，无法校验，信任保留
            elif _url_file_exists(url):
                keep = True
            if keep:
                # 封面有效但 has_image 标记错误的，顺手纠正
                conn.execute("UPDATE orders SET has_image = 1 WHERE id = ? AND has_image != 1", (oid,))
                continue
            conn.execute(
                "UPDATE orders SET image_url = NULL, image_path = NULL, has_image = 0 WHERE id = ?",
                (oid,))
            stats['cleared_covers'] += 1
    if stats['removed_images'] or stats['cleared_covers'] or stats['fixed_covers']:
        logger.info('repair_image_consistency: %s', stats)
    return stats


def archive_order(order_id: int, completed_at: str | None = None, is_overdue: int = 0) -> bool:
    """切换归档状态。

    归档时（is_archived 0→1）记录 completed_at：
        - completed_at 为 None 时用当前时间（区间内完成 / 提前确认“是” / 逾期确认“是”）
        - 显式传入时采用该值（逾期“旧记录导入”按预设截止日期）
    is_overdue: 归档时是否标记为逾期工作（P15b）。
    取消归档时清空 completed_at 与 is_overdue。
    """
    with transaction() as conn:
        row = conn.execute("SELECT is_archived FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row:
            new_val = 0 if row['is_archived'] else 1
            # P19-F1：内部对齐到唯一入口 set_archived；确认链传入的 completed_at/is_overdue 原样生效
            set_archived(conn, order_id, new_val == 1, completed_at=completed_at, is_overdue=is_overdue)
    return True


def set_order_archived(order_id: int, archived: bool) -> bool:
    """直接归档开关（批量操作用）：经 set_archived 唯一入口，自动写/清 completed_at 与 is_overdue。"""
    with transaction() as conn:
        set_archived(conn, order_id, archived)
    return True


def _apply_days_remaining_for_rows(rows: list[dict]) -> list[dict]:
    """查询时计算 days_remaining：距 scheduled_end 的天数差。

    终态（完成/退单）和已归档订单 → None；无 scheduled_end 或解析失败 → None。
    """
    from datetime import date as dt_date
    today = dt_date.today()
    terminal = get_terminal_stages()
    for r in rows:
        stage = r.get('current_stage', '')
        if stage in terminal or r.get('is_archived'):
            r['days_remaining'] = None
            continue
        end_str = r.get('scheduled_end', '')
        if not end_str or not end_str.strip():
            r['days_remaining'] = None
            continue
        try:
            end_date = dt_date.fromisoformat(end_str.strip()[:10])
            r['days_remaining'] = (end_date - today).days
        except (ValueError, TypeError):
            r['days_remaining'] = None
    return rows


def _refresh_ddl_for_rows(rows: list[dict]) -> list[dict]:
    """读取时刷新 DDL 状态（处理时间推移导致的逾期）"""
    terminal = get_terminal_stages()  # P19-F2：终态元数据（取代位置魔法）
    for r in rows:
        if r.get('current_stage') in terminal:
            continue  # 终态不变
        r = _auto_calc_ddl_status(r)
    return rows


def _apply_repeat_for_rows(rows: list[dict], conn=None) -> list[dict]:
    """P19-F8 复购标记查询时计算（派生视图化，取代写入时落库）。

    口径（与统计口径字典一致）：repeat_count = 同 customer_id 他单数（排除退单终态、
    排除本单）；is_repeat = 1 if repeat_count > 0 else 0；customer_id 为空 → 0/0。
    实现：批量一次 GROUP BY 查询 + Python 覆盖（与 _refresh_ddl_for_rows 同构）；
    不走 SELECT o.* + 同名列覆盖——sqlite3.Row 重复列名取第一列，会读到落库旧值。
    落库 is_repeat/repeat_count 两列暂留不删（spec 11 §7，两个版本后评估删除），
    仅作旧版本回滚兜底；本函数为全部读取路径的唯一事实来源。
    """
    if not rows:
        return rows
    own = conn is None
    if own:
        conn = get_db()
    cids = {r.get('customer_id') for r in rows if r.get('customer_id')}
    counts: dict = {}
    if cids:
        placeholders = ','.join('?' for _ in cids)
        q = (f"SELECT customer_id, COUNT(*) FROM orders "
             f"WHERE customer_id IN ({placeholders}) AND current_stage != ? "
             f"GROUP BY customer_id")
        for cid, cnt in conn.execute(q, [*cids, get_refund_stage()]).fetchall():
            counts[cid] = cnt
    if own:
        conn.close()
    for r in rows:
        cid = r.get('customer_id')
        cnt = counts.get(cid, 0) if cid else 0
        # counts 统计同客户非退单订单数（含本单）；本单非退单时减 1 排除本单
        if cid and not is_refund_stage(r.get('current_stage') or ''):
            cnt -= 1
        r['repeat_count'] = cnt
        r['is_repeat'] = 1 if cnt > 0 else 0
    # 查询时计算剩余天数（与复购标记同路径注入，覆盖所有读取入口）
    _apply_days_remaining_for_rows(rows)
    return rows


# ═══════════════════════════════════════════════════════════
# 查询
# ═══════════════════════════════════════════════════════════

# P13b G3 列排序：排序键公式
#   数值列直排；文本/日期列 NULL 排最后；来源/阶段/收款按注册表顺序 CASE WHEN 映射
#   已知限制：SQLite 中文按 Unicode 码点排序，不做拼音排序
_ORDER_SORT_COLUMNS = {
    'id': ('num', 'o.id'),
    'income': ('num', 'o.income'),
    'actual_received': ('num', 'o.actual_received'),
    'customer': ('text', 'c.name'),
    'project_name': ('text', 'o.project_name'),
    'commission_type': ('text', 'o.commission_type'),
    'source': ('choice', 'source', 'o.source'),
    'stage': ('choice', 'stage', 'o.current_stage'),
    'payment': ('choice', 'payment_status', 'o.payment_status'),
    'ddl': ('date', 'o.scheduled_end'),
    'scheduled': ('date', 'o.scheduled_start'),
}

_ORDER_SORT_DEFAULT = "o.sort_order ASC, o.scheduled_start ASC"


def _order_sort_clause(sort: str, direction: str) -> tuple:
    """构造订单 ORDER BY 子句，返回 (clause, extra_params)。"""
    if not sort or sort not in _ORDER_SORT_COLUMNS or direction not in ('asc', 'desc'):
        return _ORDER_SORT_DEFAULT, []
    spec = _ORDER_SORT_COLUMNS[sort]
    dir_sql = 'DESC' if direction == 'desc' else 'ASC'
    kind = spec[0]
    if kind == 'num':
        return f"{spec[1]} {dir_sql}, {_ORDER_SORT_DEFAULT}", []
    if kind in ('text', 'date'):
        col = spec[1]
        return f"{col} IS NULL ASC, {col} {dir_sql}, {_ORDER_SORT_DEFAULT}", []
    # choice：注册表顺序 CASE WHEN 映射，未匹配/NULL 排最后
    _, choice_type, col = spec
    choices = get_choices(choice_type)
    cases = ' '.join(f"WHEN ? THEN {i}" for i in range(len(choices)))
    expr = f"CASE {col} {cases} ELSE {len(choices)} END"
    return f"{col} IS NULL ASC, {expr} {dir_sql}, {_ORDER_SORT_DEFAULT}", list(choices)


def list_orders(filters: dict | None = None) -> list[dict]:
    """查询订单列表，支持多种筛选 + 分页"""
    filters = filters or {}
    conn = get_db()
    where = []
    params = []

    if filters.get('stage'):
        where.append("o.current_stage = ?")
        params.append(filters['stage'])
    if filters.get('source'):
        where.append("o.source = ?")
        params.append(filters['source'])
    if filters.get('commission_type'):
        where.append("o.commission_type = ?")
        params.append(filters['commission_type'])
    if filters.get('status'):
        where.append("o.ddl_status = ?")
        params.append(filters['status'])
    if filters.get('payment_status'):
        where.append("o.payment_status = ?")
        params.append(filters['payment_status'])
    if filters.get('search'):
        where.append("(o.project_name LIKE ? OR c.name LIKE ?)")
        params.extend([f"%{filters['search']}%", f"%{filters['search']}%"])
    # P16e：按订单结束时间（scheduled_end）范围筛选，任一端为空按单边开区间
    if filters.get('end_from'):
        where.append("o.scheduled_end >= ?")
        params.append(filters['end_from'])
    if filters.get('end_to'):
        where.append("o.scheduled_end <= ?")
        params.append(filters['end_to'])
    if filters.get('customer_id'):
        where.append("o.customer_id = ?")
        params.append(filters['customer_id'])
    if filters.get('archived') is not None:
        where.append("o.is_archived = ?")
        params.append(1 if filters['archived'] else 0)
    else:
        where.append("o.is_archived = 0")  # 默认只看活跃
    if filters.get('week') == 'current':
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        where.append("o.scheduled_start <= ? AND o.scheduled_end >= ?")
        params.extend([sunday.isoformat(), monday.isoformat()])

    where_clause = ' AND '.join(where)
    base_sql = f"""FROM orders o
              LEFT JOIN customers c ON o.customer_id = c.id
              WHERE {where_clause}"""

    # 分页参数（P19-F11：per_page<=0 表示全量——看板/主页本周等无分页 UI 的视图
    # 防默认 30 静默截断丢单；SQLite LIMIT -1 = 无限制）
    page = int(filters.get('page', 1))
    per_page = int(filters.get('per_page', 30))
    if per_page <= 0:
        per_page = -1
        offset = 0
    else:
        offset = (page - 1) * per_page

    # 查询总数
    count_row = conn.execute(f"SELECT COUNT(*) {base_sql}", params).fetchone()
    total = count_row[0] if count_row else 0

    # 查询当前页（P13b G3：sort/dir 三态排序，默认 sort_order ASC, scheduled_start ASC）
    order_clause, order_params = _order_sort_clause(filters.get('sort'), filters.get('dir'))
    # P15c：本周排单默认按 DDL（scheduled_end）升序，NULL 排最后（未显式指定 sort 时）
    if filters.get('week') == 'current' and not filters.get('sort'):
        order_clause, order_params = "o.scheduled_end IS NULL ASC, o.scheduled_end ASC, o.sort_order ASC", []
    sql = f"""SELECT o.*, c.name as customer_name {base_sql}
              ORDER BY {order_clause}
              LIMIT ? OFFSET ?"""
    rows = conn.execute(sql, params + order_params + [per_page, offset]).fetchall()
    result = _apply_repeat_for_rows([dict(r) for r in rows], conn=conn)  # P19-F8 查询时计算复购
    conn.close()

    result = _refresh_ddl_for_rows(result)
    # 将 total 挂在第一个结果的特殊标记上，由调用方提取
    if result:
        result[0]['_total'] = total
        result[0]['_page'] = page
        result[0]['_per_page'] = per_page
    # 空结果直接返回空列表（不再包装成假订单）
    return result


def get_orders_for_gantt() -> list[dict]:
    """时间线全量：所有带排期的订单（含已归档的已完成/退单、未开始），返回甘特图格式。

    仅要求 scheduled_start/scheduled_end 齐全（时间线条形需要起止锚点）；
    不再按 is_archived 过滤 —— 已完成/退单会静默归档，过滤会导致它们从时间线消失。
    状态维度改由前端「显示全部 / 仅进行中」开关控制。
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT o.*, c.name as customer_name
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE o.scheduled_start IS NOT NULL
          AND o.scheduled_end IS NOT NULL
        ORDER BY o.scheduled_start ASC
    """).fetchall()
    conn.close()

    stage_progress = {name: m['progress'] for name, m in STAGE_META.items()}  # P19-F2：进度取自元数据
    stage_class = {
        'pending': 'stage-pending', 'sketch': 'stage-sketch',
        'lineart': 'stage-lineart', 'detail': 'stage-detail',
        'finish': 'stage-finish', 'completed': 'stage-completed',
        'cancelled': 'stage-cancelled'
    }

    result = []
    for r in rows:
        r = dict(r)
        _stage_code = enum_code_or_raw('stage', r['current_stage'])
        classes = [stage_class.get(_stage_code, '')]
        if is_overdue_ddl(r['ddl_status']):
            classes.append('overdue')
        if r['is_archived']:
            classes.append('archived')

        result.append({
            'id': str(r['id']),
            'name': f"{r['customer_name'] or ''}-{r['project_name']}" if r['customer_name'] else r['project_name'],
            'start': r['scheduled_start'] or '',
            'end': r['scheduled_end'] or '',
            'progress': stage_progress.get(_stage_code, 0),
            'dependencies': '',
            'custom_class': ' '.join(c for c in classes if c),
            'current_stage': enum_label('stage', r['current_stage']),
            'stage_code': _stage_code,
            'is_archived': r['is_archived'],
        })

    return result


# 日历着色调色板
CALENDAR_PALETTES = {
    'stage': {
        'pending': '#b0b0aa', 'sketch': '#6b8eb8', 'lineart': '#5b9e9e',
        'detail': '#c49b4a', 'finish': '#8b7ec8', 'completed': '#5b9e6b',
        'cancelled': '#c4756b'
    },
    'source': {
        # Validated categorical — slots 1-4
        'mihuashi': '#2a78d6', 'bili': '#1baf7a',
        'wechat': '#eda100', 'other': '#008300'
    },
    'ddl': {
        # Status-adjacent — good/warning/critical + muted
        'normal': '#0ca30c', 'due': '#fab219',
        'overdue': '#d03b3b', 'done': '#898781'
    },
    'payment': {
        'deposit_paid': '#2a78d6', 'unpaid': '#fab219', 'settled': '#0ca30c',
        'debt': '#d03b3b', 'waived': '#008300'
    },
    'commission': {
        # ≥8 categories → fold into "Other" per dataviz rule
        'cmt_001': '#2a78d6', 'cmt_002': '#1baf7a', 'cmt_003': '#eda100',
        'cmt_004': '#008300', 'cmt_005': '#4a3aa7', 'cmt_006': '#e34948',
        'cmt_007': '#e87ba4', 'cmt_008': '#eb6834'
    },
}

# 模式 → 取值字段
CALENDAR_COLOR_FIELDS = {
    'stage': 'current_stage',
    'source': 'source',
    'ddl': 'ddl_status',
    'payment': 'payment_status',
    'commission': 'commission_type',
}


def get_merged_palette(color_mode: str) -> dict:
    """#43：合并默认调色板 + 用户自定义设置（键 `cal_<mode>_<标签>`），返回 {标签: 颜色}。
    日历着色与收入页品类甜甜圈共用此一套配置（单一数据源，跳页同色）。
    Spec12：stage 模式下自动发现 stage_flows 中的自定义阶段名，未配置 cal_stage_* 时
    从固定分类色板中轮询分配，保证自定义阶段有区分度。
    """
    palette = dict(CALENDAR_PALETTES.get(color_mode, CALENDAR_PALETTES['stage']))
    prefix = f'cal_{color_mode}_'
    try:
        for k, v in get_all_settings().items():
            if k.startswith(prefix):
                palette[k[len(prefix):]] = v
    except Exception as e:
        logging.error(f'get_merged_palette 读取设置失败 (mode={color_mode}): {e}')
    # Spec12：为 stage_flows 中的自定义阶段名补充回退色
    if color_mode == 'stage':
        _auto_stage_fallback_colors(palette)
    return palette


# Spec12：自定义阶段回退色板（categorical，避免与已有 --stage-* 冲突）
_STAGE_FALLBACK_COLORS = [
    '#6366f1', '#06b6d4', '#f59e0b', '#ec4899', '#14b8a6',
    '#8b5cf6', '#f97316', '#84cc16', '#e11d48', '#0ea5e9',
]


def _auto_stage_fallback_colors(palette: dict) -> None:
    """为 stage_flows 中尚未在 palette 中出现的阶段名分配回退色。"""
    try:
        flows = get_stage_flows()
    except Exception:
        return
    idx = 0
    for flow in flows:
        for s in flow.get('stages', []):
            name = s.get('name', '')
            if name and name not in palette:
                palette[name] = _STAGE_FALLBACK_COLORS[idx % len(_STAGE_FALLBACK_COLORS)]
                idx += 1


def _calendar_filter_sql(filters: dict | None) -> tuple:
    """P13b F1 级联筛选：阶段/来源/类别/客户/收款状态 → (where 片段列表, params)
    Spec33 卡 166：入参先归一（旧中文书签 ?stage=完成 与 ?stage=completed 同结果）。"""
    where, params = [], []
    filters = filters or {}
    if filters.get('stage'):
        where.append("o.current_stage = ?")
        params.append(enum_code_or_raw('stage', filters['stage']))
    if filters.get('source'):
        where.append("o.source = ?")
        params.append(resolve_choice_code('source', filters['source']) or filters['source'])
    if filters.get('commission_type'):
        where.append("o.commission_type = ?")
        params.append(resolve_choice_code('commission_type', filters['commission_type']) or filters['commission_type'])
    if filters.get('customer_id'):
        where.append("o.customer_id = ?")
        params.append(filters['customer_id'])
    if filters.get('payment_status'):
        where.append("o.payment_status = ?")
        params.append(enum_code_or_raw('payment', filters['payment_status']))
    return where, params


def _contrast_text_color(bg_hex: str) -> str:
    """根据背景色 hex 返回对比文字色（WCAG 相对亮度公式）。

    亮底 → '#1a1a1a'（深色字），暗底 → '#fff'（白色字）。
    解析失败兑底返回 '#1a1a1a'。Spec 28 D4。
    """
    try:
        h = bg_hex.lstrip('#')
        if len(h) != 6:
            return '#1a1a1a'
        r, g, b = int(h[:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
        # sRGB → 线性（gamma 2.2 近似）
        r_lin = r ** 2.2
        g_lin = g ** 2.2
        b_lin = b ** 2.2
        # 相对亮度
        lum = 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin
        # 白/黑对比度比值
        contrast_white = (1.0 + 0.05) / (lum + 0.05)
        contrast_black = (lum + 0.05) / (0.0 + 0.05)
        return '#fff' if contrast_white >= contrast_black else '#1a1a1a'
    except (ValueError, TypeError):
        return '#1a1a1a'


def get_orders_for_calendar(color_mode: str = 'source', filters: dict | None = None,
                            show_archived: bool = False) -> list[dict]:
    """有日期的订单，返回 FullCalendar 事件格式。
    color_mode: stage | source | ddl | payment | commission
    优先级：custom_color > settings 自定义 > 默认调色板 > 灰色
    filters: P13b F1 级联筛选（阶段/来源/类别/客户/收款状态）
    show_archived: P16d 日历归档显隐开关。False（默认）仅取 is_archived=0；
                   True 纳入已归档（已完成+退单）项目。
    """
    # 合并默认调色板 + 用户自定义设置（与收入页品类着色同源，见 get_merged_palette）
    base_palette = get_merged_palette(color_mode)

    field = CALENDAR_COLOR_FIELDS.get(color_mode, 'current_stage')
    default_color = '#b0b0aa'

    conn = get_db()
    filter_where, filter_params = _calendar_filter_sql(filters)
    extra = (' AND ' + ' AND '.join(filter_where)) if filter_where else ''
    # P16d: 默认隐藏已归档；开关开启时不限制 is_archived
    archived_where = '' if show_archived else 'o.is_archived = 0 AND '
    rows = conn.execute(f"""
        SELECT o.*, c.name as customer_name
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE {archived_where}o.scheduled_start IS NOT NULL{extra}
        ORDER BY o.scheduled_start ASC
    """, filter_params).fetchall()
    conn.close()

    result = []
    for r in rows:
        r = dict(r)
        end = r['scheduled_end'] or r['scheduled_start']
        if end and 'T' not in str(end):
            # 纯日期格式：FullCalendar 全天事件 end +1 天（exclusive）
            from datetime import datetime as dt
            try:
                end_dt = dt.strptime(end, '%Y-%m-%d')
                end = (end_dt + timedelta(days=1)).strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                pass
        # datetime 格式（含 T）：FullCalendar 定时事件，end 直接使用原始值

        # 颜色优先级：custom_color > settings palette > default
        color = r.get('custom_color') or base_palette.get(r.get(field, ''), default_color)

        result.append({
            'id': str(r['id']),
            'title': f"{r['customer_name'] or ''}-{r['project_name']}" if r['customer_name'] else r['project_name'],
            'start': r['scheduled_start'],
            'end': end,
            'backgroundColor': color,
            'textColor': _contrast_text_color(color),
            'borderColor': color,
            'extendedProps': {
                'customer_name': r['customer_name'],
                'stage': enum_label('stage', r['current_stage']),
                'source': choice_label('source', r['source']) if r['source'] else r['source'],
                'ddl': enum_label('ddl', r['ddl_status']),
                'payment': enum_label('payment', r['payment_status']),
                'commission': choice_label('commission_type', r['commission_type']) if r['commission_type'] else r['commission_type'],
                'stage_code': enum_code_or_raw('stage', r['current_stage']),
                'source_code': resolve_choice_code('source', r['source']) or r['source'],
                'ddl_code': enum_code_or_raw('ddl', r['ddl_status']),
                'payment_code': enum_code_or_raw('payment', r['payment_status']),
                'commission_code': resolve_choice_code('commission_type', r['commission_type']) or r['commission_type'],
                'income': r['income'],
                'overdue': is_overdue_ddl(r['ddl_status']),
                'custom_color': r.get('custom_color'),
                'color_mode': color_mode,
            }
        })
    return result


def set_order_custom_color(order_id: int, color: str | None) -> bool:
    """设置订单的自定义颜色（空字符串 = 清除）"""
    conn = get_db()
    conn.execute("UPDATE orders SET custom_color = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                 (color if color else None, order_id))
    conn.commit()
    conn.close()
    return True


def get_unscheduled_orders(filters: dict | None = None, show_archived: bool = False) -> list[dict]:
    """scheduled_start IS NULL 的订单（P13b F1：支持级联筛选）

    show_archived: P16d 与日历归档开关同步。False（默认）仅取 is_archived=0；
                   True 纳入已归档的未排期项。
    """
    conn = get_db()
    filter_where, filter_params = _calendar_filter_sql(filters)
    extra = (' AND ' + ' AND '.join(filter_where)) if filter_where else ''
    archived_where = '' if show_archived else 'o.is_archived = 0 AND '
    rows = conn.execute(f"""
        SELECT o.*, c.name as customer_name
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE {archived_where}o.scheduled_start IS NULL{extra}
        ORDER BY o.sort_order ASC, o.created_at DESC
    """, filter_params).fetchall()
    conn.close()
    return _apply_days_remaining_for_rows([dict(r) for r in rows])


def get_overdue_orders() -> list[dict]:
    """逾期订单列表 — P19-F3 起改调口径字典 metric_overdue_orders（卡片/明细同源）"""
    return metric_overdue_orders()


def get_all_orders() -> list[dict]:
    """全部订单（含已归档）+ 客户名，按 sort_order、id 排序，供导出用。

    不复用 list_orders：后者默认只取活跃订单（archived=0），且对非终态订单
    做 _refresh_ddl_for_rows 内存改写 ddl_status；导出快照需取库内原值、含归档。
    复购标记查询时计算（P19-F8），不落库旧值。
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT o.*, c.name as customer_name
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        ORDER BY o.sort_order ASC, o.id ASC
    """).fetchall()
    result = _apply_repeat_for_rows([dict(r) for r in rows], conn=conn)
    conn.close()
    return result


def list_feed_orders() -> list[dict]:
    """Spec20 日历订阅：未归档订单（含客户名），供 _build_ics() 生成 VEVENT。

    D6 口径：仅 is_archived=0；归档/删除 → 自动从订阅消失。
    不做 DDL 刷新/复购计算等读时加工——feed 只需库内原值。
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT o.id, o.project_name, o.source, o.current_stage, o.actual_received,
               o.scheduled_start, o.scheduled_end, o.page_deadline,
               o.notes, o.updated_at, c.name AS customer_name
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE o.is_archived = 0
        ORDER BY o.id ASC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_gallery_page(filters: dict | None = None, offset: int = 0, limit: int = 24) -> tuple[list[dict], int]:
    """画廊分页查询（P13c）：返回 (当前批次, 总数)。

    筛选参数：stage / source / customer（客户名称）/ type（类别）。
    排序保持 updated_at DESC，与旧版全量查询一致。
    """
    # P20-F14：画廊出图完全依赖 image_url；has_image=1 但 url 为空的脏行必然破图，不再放行
    where = ["(o.image_url IS NOT NULL AND o.image_url != '')"]
    params: list = []
    if filters:
        if filters.get('stage'):
            where.append("o.current_stage = ?")
            params.append(filters['stage'])
        if filters.get('source'):
            where.append("o.source = ?")
            params.append(filters['source'])
        if filters.get('customer'):
            where.append("c.name = ?")
            params.append(filters['customer'])
        if filters.get('type'):
            where.append("o.commission_type = ?")
            params.append(filters['type'])
    where_sql = " AND ".join(where)
    conn = get_db()
    total = conn.execute(
        f"""SELECT COUNT(*) FROM orders o
            LEFT JOIN customers c ON o.customer_id = c.id
            WHERE {where_sql}""",
        params
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT o.id, o.project_name, o.current_stage, o.source, o.commission_type,
                   o.income, o.image_url, o.image_path, c.name as customer_name
            FROM orders o
            LEFT JOIN customers c ON o.customer_id = c.id
            WHERE {where_sql}
            ORDER BY o.updated_at DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset]
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], total


# ═══════════════════════════════════════════════════════════
# 批量操作
# ═══════════════════════════════════════════════════════════


def update_stage(order_id: int, new_stage: str) -> bool:
    """更新订单阶段（P19-F5：不再绕道 update_order；读+DDL 重算+条件归档 单事务，拖拽 ≤2 连接）。

    阶段变化不改金额/客户，无需 recalc 客户统计；归档经管线内部唯一入口。"""
    with transaction() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return False
        data = dict(row)
        data['current_stage'] = new_stage
        data = _auto_calc_ddl_status(data)  # DDL + 「终态+已结算/免收」自动归档
        conn.execute(
            """UPDATE orders SET current_stage = ?, ddl_status = ?, is_archived = ?,
               completed_at = ?, is_overdue = ?, updated_at = datetime('now','localtime')
               WHERE id = ?""",
            (new_stage, data.get('ddl_status'), int(data.get('is_archived') or 0),
             data.get('completed_at'), int(data.get('is_overdue') or 0), order_id))
    return True


def reschedule_order(order_id: int, start: str, end: str) -> bool:
    """更新订单排期（P19-F5：日期+DDL 重算单事务；不触发归档字段，保持旧语义）。"""
    with transaction() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return False
        data = dict(row)
        data['scheduled_start'] = start
        data['scheduled_end'] = end
        data = _auto_calc_ddl_status(data)
        conn.execute(
            """UPDATE orders SET scheduled_start = ?, scheduled_end = ?, ddl_status = ?,
               updated_at = datetime('now','localtime') WHERE id = ?""",
            (start, end, data.get('ddl_status'), order_id))
    return True


# ═══════════════════════════════════════════════════════════
# 批量操作（P19-F5：逐条多事务 → 整批单事务）
# ═══════════════════════════════════════════════════════════

def batch_update_stage(order_ids: list[int], new_stage: str) -> dict:
    """批量改阶段（Spec12 快照内校验）：整批单事务，每单走 DDL+条件归档管线。

    返回 dict：
    - count: 成功数
    - skipped: 跳过数（该单快照不含目标阶段且非退单）
    - skip_ids: 被跳过的订单 ID 列表（供 Toast 提示）
    """
    count, skipped, skip_ids = 0, 0, []
    refund = get_refund_stage()
    with transaction() as conn:
        for oid in order_ids:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
            if not row:
                continue
            # Spec12：本单快照 + 退单 才允许；快照外阶段跳过并记录
            allowed = set(get_order_stage_names(dict(row))) | {refund}
            if new_stage not in allowed:
                skipped += 1
                skip_ids.append(oid)
                continue
            data = dict(row)
            data['current_stage'] = new_stage
            data = _auto_calc_ddl_status(data)
            conn.execute(
                """UPDATE orders SET current_stage = ?, ddl_status = ?, is_archived = ?,
                   completed_at = ?, is_overdue = ?, updated_at = datetime('now','localtime')
                   WHERE id = ?""",
                (new_stage, data.get('ddl_status'), int(data.get('is_archived') or 0),
                 data.get('completed_at'), int(data.get('is_overdue') or 0), oid))
            count += 1
    return {'count': count, 'skipped': skipped, 'skip_ids': skip_ids}


def batch_set_archived(order_ids: list[int], archived: bool) -> int:
    """批量归档/取消归档：整批单事务，全部经 set_archived 唯一入口。"""
    count = 0
    with transaction() as conn:
        for oid in order_ids:
            row = conn.execute("SELECT id FROM orders WHERE id = ?", (oid,)).fetchone()
            if not row:
                continue
            set_archived(conn, oid, archived)
            count += 1
    return count


def batch_update_field(order_ids: list[int], updates: dict) -> int:
    """批量字段更新（source/payment_status/ddl_status/is_commercial）：整批单事务。

    每单走单管线（merge→financials→ddl/archive→UPDATE），客户集去重 recalc。"""
    keys = set(updates.keys())
    needs_financials = bool(_MONEY_FIELDS & keys)
    needs_ddl = bool(_DDL_TRIGGER_FIELDS & keys) or needs_financials
    count = 0
    cids = set()
    with transaction() as conn:
        for oid in order_ids:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
            if not row:
                continue
            merged = {**dict(row), **updates}
            if needs_financials:
                merged = _calc_financials(merged)
            if needs_ddl:
                merged = _auto_calc_ddl_status(merged)
            payload = {
                'ddl_status': merged.get('ddl_status'),
                'income': float(merged.get('income') or 0),
                'platform_fee': float(merged.get('platform_fee') or 0),
                'actual_received': float(merged.get('actual_received') or 0),
            }
            for k, v in updates.items():
                payload[k] = v
            if int(merged.get('is_archived') or 0) == 1:
                payload['is_archived'] = 1
                payload['completed_at'] = merged.get('completed_at')
                payload['is_overdue'] = int(merged.get('is_overdue') or 0)
            set_clause = ', '.join([f"{k} = ?" for k in payload.keys()])
            conn.execute(
                f"UPDATE orders SET {set_clause}, updated_at = datetime('now','localtime') WHERE id = ?",
                list(payload.values()) + [oid])
            if row['customer_id']:
                cids.add(row['customer_id'])
            count += 1
        for cid in cids:
            recalc_customer_stats(cid, conn=conn)
    return count


def batch_delete_orders(order_ids: list[int]) -> int:
    """批量删除：整批单事务，客户集去重 recalc。"""
    count = 0
    cids = set()
    with transaction() as conn:
        for oid in order_ids:
            row = conn.execute("SELECT customer_id FROM orders WHERE id = ?", (oid,)).fetchone()
            if not row:
                continue
            conn.execute("DELETE FROM orders WHERE id = ?", (oid,))
            if row['customer_id']:
                cids.add(row['customer_id'])
            count += 1
        for cid in cids:
            recalc_customer_stats(cid, conn=conn)
    return count


def batch_recompute_orders(order_ids: list[int]) -> int:
    """批量重算（P17a）：整批单事务，每单走 recompute 管线。"""
    count = 0
    cids = set()
    with transaction() as conn:
        for oid in order_ids:
            existing = get_order(oid, conn=conn)
            if not existing:
                continue
            data = dict(existing)
            data = _calc_financials(data)
            data = _auto_calc_ddl_status(data)
            derived = {
                'ddl_status': data.get('ddl_status'),
                'income': float(data.get('income') or 0),
                'platform_fee': float(data.get('platform_fee') or 0),
                'actual_received': float(data.get('actual_received') or 0),
            }
            if int(data.get('is_archived') or 0) == 1:
                derived['is_archived'] = 1
                derived['completed_at'] = data.get('completed_at')
                derived['is_overdue'] = int(data.get('is_overdue') or 0)
            set_clause = ', '.join([f"{k} = ?" for k in derived.keys()])
            conn.execute(
                f"UPDATE orders SET {set_clause}, updated_at = datetime('now','localtime') WHERE id = ?",
                list(derived.values()) + [oid])
            if existing.get('customer_id'):
                cids.add(existing['customer_id'])
            count += 1
        for cid in cids:
            recalc_customer_stats(cid, conn=conn)
    return count


# ═══════════════════════════════════════════════════════════
# 统计
# ═══════════════════════════════════════════════════════════

def _shift_months(d: date, months: int) -> date:
    """日期按月平移（天数钳制到目标月月末）"""
    import calendar
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _pct_change(cur: float, prev: float) -> int:
    """环比百分比：prev 为 0 时，cur>0 记 100，否则记 0"""
    if prev and prev > 0:
        return round((cur - prev) / prev * 100)
    return 100 if cur > 0 else 0


# 预设范围 → 环比平移月数 / 对比文案
_PRESET_SHIFT = {'month': (-1, 'vs 上月'), 'quarter': (-3, 'vs 上季度'), 'year': (-12, 'vs 去年')}
_PRESET_LABEL = {'month': '本月', 'quarter': '本季度', 'year': '本年', 'all': '全部', 'custom': '自定义'}


# ═══════════════════════════════════════════════════════════
# 统计口径字典（P19-F3）— 每个统计指标全库唯一定义，主页/看板/客户页同源调用
# ═══════════════════════════════════════════════════════════
#
# 统一口径（主页与收入看板一致）：
#   所有收入/完成统计统一按「排期月份」（scheduled_end）归属。
# 规则：
#   - 金额字段一律 actual_received（净额）；毛额 income 仅在品类分布分列并标注「毛」。
#   - 已实现收入/完成数：is_archived=1 且 终态=完成（非退单），按 scheduled_end 归属。
#   - 活跃/预计/逾期：is_archived=0 且 非终态。
#   - 客户 total_spent：该客户全部订单 SUM(actual_received)（含进行中/未收款/退单），页面明示。
#   - 复购标记（派生视图，P19-F8）：repeat_count=同 customer_id 他单数（排除退单终态、排除本单），
#     is_repeat=(repeat_count>0)；查询时计算（_apply_repeat_for_rows），落库两列暂留仅作回滚兜底。

def _metric_conn(conn):
    """内部：传入连接则复用，否则自建。返回 (conn, should_close)。"""
    return (conn, False) if conn is not None else (get_db(), True)


def _safe_iso(s):
    """P19-F6 内部容错：任意输入 → date；空/非法 → None（不抛异常，杜绝 500）。"""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _period_end_date(preset: str, today_d: date) -> date:
    """返回预设周期的末日：month→月末, quarter→季末, year→年末；其余回落到今天。"""
    import calendar
    if preset == 'month':
        return date(today_d.year, today_d.month, calendar.monthrange(today_d.year, today_d.month)[1])
    elif preset == 'quarter':
        q_end_month = (today_d.month - 1) // 3 * 3 + 3
        return date(today_d.year, q_end_month, calendar.monthrange(today_d.year, q_end_month)[1])
    elif preset == 'year':
        return date(today_d.year, 12, 31)
    return today_d


def _in_range_sql(date_expr: str, start, end, params: list) -> str:
    """「date_expr 落在 [start, end]」SQL 片段（end 按次日开区间）；start/end 为 None 不限。"""
    conds = [f"{date_expr} IS NOT NULL"]
    if start:
        conds.append(f"{date_expr} >= ?")
        params.append(str(start)[:10])
    if end:
        end_d = _safe_iso(end)
        if end_d:  # 非法日期容错：跳过上限条件（路由层已拦，双层防护）
            end_next = (end_d + timedelta(days=1)).isoformat()
            conds.append(f"{date_expr} < ?")
            params.append(end_next)
    return ' AND '.join(conds)


def _sched_overlap_sql(start, end, params: list) -> str:
    """「排期与 [start, end] 有交集」SQL 片段；start/end 为 None → 1=1（不限）。"""
    conds = []
    if end:
        conds.append("scheduled_start <= ?")
        params.append(str(end)[:10])
    if start:
        conds.append("(scheduled_end >= ? OR scheduled_end IS NULL)")
        params.append(str(start)[:10])
    return ' AND '.join(conds) if conds else "1=1"


def metric_active_count(start=None, end=None, conn=None) -> int:
    """活跃订单数。
    过滤：is_archived=0 且 非终态（STAGE_META）且 排期与 [start,end] 有交集；无范围=全部活跃。
    金额：—（计数）。含退单：否。
    """
    c, close = _metric_conn(conn)
    params: list = []
    overlap = _sched_overlap_sql(start, end, params)
    terminal = tuple(get_terminal_stages())
    row = c.execute(
        f"SELECT COUNT(1) FROM orders WHERE is_archived = 0 "
        f"AND current_stage NOT IN ({','.join('?' * len(terminal))}) AND {overlap}",
        list(terminal) + params
    ).fetchone()
    if close:
        c.close()
    return row[0]


def metric_realized_income(start=None, end=None, conn=None) -> float:
    """本期到账（已到账口径，Spec 26）。
    simple 段：已归档+完成单虚拟事件，scheduled_end∈范围（过滤逐字沿用改造前）；
    installment 段：每笔收款 paid_at∈范围求和，不以整单状态为前提。
    时间归属：ev_date ∈ [start,end]（None=不限）；金额：SUM(ev_amount)。含退单：
    simple 段否（完成单限定）；installment 段退单已到账笔计入（spec §3.3）。
    """
    c, close = _metric_conn(conn)
    params: list = []
    in_range = _in_range_sql('ev_date', start, end, params)
    row = c.execute(
        f"SELECT COALESCE(SUM(ev_amount), 0) "
        f"FROM ({_payment_events_sql(simple_extra=' AND is_archived = 1 AND current_stage = ?')}) "
        f"WHERE {in_range}",
        [get_done_stage()] + params
    ).fetchone()
    if close:
        c.close()
    return round(row[0], 2)


def metric_completed_count(start=None, end=None, conn=None) -> int:
    """完成单数（统一口径）。口径同 metric_realized_income，COUNT。含退单：否。"""
    c, close = _metric_conn(conn)
    params: list = []
    in_range = _in_range_sql('scheduled_end', start, end, params)
    row = c.execute(
        f"SELECT COUNT(1) FROM orders WHERE is_archived = 1 AND current_stage = ? AND {in_range}",
        [get_done_stage()] + params
    ).fetchone()
    if close:
        c.close()
    return row[0]


def metric_expected_income(start=None, end=None, conn=None) -> float:
    """预计收入（Spec 26 D6 后半：待收口径）。
    过滤：is_archived=0 且 非终态 且 排期与 [start,end] 有交集；
    金额：simple 活跃单贡献 actual_received（净额，等价原口径）；
    installment 活跃单贡献 actual_received − Σ已到账（相关子查询，下限 0）。
    含退单：否。
    """
    c, close = _metric_conn(conn)
    params: list = []
    overlap = _sched_overlap_sql(start, end, params)
    terminal = tuple(get_terminal_stages())
    row = c.execute(
        f"SELECT COALESCE(SUM(CASE WHEN COALESCE(payment_mode, 'simple') = 'installment' "
        f"THEN MAX(actual_received - COALESCE((SELECT SUM(amount) FROM order_payments "
        f"WHERE order_id = orders.id), 0), 0) ELSE actual_received END), 0) "
        f"FROM orders WHERE is_archived = 0 "
        f"AND current_stage NOT IN ({','.join('?' * len(terminal))}) AND {overlap}",
        list(terminal) + params
    ).fetchone()
    if close:
        c.close()
    return round(row[0], 2)


def _overdue_where(alias: str = '') -> tuple:
    """逾期 WHERE 片段 + 参数（逾期卡片与明细同源的唯一出处）。
    口径：is_archived=0 且 非终态 且 scheduled_end<today（全时间，不随范围变化）。
    """
    p = f"{alias}." if alias else ''
    sql = (f"{p}is_archived = 0 AND {p}scheduled_end < ? "
           f"AND {p}current_stage NOT IN (?, ?)")
    return sql, [date.today().isoformat(), get_done_stage(), get_refund_stage()]


def metric_overdue_orders(conn=None) -> list[dict]:
    """逾期订单明细（全时间口径）。过滤见 _overdue_where；与 metric_overdue_count 同一 WHERE。"""
    c, close = _metric_conn(conn)
    where, params = _overdue_where('o')
    rows = c.execute(
        f"""SELECT o.*, c.name as customer_name
           FROM orders o
           LEFT JOIN customers c ON o.customer_id = c.id
           WHERE {where}
           ORDER BY o.scheduled_end ASC""",
        params
    ).fetchall()
    result = _apply_days_remaining_for_rows([dict(r) for r in rows])
    if close:
        c.close()
    return result


def metric_overdue_count(conn=None) -> int:
    """逾期数（全时间口径）。与 metric_overdue_orders 同一 WHERE（卡片数=明细行数）。"""
    c, close = _metric_conn(conn)
    where, params = _overdue_where()
    row = c.execute(f"SELECT COUNT(1) FROM orders WHERE {where}", params).fetchone()
    if close:
        c.close()
    return row[0]


def metric_type_distribution(conn=None, year: int = None, month: int = None) -> list[dict]:
    """品类分布（P20-F17：已完成订单 is_archived=1 且 commission_type 非空，看售出结果）。
    每品类：cnt 单数 / gross=SUM(income) 应收（毛）/ net=SUM(actual_received) 实收（净）。

    year/month: 可选筛选，按排期月份（scheduled_start）过滤；未排期单不计入统计。

    Spec 35 D1/D3：返回项附 label 供显示，commission_type 原值保留供配色查找
    （COMMISSION_COLORS 的键是 code）；choice_items 无对应行的值原样直通。
    """
    c, close = _metric_conn(conn)
    where_parts = ["is_archived = 1", "commission_type IS NOT NULL", "commission_type != ''"]
    params = []
    if year is not None:
        where_parts.append("strftime('%Y', scheduled_start) = ?")
        params.append(str(year))
    if month is not None:
        where_parts.append("strftime('%m', scheduled_start) = ?")
        params.append(str(month).zfill(2))
    where_clause = ' AND '.join(where_parts)
    rows = c.execute(f"""
        SELECT commission_type, COUNT(1) as cnt,
               COALESCE(SUM(income), 0) as gross,
               COALESCE(SUM(actual_received), 0) as net
        FROM orders
        WHERE {where_clause}
        GROUP BY commission_type
        ORDER BY cnt DESC
    """, params).fetchall()
    if close:
        c.close()
    label_map = {r['code']: r['label'] for r in get_choice_rows('commission_type')}
    result = []
    for r in rows:
        item = dict(r)
        item['label'] = label_map.get(item['commission_type'], item['commission_type'])
        result.append(item)
    return result


def get_dashboard_stats(start_date: str = None, end_date: str = None, preset: str = 'month') -> dict:
    """仪表盘统计 — P13a 口径重定义。

    Args:
        start_date: 统计起始日期 (YYYY-MM-DD)，默认本月1号
        end_date:   统计截止日期 (YYYY-MM-DD)，默认今天
        preset:     month/quarter/year/all/custom，决定环比的上一周期

    口径（P13a 定义，P19-F3 起全部改调统计口径字典，见上方专区 docstring）：
        active_count:    metric_active_count（活跃非终态，排期交集）
        income:          metric_realized_income（归档+终态完成，scheduled_end 归属，净额）
        overdue_count:   metric_overdue_count（活跃非终态 + scheduled_end<today，与明细同源）
        completed_count: metric_completed_count（同 realized，COUNT）
        expected_income: metric_expected_income（活跃非终态，排期交集，净额）
    """
    today_d = date.today()

    if preset == 'all':
        rs = re = None
    else:
        # P19-F6：非法日期兜底默认范围（路由层已拦 400，此处双层防护）
        # 期末修正：预设范围 end 默认到期末（月末/季末/年末），非今天
        rs = _safe_iso(start_date) if start_date else today_d.replace(day=1)
        re = _safe_iso(end_date) if end_date else _period_end_date(preset, today_d)
        if start_date and rs is None:
            rs = today_d.replace(day=1)
        if end_date and re is None:
            re = _period_end_date(preset, today_d)

    conn = get_db()
    cur = {
        'active': metric_active_count(rs, re, conn),
        'income': metric_realized_income(rs, re, conn),
        'completed': metric_completed_count(rs, re, conn),
        'expected': metric_expected_income(rs, re, conn),
    }

    # 逾期：全部时间口径，不随范围变化，无环比（与逾期明细同源）
    overdue = metric_overdue_count(conn)

    # 环比：仅 month/quarter/year 预设，上一同类周期（同长度平移）
    prev = None
    if preset in _PRESET_SHIFT:
        months, _ = _PRESET_SHIFT[preset]
        prs, pre_ = _shift_months(rs, months), _shift_months(re, months)
        prev = {
            'active': metric_active_count(prs, pre_, conn),
            'income': metric_realized_income(prs, pre_, conn),
            'completed': metric_completed_count(prs, pre_, conn),
            'expected': metric_expected_income(prs, pre_, conn),
        }

    conn.close()

    has_compare = prev is not None
    return {
        'active_count': cur['active'],
        'active_change': _pct_change(cur['active'], prev['active']) if has_compare else None,
        'income': round(cur['income'], 2),
        'income_change': _pct_change(cur['income'], prev['income']) if has_compare else None,
        'overdue_count': overdue,
        'completed_count': cur['completed'],
        'completed_change': _pct_change(cur['completed'], prev['completed']) if has_compare else None,
        'expected_income': round(cur['expected'], 2),
        'expected_change': _pct_change(cur['expected'], prev['expected']) if has_compare else None,
        'has_compare': has_compare,
        'compare_label': _PRESET_SHIFT.get(preset, ('', ''))[1],
        'range_label': _PRESET_LABEL.get(preset, '本月'),
        'range_start': rs.isoformat() if rs else None,
        'range_end': re.isoformat() if re else None,
        'preset': preset,
        'is_custom_range': preset == 'custom',
    }


def get_stats_detail(metric: str, start_date: str = None, end_date: str = None, year: int = None, month: int = None) -> dict:
    """统计卡明细（小票弹窗数据源）。统一口径：所有收入/完成统计按 scheduled_end 归属。

    主页小票（与 get_dashboard_stats 同源）：
        active     — is_archived=0 + 非终态 + 排期与范围有交集（date_expr=scheduled_start）
        expected   — 同 active；金额待收口径（Spec 26 D6 后半，与
                     metric_expected_income 同源：simple=净额；installment=
                     净额−Σ已到账下限 0）；附 net_amount（应收净额）/
                     received_amount（已到账）/payment_mode 三列供小票按单展示
        income     — 收款事件流（Spec 26，与 metric_realized_income 同源成对）：
                      simple=已归档完成单虚拟事件；installment=每笔收款一行
                      （附 payment_mode 供小票按笔标记分期，task-110）
        completed  — 同改造前 income 口径（工作口径，Spec 26 明确不动）
        overdue    — 活跃 + 非终态 + scheduled_end<today（与 _overdue_where 同源，全时间口径）
    图表小票（与 get_monthly_income_stats / get_monthly_projected_stats 同源）：
        monthly_income    — 指定年月的已到账明细（事件流，同月度收入图口径；
                            附 payment_mode 供小票按笔标记分期）
        monthly_projected — 指定年月的进行中订单预计净额明细

    参数：metric + 范围（主页 active/income/overdue/completed/expected 用 start_date/end_date；图表 monthly_income/monthly_projected 用 year/month）。
    返回 {'items': [{id, date, project_name, amount}], 'total': 金额合计, 'count': 单数}
    （expected 与事件流 metric 的 items 附 payment_mode 等扩展键；total 恒为 amount 列合计）。
    """
    stage_done = get_done_stage()  # P19-F2
    stage_cancelled = get_refund_stage()
    today_d = date.today()
    today = today_d.isoformat()

    conn = get_db()
    where, date_expr, amount_expr, params = '', '', 'actual_received', []
    from_expr = 'orders'  # Spec 26：income/monthly_income 分支改事件流子查询
    extra_cols = ''  # Spec 26 task-110：分支扩展列（expected 三数 / 事件流 mode）
    order = 'date ASC'

    if metric in ('active', 'expected'):
        # 排期与范围有交集 且 未归档 且 阶段∉{完成,退单}
        conds = ["is_archived = 0", "current_stage NOT IN (?, ?)"]
        params = [stage_done, stage_cancelled]
        if start_date:
            conds.append("(scheduled_end >= ? OR scheduled_end IS NULL)")
            params.append(start_date)
        if end_date:
            conds.append("scheduled_start <= ?")
            params.append(end_date)
        where = ' AND '.join(conds)
        date_expr = "COALESCE(scheduled_start, substr(updated_at, 1, 10))"
        if metric == 'expected':
            # Spec 26 D6 后半（task-110 收口）：待收口径，与 metric_expected_income
            # 同一表达式（卡片数=小票 total 同源）；三数列供小票按单展示
            amount_expr = ("CASE WHEN COALESCE(payment_mode, 'simple') = 'installment' "
                           "THEN MAX(actual_received - COALESCE((SELECT SUM(amount) FROM order_payments "
                           "WHERE order_id = orders.id), 0), 0) ELSE actual_received END")
            extra_cols = (", COALESCE(actual_received, 0) AS net_amount"
                          ", COALESCE((SELECT SUM(amount) FROM order_payments "
                          "WHERE order_id = orders.id), 0) AS received_amount"
                          ", COALESCE(payment_mode, 'simple') AS payment_mode")
    elif metric == 'income':
        # 本期到账明细（事件流，Spec 26，与 metric_realized_income 同源成对）：
        # simple=已归档完成单虚拟事件（过滤逐字沿用）；installment=每笔收款一行
        conds = ["ev_date IS NOT NULL"]
        params = [stage_done]
        if start_date:
            conds.append("ev_date >= ?")
            params.append(start_date)
        if end_date:
            end_d = _safe_iso(end_date)  # P19-F6 容错：非法日期跳过上限
            if end_d:
                re_next = (end_d + timedelta(days=1)).isoformat()
                conds.append("ev_date < ?")
                params.append(re_next)
        where = ' AND '.join(conds)
        date_expr = "ev_date"
        amount_expr = 'ev_amount'
        from_expr = f"({_payment_events_sql(simple_extra=' AND is_archived = 1 AND current_stage = ?', with_order_info=True)})"
        extra_cols = ', ev_mode AS payment_mode'  # task-110：小票按笔标记分期
    elif metric == 'completed':
        # 完成单数明细（工作口径，Spec 26 明确不动）：已归档+完成单按 scheduled_end 落范围
        conds = ["is_archived = 1", "scheduled_end IS NOT NULL", "current_stage = ?"]
        params = [stage_done]
        if start_date:
            conds.append("scheduled_end >= ?")
            params.append(start_date)
        if end_date:
            end_d = _safe_iso(end_date)  # P19-F6 容错：非法日期跳过上限
            if end_d:
                re_next = (end_d + timedelta(days=1)).isoformat()
                conds.append("scheduled_end < ?")
                params.append(re_next)
        where = ' AND '.join(conds)
        date_expr = "scheduled_end"
    elif metric == 'overdue':
        # 与逾期卡片/明细同源（P19-F3 口径字典 _overdue_where）：活跃 + 非终态 + scheduled_end<today
        where, params = _overdue_where()
        date_expr = "scheduled_end"
    elif metric == 'monthly_income':
        # 图表小票：指定年月的已到账明细（事件流，Spec 26，与月度收入图同源）
        paid_sql, paid_params = _paid_status_sql()
        month_str = month or str(today_d.month)
        m = int(month_str) if str(month_str).isdigit() else today_d.month
        y = year or today_d.year
        m_start = date(y, m, 1).isoformat()
        m_end = date(y + (1 if m == 12 else 0), (m % 12) + 1, 1).isoformat()
        where = "ev_date IS NOT NULL AND ev_date >= ? AND ev_date < ?"
        params = paid_params + [m_start, m_end]  # 子查询内 IN (?) 占位符先出现，月界在后
        date_expr = "ev_date"
        amount_expr = 'ev_amount'
        from_expr = f"({_payment_events_sql(simple_extra=f' AND {paid_sql}', with_order_info=True)})"
        extra_cols = ', ev_mode AS payment_mode'  # task-110：小票按笔标记分期
    elif metric == 'monthly_projected':
        # 图表小票：指定月份的进行中订单预计收入明细
        month_str = month or str(today_d.month)
        m = int(month_str) if str(month_str).isdigit() else today_d.month
        y = year or today_d.year
        m_start = date(y, m, 1).isoformat()
        m_end = date(y + (1 if m == 12 else 0), (m % 12) + 1, 1).isoformat()
        where = f"scheduled_end >= ? AND scheduled_end < ? AND is_archived = 0 AND current_stage NOT IN (?, ?)"
        params = [m_start, m_end, stage_done, stage_cancelled]
        date_expr = "scheduled_end"
        amount_expr = 'actual_received'  # P19-F3：预计收入统一净额（与 metric_expected_income 同字段）
    else:
        conn.close()
        return {'items': [], 'total': 0, 'count': 0}

    rows = conn.execute(
        f"""SELECT id, project_name,
                   {date_expr} AS date,
                   COALESCE({amount_expr}, 0) AS amount{extra_cols}
            FROM {from_expr} WHERE {where}
            ORDER BY {order}, id ASC""",
        params
    ).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    return {
        'items': items,
        'total': round(sum(i['amount'] for i in items), 2),
        'count': len(items),
    }


def get_commission_type_distribution(year: int = None, month: int = None) -> list[dict]:
    """品类分布 — P19-F3 起改调口径字典 metric_type_distribution（cnt/gross 应收毛/net 实收净）
    支持按 year/month 筛选排期月份"""
    return metric_type_distribution(year=year, month=month)


def get_top_customers(limit: int = 5) -> list[dict]:
    """消费排名"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, total_spent, purchase_count FROM customers ORDER BY total_spent DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════
# 收入看板查询
# ═══════════════════════════════════════════════════════════

def get_available_years() -> list[int]:
    """P20-F19：获取年度选择器范围 = 数据年份 ∪ [最早年, max(当年, 最早年+2)]。
    确保即使数据集中在单一年份，用户也能查看邻近年度（空数据时显示 0）。
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT CAST(strftime('%Y', created_at) AS INTEGER) as y FROM orders "
        "UNION SELECT DISTINCT CAST(strftime('%Y', completed_at) AS INTEGER) FROM orders WHERE completed_at IS NOT NULL "
        "UNION SELECT DISTINCT CAST(strftime('%Y', scheduled_end) AS INTEGER) FROM orders WHERE scheduled_end IS NOT NULL "
        "UNION SELECT DISTINCT CAST(strftime('%Y', paid_at) AS INTEGER) FROM order_payments "
        "ORDER BY y DESC"
    ).fetchall()
    conn.close()
    data_years = [r[0] for r in rows if r[0]]
    if not data_years:
        return [date.today().year]
    today = date.today().year
    earliest = min(data_years)
    latest = max(data_years)
    upper = max(today, earliest + 2, latest)
    full_range = list(range(earliest, upper + 1))
    full_range.sort(reverse=True)
    return full_range


def _payment_events_sql(simple_extra: str = '', installment_extra: str = '',
                        with_order_info: bool = False) -> str:
    """收款事件流子查询构建器（Spec 26 §3.1 现金口径统一数据源）。

    - simple 段：虚拟事件（ev_date=scheduled_end, ev_amount=actual_received），
      附加过滤由调用方以改造前逐字条件传入（等价性由 task-103 基线对拍证明）；
    - installment 段：真实事件（ev_date=paid_at, ev_amount=amount）到账即计，
      不含整单 payment_status 过滤（spec §3.3：退单分期单已到账笔计入月度图）。

    simple_extra / installment_extra：以 " AND ..." 形式拼接的附加 WHERE 段
    （占位符参数由调用方按 SQL 拼接顺序提供）；外层对 ev_date 统一做范围过滤。
    with_order_info：额外输出 id / project_name（小票明细用；installment 段
    同单多笔收款产出多行，每笔一行，id 重复为预期语义）。
    输出恒含 ev_mode（'simple'/'installment' 常量列；Spec 26 task-110 小票按笔
    标记分期用；聚合调用方不取该列，无影响）。
    """
    info_simple = 'id, project_name, ' if with_order_info else ''
    info_inst = 'o.id, o.project_name, ' if with_order_info else ''
    return f"""SELECT {info_simple}scheduled_end AS ev_date, actual_received AS ev_amount,
               'simple' AS ev_mode
               FROM orders
               WHERE payment_mode = 'simple'{simple_extra}
               UNION ALL
               SELECT {info_inst}p.paid_at AS ev_date, p.amount AS ev_amount,
               'installment' AS ev_mode
               FROM order_payments p JOIN orders o ON p.order_id = o.id
               WHERE o.payment_mode = 'installment'{installment_extra}"""


def _payment_events_monthly_rows(conn, year: int) -> list:
    """事件流按月求和行（月度收入/年累进两图共用）。

    simple 段过滤逐字沿用改造前 WHERE（payment_status IN PAID_STATUSES +
    scheduled_end 归月；scheduled_end IS NOT NULL 由外层范围比较自然排除
    NULL 行，语义等价）；installment 段每笔按 paid_at 归月。
    """
    paid_sql, paid_params = _paid_status_sql()
    return conn.execute(
        f"""SELECT CAST(strftime('%m', ev_date) AS INTEGER) as m,
                   COALESCE(SUM(ev_amount), 0) as total
            FROM ({_payment_events_sql(simple_extra=f" AND {paid_sql}")})
            WHERE ev_date >= ? AND ev_date < ?
            GROUP BY m ORDER BY m""",
        (*paid_params, f"{year}-01-01", f"{year + 1}-01-01")
    ).fetchall()


def get_monthly_income_stats(year: int = None, months: int = 12) -> list[dict]:
    """月度收入统计（已到账口径，Spec 26）——
    simple=已结算单虚拟事件按排期月；installment=每笔收款按到账月，不以整单结算为前提。
    """
    if year is None:
        year = date.today().year
    conn = get_db()
    rows = _payment_events_monthly_rows(conn, year)
    result = _fill_monthly_result(rows, months, 'income')
    conn.close()
    return result


def get_monthly_projected_income(year: int = None, months: int = 12) -> list[dict]:
    """每月预计收入 — 单次 GROUP BY 查询（动态阶段名）"""
    if year is None:
        year = date.today().year
    stage_done = get_done_stage()  # P19-F2
    stage_cancelled = get_refund_stage()
    conn = get_db()
    rows = conn.execute(
        f"""SELECT CAST(strftime('%m', scheduled_end) AS INTEGER) as m,
                  COALESCE(SUM(actual_received), 0) as total
           FROM orders
           WHERE scheduled_end >= ? AND scheduled_end < ?
             AND is_archived = 0 AND current_stage NOT IN (?, ?)
           GROUP BY m ORDER BY m""",
        (f"{year}-01-01", f"{year+1}-01-01", stage_done, stage_cancelled)
    ).fetchall()
    result = _fill_monthly_result(rows, months, 'projected')
    conn.close()
    return result


def get_cumulative_annual_income(year: int = None) -> list[dict]:
    """当年收入累进（已到账口径，Spec 26）——事件流逐月累加（口径同月度收入图）"""
    if year is None:
        year = date.today().year
    conn = get_db()
    rows = _payment_events_monthly_rows(conn, year)
    cumulative = 0.0
    result = []
    row_map = {r[0]: r[1] for r in rows}
    for m in range(1, 13):
        monthly = round(row_map.get(m, 0), 2)
        cumulative += monthly
        result.append({'month': f"{m}月", 'cumulative': cumulative, 'monthly': monthly})
    conn.close()
    return result


def _fill_monthly_result(rows, months, value_key):
    """将 GROUP BY 结果填充为完整月份列表"""
    row_map = {r[0]: r[1] for r in rows}
    return [{'month': f"{m}月", value_key: round(row_map.get(m, 0), 2)} for m in range(1, months + 1)]


# ═══════════════════════════════════════════════════════════
# P20b 时薪统计（口径见 spec12 §3：已完成 且 work_hours>0 且未排除；
# 月归属 scheduled_end；汇总时薪 = SUM(actual_received)/SUM(work_hours) 加权）
# ═══════════════════════════════════════════════════════════

def _hourly_filter_sql() -> tuple[str, list]:
    """时薪统计统一过滤条件（参数化，供各统计函数拼接）"""
    return ("current_stage = ? AND work_hours > 0 AND exclude_hourly = 0", [get_done_stage()])


def _weighted_rate(amount, hours):
    """加权时薪：总实收/总工时；无工时返回 None（前端显 —）"""
    return round(amount / hours, 2) if hours else None


def get_hourly_rate_summary(year: int = None) -> dict:
    """时薪统计卡三值：本月加权时薪（自然月，跟随今天）/ 指定年加权时薪 / 平均预估偏差"""
    if year is None:
        year = date.today().year
    conn = get_db()
    cond, params = _hourly_filter_sql()
    today = date.today()
    month_row = conn.execute(
        f"""SELECT COALESCE(SUM(actual_received), 0) as amt, COALESCE(SUM(work_hours), 0) as hrs
            FROM orders
            WHERE {cond} AND scheduled_end IS NOT NULL
              AND strftime('%Y-%m', scheduled_end) = ?""",
        tuple(params) + (f"{today.year}-{today.month:02d}",)
    ).fetchone()
    year_row = conn.execute(
        f"""SELECT COALESCE(SUM(actual_received), 0) as amt, COALESCE(SUM(work_hours), 0) as hrs
            FROM orders
            WHERE {cond} AND scheduled_end >= ? AND scheduled_end < ?""",
        tuple(params) + (f"{year}-01-01", f"{year+1}-01-01")
    ).fetchone()
    bias_row = conn.execute(
        f"""SELECT AVG((work_hours - estimated_hours) * 1.0 / estimated_hours) as bias
            FROM orders
            WHERE {cond} AND estimated_hours > 0
              AND scheduled_end >= ? AND scheduled_end < ?""",
        tuple(params) + (f"{year}-01-01", f"{year+1}-01-01")
    ).fetchone()
    conn.close()
    return {
        'month_rate': _weighted_rate(month_row['amt'], month_row['hrs']),
        'year_rate': _weighted_rate(year_row['amt'], year_row['hrs']),
        'avg_bias': round(bias_row['bias'], 4) if bias_row['bias'] is not None else None,
    }


def get_monthly_hourly_trend(year: int = None) -> list[dict]:
    """月度时薪趋势：12 月每月 {加权时薪, 单数, 总工时}；空月 rate=None（折线 spanGaps 断开）"""
    if year is None:
        year = date.today().year
    conn = get_db()
    cond, params = _hourly_filter_sql()
    rows = conn.execute(
        f"""SELECT CAST(strftime('%m', scheduled_end) AS INTEGER) as m,
                  COALESCE(SUM(actual_received), 0) as amt,
                  COALESCE(SUM(work_hours), 0) as hrs,
                  COUNT(*) as cnt
           FROM orders
           WHERE {cond} AND scheduled_end >= ? AND scheduled_end < ?
           GROUP BY m ORDER BY m""",
        tuple(params) + (f"{year}-01-01", f"{year+1}-01-01")
    ).fetchall()
    conn.close()
    row_map = {r['m']: r for r in rows}
    result = []
    for m in range(1, 13):
        r = row_map.get(m)
        result.append({
            'month': f"{m}月",
            'rate': _weighted_rate(r['amt'], r['hrs']) if r else None,
            'count': r['cnt'] if r else 0,
            'hours': round(r['hrs'], 1) if r else 0,
        })
    return result


def _hourly_group_stats(group_col: str, year: int, month: int = None) -> list[dict]:
    """按维度分组的加权时薪（内部：group_col 仅取白名单列，非用户输入）"""
    conn = get_db()
    cond, params = _hourly_filter_sql()
    sql = (
        f"""SELECT COALESCE(NULLIF({group_col}, ''), '未分类') as name,
                  COALESCE(SUM(actual_received), 0) as amt,
                  COALESCE(SUM(work_hours), 0) as hrs,
                  COUNT(*) as cnt
           FROM orders
           WHERE {cond} AND scheduled_end >= ? AND scheduled_end < ?"""
    )
    sql_params = tuple(params) + (f"{year}-01-01", f"{year+1}-01-01")
    if month:
        sql += " AND CAST(strftime('%m', scheduled_end) AS INTEGER) = ?"
        sql_params += (month,)
    sql += " GROUP BY name"
    rows = conn.execute(sql, sql_params).fetchall()
    conn.close()
    # 卡168 code 化后 group_col 存的是 code，经 choice_items 翻成中文显示名；
    # 查不到（老库未迁移中文脏值 / '未分类'）原样直通。name 供显示，code 供配色查找
    # （SOURCE_COLORS/COMMISSION_COLORS 键为 code）。
    label_map = {r['code']: r['label'] for r in get_choice_rows(group_col)}
    result = [{
        'name': label_map.get(r['name'], r['name']),
        'code': r['name'],
        'rate': _weighted_rate(r['amt'], r['hrs']),
        'count': r['cnt'],
        'hours': round(r['hrs'], 1),
    } for r in rows if r['hrs']]
    result.sort(key=lambda x: x['rate'], reverse=True)
    return result


def get_hourly_by_commission_type(year: int = None, month: int = None) -> list[dict]:
    """品类时薪对比（支持月份筛选，仿 type-distribution）"""
    if year is None:
        year = date.today().year
    return _hourly_group_stats('commission_type', year, month)


def get_hourly_by_source(year: int = None) -> list[dict]:
    """来源时薪对比（抽成后真实时薪：actual_received 已扣手续费）"""
    if year is None:
        year = date.today().year
    return _hourly_group_stats('source', year)


def get_quote_sample(commission_type: str = None) -> dict | None:
    """报价样本：严格基于相同类别(commission_type)的完成单计算。

    无类别 / 该类别无完成单 → 返回 None（不降级全局）。
    返回 {rate: 加权时薪, rates: 单笔时薪升序列表, count, scope: 'type'}。
    """
    if not commission_type:
        return None
    conn = get_db()
    cond, params = _hourly_filter_sql()
    rows = conn.execute(
        f"""SELECT actual_received, work_hours FROM orders
            WHERE {cond} AND actual_received > 0 AND commission_type = ?""",
        tuple(params) + (commission_type,)
    ).fetchall()
    conn.close()
    if not rows:
        return None
    total_amt = sum(r['actual_received'] for r in rows)
    total_hrs = sum(r['work_hours'] for r in rows)
    rates = sorted(r['actual_received'] / r['work_hours'] for r in rows)
    return {
        'rate': _weighted_rate(total_amt, total_hrs),
        'rates': [round(x, 2) for x in rates],
        'count': len(rows),
        'scope': 'type',
    }


def update_order_work_hours(order_id: int, work_hours: float) -> tuple:
    """补录实际工时（补录弹窗「保存」，复用 update_order 单管线；透传 (ok, err)）"""
    return update_order(order_id, {'work_hours': work_hours})


def set_order_exclude_hourly(order_id: int) -> tuple:
    """单订单排除时薪统计（补录弹窗「此单不统计」，永不再弹；透传 (ok, err)）"""
    return update_order(order_id, {'exclude_hourly': 1})


# ═══════════════════════════════════════════════════════════
# 客户 CRUD
# ═══════════════════════════════════════════════════════════

def create_customer(data: dict) -> int:
    """创建客户

    #40 P3：UNIQUE 冲突转应用层 ValueError（路由返 400），
    try/finally 保证异常路径连接也关闭。
    """
    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO customers (name, platform_url, preferences, notes, tags, is_vip, discount_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (data['name'], data.get('platform_url', ''),
             data.get('preferences', ''), data.get('notes', ''),
             data.get('tags', ''),
             int(data.get('is_vip') or 0),  # Spec19：is_vip 仅徽标语义（D6）
             data.get('discount_pct'))  # Spec19：None = 不打折（D2）
        )
        cid = cur.lastrowid
        conn.commit()
        return cid
    except sqlite3.IntegrityError:
        logging.error("create_customer: 客户名已存在 name=%r", data.get('name'))
        raise ValueError('客户名已存在')
    finally:
        conn.close()


def get_customer(customer_id: int) -> dict | None:
    """获取单个客户"""
    conn = get_db()
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_customer_by_platform_url(platform_url: str) -> dict | None:
    """按平台链接精确匹配客户（稳定标识：客户改名不影响匹配，避免重复建客户）"""
    if not platform_url:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM customers WHERE platform_url = ?", (platform_url,)).fetchone()
    conn.close()
    return dict(row) if row else None


# P13b I2 客户排序列白名单
_CUSTOMER_SORT_COLUMNS = {'id', 'name', 'total_spent', 'purchase_count'}


def list_customers(search: str = None, sort: str = None, direction: str = None) -> list[dict]:
    """客户列表（P13b I1：搜索含 notes；I2：四列排序，默认 total_spent DESC）"""
    conn = get_db()
    where, params = '', []
    if search:
        where = "WHERE name LIKE ? OR notes LIKE ?"
        params = [f"%{search}%", f"%{search}%"]

    if sort in _CUSTOMER_SORT_COLUMNS and direction in ('asc', 'desc'):
        dir_sql = 'DESC' if direction == 'desc' else 'ASC'
        if sort == 'name':
            order_by = f"name IS NULL ASC, name {dir_sql}"
        else:
            order_by = f"{sort} {dir_sql}"
    else:
        order_by = "total_spent DESC"

    rows = conn.execute(f"SELECT * FROM customers {where} ORDER BY {order_by}", params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_customer(customer_id: int, data: dict) -> bool:
    """更新客户"""
    if not data:
        return False
    # 列名白名单，防止注入
    valid_cols = {'name', 'platform_url', 'former_names', 'preferences', 'notes', 'total_spent', 'purchase_count', 'tags',
                  'is_vip', 'discount_pct'}  # Spec19 VIP 折扣（D6：is_vip 仅徽标语义）
    data = {k: v for k, v in data.items() if k in valid_cols}
    if not data:
        return False
    data['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    set_clause = ', '.join([f"{k} = ?" for k in data.keys()])
    conn = get_db()
    try:
        conn.execute(f"UPDATE customers SET {set_clause} WHERE id = ?", list(data.values()) + [customer_id])
        conn.commit()
    finally:
        # UNIQUE 冲突等异常也必须关闭连接，否则泄漏连接持有写锁，阻塞后续写入
        conn.close()
    return True


def delete_customer(customer_id: int) -> bool:
    """删除客户"""
    conn = get_db()
    conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    conn.commit()
    conn.close()
    return True


def get_customer_orders(customer_id: int) -> list[dict]:
    """客户的所有订单"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM orders WHERE customer_id = ? ORDER BY created_at DESC",
        (customer_id,)
    ).fetchall()
    conn.close()
    return _apply_days_remaining_for_rows([dict(r) for r in rows])


def get_customer_images(customer_id: int) -> list[dict]:
    """客户名下所有订单的作品图片（用于客户详情作品画廊，P15f）。

    返回每条 order_images 记录并附带所属订单的 project_name，
    按订单创建时间倒序、组内 sort_order/id 升序排列。"""
    conn = get_db()
    rows = conn.execute(
        """SELECT oi.*, o.project_name AS project_name, o.id AS order_id
           FROM order_images oi
           JOIN orders o ON oi.order_id = o.id
           WHERE o.customer_id = ?
           ORDER BY o.created_at DESC, oi.sort_order, oi.id""",
        (customer_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def recalc_customer_stats(customer_id: int, conn=None) -> None:
    """重新计算客户的 total_spent 和 purchase_count。

    口径：仅统计已归档且已完成且已结算的订单 SUM(actual_received) / COUNT——
    排除进行中、未收款、退单，仅计入已实收金额。
    conn：事务连接复用，传入则不 commit/close。
    """
    if not customer_id:
        return
    own = conn is None
    if own:
        conn = get_db()
    done_vals = _enum_sql_values('stage', {get_done_stage()})
    paid_vals = _enum_sql_values('payment', get_paid_statuses())
    row = conn.execute(
        f"""SELECT COUNT(*) as cnt, COALESCE(SUM(actual_received), 0) as total
           FROM orders
           WHERE customer_id = ?
             AND is_archived = 1
             AND current_stage IN ({','.join('?' * len(done_vals))})
             AND payment_status IN ({','.join('?' * len(paid_vals))})""",
        [customer_id] + done_vals + paid_vals
    ).fetchone()
    conn.execute(
        "UPDATE customers SET total_spent = ?, purchase_count = ?, updated_at = datetime('now','localtime') WHERE id = ?",
        (row['total'], row['cnt'], customer_id)
    )
    if own:
        conn.commit()
        conn.close()


# ═══════════════════════════════════════════════════════════
# 小工具数据层（Spec 22：002 回复模板 / 003 价目表）
# ═══════════════════════════════════════════════════════════

# ── 002 回复模板（reply_templates）──

def create_reply_template(group_name: str, title: str, content: str) -> int:
    """新建回复模板，返回新记录 ID。"""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO reply_templates (group_name, title, content) VALUES (?, ?, ?)",
        ((group_name or '').strip() or '未分组', title, content)
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def update_reply_template(tid: int, group_name: str, title: str, content: str) -> bool:
    """全量更新回复模板（分组/标题/内容）。"""
    conn = get_db()
    cur = conn.execute(
        "UPDATE reply_templates SET group_name = ?, title = ?, content = ?, "
        "updated_at = datetime('now','localtime') WHERE id = ?",
        ((group_name or '').strip() or '未分组', title, content, tid)
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def delete_reply_template(tid: int) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM reply_templates WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def get_reply_template(tid: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM reply_templates WHERE id = ?", (tid,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_reply_templates(group: str | None = None) -> list[dict]:
    """回复模板列表；group 传入则按分组过滤（「全部」由路由层不传实现）。"""
    conn = get_db()
    if group:
        rows = conn.execute(
            "SELECT * FROM reply_templates WHERE group_name = ? "
            "ORDER BY group_name, sort_order, id",
            (group,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM reply_templates ORDER BY group_name, sort_order, id"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_reply_groups() -> list[dict]:
    """分组统计 [{name, count}]（「全部」/「未分组」由路由层合成）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT group_name AS name, COUNT(*) AS count FROM reply_templates "
        "GROUP BY group_name ORDER BY group_name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def rename_reply_group(old_name: str, new_name: str) -> int:
    """重命名分组（事务批量更新组内全部模板）。返回受影响条数。"""
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE reply_templates SET group_name = ?, updated_at = datetime('now','localtime') "
            "WHERE group_name = ?",
            (new_name, old_name)
        )
        return cur.rowcount


def delete_reply_group(group_name: str) -> int:
    """删除分组：组内模板归「未分组」（事务）。返回受影响条数。"""
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE reply_templates SET group_name = '未分组', updated_at = datetime('now','localtime') "
            "WHERE group_name = ? AND group_name != '未分组'",
            (group_name,)
        )
        return cur.rowcount


# ── 003 价目表（pricelist_items + pricelist_images）──

# 更新列白名单（T22.14：update_pricelist_item 只允许这些列）
PRICELIST_UPDATABLE_COLUMNS = {
    'category', 'name', 'price', 'price_max', 'unit', 'description',
    'example_image_path', 'sort_order',
}

# 每项目例图上限（2026-08-12 UX 改造：多例图横排；2026-08-16 Spec 30 扩容 3→10）
PRICELIST_IMAGE_LIMIT = 10


def _pricelist_image_to_dict(row) -> dict:
    """例图行转 dict 并派生 thumb_url/preview_url（image_path 存 preview 相对路径）。"""
    d = dict(row)
    path = d.get('image_path') or ''
    if path:
        d['preview_url'] = '/uploads/' + path
        d['thumb_url'] = '/uploads/' + path.replace('/preview_', '/thumb_')
    else:
        d['preview_url'] = ''
        d['thumb_url'] = ''
    return d


def _pricelist_row_to_dict(row, images: list | None = None) -> dict:
    """项目行转 dict；images 由调用方装配，thumb/preview 取首图（列表卡兼容）。"""
    d = dict(row)
    imgs = images if images is not None else []
    d['images'] = imgs
    if imgs:
        d['thumb_url'] = imgs[0]['thumb_url']
        d['preview_url'] = imgs[0]['preview_url']
    else:
        d['thumb_url'] = ''
        d['preview_url'] = ''
    return d


def create_pricelist_item(data: dict) -> int:
    """新建价目表项目，返回新记录 ID。"""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO pricelist_items (category, name, price, price_max, unit, description) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (data.get('category', '默认'), data['name'], data.get('price', 0),
         data.get('price_max'), data.get('unit', ''), data.get('description', ''))
    )
    iid = cur.lastrowid
    conn.commit()
    conn.close()
    return iid


def update_pricelist_item(iid: int, data: dict) -> bool:
    """按列白名单更新价目表项目。"""
    sets = []
    params = []
    for k, v in data.items():
        if k in PRICELIST_UPDATABLE_COLUMNS:
            sets.append(f"{k} = ?")
            params.append(v)
    if not sets:
        return False
    conn = get_db()
    cur = conn.execute(
        f"UPDATE pricelist_items SET {', '.join(sets)}, "
        "updated_at = datetime('now','localtime') WHERE id = ?",
        (*params, iid)
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def get_pricelist_item(iid: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM pricelist_items WHERE id = ?", (iid,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    img_rows = conn.execute(
        "SELECT * FROM pricelist_images WHERE item_id = ? ORDER BY sort_order, id", (iid,)
    ).fetchall()
    conn.close()
    return _pricelist_row_to_dict(row, [_pricelist_image_to_dict(r) for r in img_rows])


def list_pricelist_items() -> list[dict]:
    """项目列表（按分类 → 排序 → id）；例图单次查询按 item_id 分组装配（防 N+1）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM pricelist_items ORDER BY category, sort_order, id"
    ).fetchall()
    img_rows = conn.execute(
        "SELECT * FROM pricelist_images ORDER BY item_id, sort_order, id"
    ).fetchall()
    conn.close()
    by_item: dict[int, list] = {}
    for r in img_rows:
        by_item.setdefault(r['item_id'], []).append(_pricelist_image_to_dict(r))
    return [_pricelist_row_to_dict(r, by_item.get(r['id'], [])) for r in rows]


_PRICELIST_CATEGORY_ORDER_KEY = 'pricelist_category_order'


def get_pricelist_category_order() -> list:
    """价目表分类显示顺序（settings 表逗号分隔序列）。
    空库 / 缺失 / 空串 → 返回 []；未列入的新分类由调用方追加末尾。
    """
    raw = get_all_settings().get(_PRICELIST_CATEGORY_ORDER_KEY, '')
    if not raw:
        return []
    return [c for c in raw.split(',') if c]


def set_pricelist_category_order(categories: list) -> None:
    """写入价目表分类显示顺序（逗号分隔序列到 settings 表）。"""
    val = ','.join(str(c) for c in categories)
    update_settings({_PRICELIST_CATEGORY_ORDER_KEY: val})


def delete_pricelist_item(iid: int) -> dict | None:
    """删除价目表项目，返回被删行（含 example_image_path，供调用方清理 uploads）。"""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM pricelist_items WHERE id = ?", (iid,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("DELETE FROM pricelist_items WHERE id = ?", (iid,))
    conn.commit()
    conn.close()
    return dict(row)


def reorder_pricelist(ids: list[int]) -> None:
    """拖拽排序：事务内按数组顺序写 sort_order（同分类内排序）。"""
    if not ids:
        return
    with transaction() as conn:
        for idx, iid in enumerate(ids):
            conn.execute(
                "UPDATE pricelist_items SET sort_order = ? WHERE id = ?",
                (idx, iid)
            )


# ── 价目表例图（pricelist_images：每项目最多 PRICELIST_IMAGE_LIMIT 张）──

def get_pricelist_images(item_id: int) -> list[dict]:
    """项目全部例图，按 sort_order/id 升序。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM pricelist_images WHERE item_id = ? ORDER BY sort_order, id",
        (item_id,)).fetchall()
    conn.close()
    return [_pricelist_image_to_dict(r) for r in rows]


def count_pricelist_images(item_id: int) -> int:
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM pricelist_images WHERE item_id = ?", (item_id,)
    ).fetchone()[0]
    conn.close()
    return n


def add_pricelist_image(item_id: int, image_path: str) -> int:
    """追加例图记录，sort_order 取当前最大值+1，返回新记录 id。"""
    conn = get_db()
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM pricelist_images WHERE item_id = ?",
        (item_id,)).fetchone()
    next_sort = row['next'] if row else 0
    cur = conn.execute(
        "INSERT INTO pricelist_images (item_id, image_path, sort_order) VALUES (?, ?, ?)",
        (item_id, image_path, next_sort))
    conn.commit()
    image_id = cur.lastrowid
    conn.close()
    return image_id


def get_pricelist_image(image_id: int) -> dict | None:
    """按 id 取单条例图记录（含派生 URL）。"""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM pricelist_images WHERE id = ?", (image_id,)
    ).fetchone()
    conn.close()
    return _pricelist_image_to_dict(row) if row else None


def delete_pricelist_image(image_id: int) -> dict | None:
    """删除例图记录，返回被删行（含 item_id/image_path，供调用方清理文件）。"""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM pricelist_images WHERE id = ?", (image_id,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("DELETE FROM pricelist_images WHERE id = ?", (image_id,))
    conn.commit()
    conn.close()
    return dict(row)


def reorder_pricelist_images(ids: list[int]) -> None:
    """例图拖拽排序：事务内按数组顺序写 sort_order（同项目内排序）。"""
    if not ids:
        return
    with transaction() as conn:
        for idx, img_id in enumerate(ids):
            conn.execute(
                "UPDATE pricelist_images SET sort_order = ? WHERE id = ?",
                (idx, img_id)
            )


def get_pricelist_meta() -> dict:
    """价目表菜单元信息 {title, note}（settings 键 pricelist_meta，JSON 存储）。"""
    raw = get_all_settings().get('pricelist_meta', '')
    try:
        meta = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        logging.warning('pricelist_meta JSON 损坏，回退空值: %r', raw)
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return {'title': meta.get('title', ''), 'note': meta.get('note', '')}


# ═══════════════════════════════════════════════════════════
# Spec 23 小票打印机（草稿 / 模板 / 计算）
# ═══════════════════════════════════════════════════════════

# 草稿默认结构：meta 含 order_id 预留位（§3.9 订单快照挂载点，不消费）
DEFAULT_RECEIPT_META = {
    'shop_name': '', 'subtitle': '', 'order_no': '', 'order_date': '',
    'contact': '', 'footer': '感谢惠顾', 'multiplier': 1,
    # 2026-08-13 用户需求 2b：整单倍率行文案（mult_expr 的 {n} 为数值占位符）
    'mult_label': '倍率', 'mult_expr': '×{n}',
    'discount_type': 'none', 'discount_value': 0,  # Spec 24：双形态（amount 金额 / rate 中文折数）
    'deposit': 0, 'order_id': None,
}

# Spec 24：单品倍率快捷预设默认值（编辑页可增删改名改值）
DEFAULT_RECEIPT_MULT_PRESETS = [
    {'label': '商用', 'value': 2},
    {'label': '买断', 'value': 3},
]
DEFAULT_RECEIPT_STYLE = {
    'preset': 'list', 'paper': '#fdfcf8', 'ink': '#1a1a1a',
    'bg_path': '', 'image_path': '', 'image_mode': 'dither',
    # 2026-08-13 用户需求 3：footer 插图（总计与感谢语之间）
    'footer_image_path': '', 'footer_image_mode': 'color',
    'barcode': True, 'zigzag': True,
}


def default_receipt_draft() -> dict:
    """从未保存过的默认草稿：1 空行 + 默认 meta/style。"""
    return {
        'items': [{'name': '', 'price': 0, 'qty': 1, 'is_gift': False, 'extras': [],
                   'multiplier': 1, 'mult_label': '', 'discount_type': 'none', 'discount_value': 0}],
        'meta': dict(DEFAULT_RECEIPT_META),
        'style': dict(DEFAULT_RECEIPT_STYLE),
    }


def get_receipt_draft() -> dict:
    """读当前草稿：settings receipt_draft 键（meta+style）+ receipt_items 组装嵌套。

    从未保存过 → 默认 1 空行草稿；已保存过空票 → items 保持 []（区分依据 settings 键是否存在）。
    """
    raw = get_all_settings().get('receipt_draft', '')
    has_saved = bool(raw)
    meta, style = dict(DEFAULT_RECEIPT_META), dict(DEFAULT_RECEIPT_STYLE)
    if raw:
        try:
            saved = json.loads(raw)
            meta.update({k: v for k, v in (saved.get('meta') or {}).items()
                         if k in DEFAULT_RECEIPT_META})
            style.update({k: v for k, v in (saved.get('style') or {}).items()
                          if k in DEFAULT_RECEIPT_STYLE})
            # Spec 24 旧数据归一：旧 discount（金额）键 → discount_type='amount' + discount_value
            old_disc = (saved.get('meta') or {}).get('discount')
            if old_disc and meta.get('discount_type') == 'none':
                meta['discount_type'] = 'amount'
                meta['discount_value'] = float(old_disc or 0)
        except (TypeError, ValueError):
            logging.warning('receipt_draft JSON 损坏，回退默认值: %r', raw[:120])
    conn = get_db()
    rows = conn.execute("SELECT * FROM receipt_items ORDER BY sort_order, id").fetchall()
    conn.close()
    items = []
    mains = [r for r in rows if r['parent_id'] is None]
    for m in mains:
        extras = [{'name': c['name'], 'price': c['price'], 'qty': c['qty']}
                  for c in rows if c['parent_id'] == m['id']]
        items.append({'name': m['name'], 'price': m['price'], 'qty': m['qty'],
                      'is_gift': bool(m['is_gift']), 'extras': extras,
                      'multiplier': m['multiplier'], 'mult_label': m['mult_label'],
                      'discount_type': m['discount_type'], 'discount_value': m['discount_value']})
    if not items and not has_saved:
        items = default_receipt_draft()['items']
    return {'items': items, 'meta': meta, 'style': style}


def save_receipt_draft(draft: dict) -> None:
    """整票保存（D4）：事务内全删全插 receipt_items + settings 写 meta/style JSON。"""
    with transaction() as conn:
        conn.execute("DELETE FROM receipt_items")
        for idx, item in enumerate(draft.get('items') or []):
            cur = conn.execute(
                "INSERT INTO receipt_items (name, price, qty, parent_id, is_gift, "
                "multiplier, mult_label, discount_type, discount_value, sort_order) "
                "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                (item['name'], float(item.get('price') or 0), float(item.get('qty') or 1),
                 1 if item.get('is_gift') else 0,
                 float(item.get('multiplier') or 1), str(item.get('mult_label') or ''),
                 str(item.get('discount_type') or 'none'), float(item.get('discount_value') or 0),
                 idx))
            pid = cur.lastrowid
            for ex in item.get('extras') or []:
                conn.execute(
                    "INSERT INTO receipt_items (name, price, qty, parent_id, is_gift, sort_order) "
                    "VALUES (?, ?, ?, ?, 0, 0)",
                    (ex['name'], float(ex.get('price') or 0), float(ex.get('qty') or 1), pid))
        update_settings({'receipt_draft': json.dumps(
            {'meta': draft.get('meta') or {}, 'style': draft.get('style') or {}},
            ensure_ascii=False)}, conn=conn)


def calc_receipt_totals(items: list, meta: dict) -> dict:
    """Spec 24 冻结公式（与前端 receipt.js rcCalc 同口径）。

    单品小计 =（price×qty + Σextras）×单品倍率 ×单品折扣（amount 直减 / rate 中文折数/10）；
    赠品恒 0 不参与倍率折扣；合计 → ×meta.multiplier（作用于全部制品）→ −整体折扣 → −定金。
    返回 has_* 条件行开关。
    """
    total = 0.0
    for it in items:
        if it.get('is_gift'):
            continue
        subtotal = float(it.get('price') or 0) * float(it.get('qty') or 0)
        for ex in it.get('extras') or []:
            subtotal += float(ex.get('price') or 0) * float(ex.get('qty') or 0)
        subtotal *= float(it.get('multiplier') or 1)
        if it.get('discount_type') == 'amount':
            subtotal = max(0.0, subtotal - float(it.get('discount_value') or 0))
        elif it.get('discount_type') == 'rate':
            subtotal *= float(it.get('discount_value') or 0) / 10
        total += subtotal
    multiplier = float(meta.get('multiplier') or 1)
    disc_type = str(meta.get('discount_type') or 'none')
    disc_value = float(meta.get('discount_value') or 0)
    deposit = float(meta.get('deposit') or 0)
    multed = total * multiplier
    if disc_type == 'amount':
        grand = multed - disc_value
    elif disc_type == 'rate':
        grand = multed * disc_value / 10
    else:
        grand = multed
    return {'total': total, 'multed': multed, 'grand': grand,
            'balance': grand - deposit,
            'has_mult': multiplier != 1,
            'has_discount': disc_value > 0 and disc_type in ('amount', 'rate'),
            'has_deposit': deposit > 0}


def get_receipt_mult_presets() -> list[dict]:
    """Spec 24 单品倍率快捷预设（settings 键 receipt_mult_presets，损坏/缺失回退默认）。"""
    raw = get_all_settings().get('receipt_mult_presets', '')
    try:
        data = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        logging.warning('receipt_mult_presets JSON 损坏，回退默认: %r', raw[:120])
        data = None
    if not isinstance(data, list) or not data:
        return [dict(p) for p in DEFAULT_RECEIPT_MULT_PRESETS]
    out = []
    for p in data:
        if isinstance(p, dict) and str(p.get('label') or '').strip():
            try:
                out.append({'label': str(p['label']).strip()[:20], 'value': float(p.get('value') or 1)})
            except (TypeError, ValueError):
                continue
    return out or [dict(p) for p in DEFAULT_RECEIPT_MULT_PRESETS]


def save_receipt_mult_presets(presets: list[dict]) -> None:
    """保存单品倍率预设列表（整体覆盖）。"""
    update_settings({'receipt_mult_presets': json.dumps(presets or [], ensure_ascii=False)})


def list_receipt_templates() -> list[dict]:
    """模板列表（config_json 解析为 config 字段，损坏回退空 dict）。"""
    conn = get_db()
    rows = conn.execute("SELECT * FROM receipt_templates ORDER BY id DESC").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d['config'] = json.loads(d.get('config_json') or '{}')
        except (TypeError, ValueError):
            logging.warning('receipt_template #%s config_json 损坏', d.get('id'))
            d['config'] = {}
        out.append(d)
    return out


def create_receipt_template(name: str, config: dict) -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO receipt_templates (name, config_json) VALUES (?, ?)",
        (name, json.dumps(config, ensure_ascii=False)))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def get_receipt_template(tid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM receipt_templates WHERE id = ?", (tid,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d['config'] = json.loads(d.get('config_json') or '{}')
    except (TypeError, ValueError):
        d['config'] = {}
    return d


def delete_receipt_template(tid: int) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM receipt_templates WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def receipt_bg_referenced(bg_basename: str) -> bool:
    """背景文件是否被任一模板 config_json 引用（§3.7 删前检查）。"""
    if not bg_basename:
        return False
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) FROM receipt_templates WHERE config_json LIKE ?",
        (f'%{bg_basename}%',)).fetchone()
    conn.close()
    return (row[0] or 0) > 0


# ═══════════════════════════════════════════════════════════
# 自动初始化
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    init_db()
    print("数据库初始化完成！")

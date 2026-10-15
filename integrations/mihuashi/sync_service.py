"""通过 oimimo 业务服务执行安全、幂等的本地订单同步。"""

import hashlib
import json
from contextlib import nullcontext
from datetime import datetime

import db
from models import OrderCreate

from .schemas import ExternalOrder


def _hash(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _source_hash(order):
    return _hash({
        'project_name': order.project_name,
        'platform_url': order.platform_url,
        'deadline': order.deadline.isoformat() if order.deadline else None,
        'quoted_amount': format(order.quoted_amount, 'f') if order.quoted_amount is not None else None,
        'platform_status': order.platform_status,
    })


def _fields_to_sync(order):
    return {
        'project_name': order.project_name,
        'platform_url': order.platform_url,
        'page_deadline': order.deadline.isoformat() if order.deadline else None,
    }


def upsert_order(external_order, allow_create=False, dry_run=False):
    """幂等 upsert；金额、排期、绘制阶段、收款和本地笔记由 oimimo 管理。"""
    order = external_order if isinstance(external_order, ExternalOrder) else ExternalOrder.model_validate(external_order)
    source_hash = _source_hash(order)
    fields = _fields_to_sync(order)
    title_hash = _hash(fields['project_name'])
    url_hash = _hash(fields['platform_url']) if fields['platform_url'] else None
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = db.get_db() if dry_run else None
    try:
        with db.transaction() if not dry_run else nullcontext(conn) as conn:
            link = conn.execute(
                'SELECT * FROM external_order_links WHERE platform=? AND external_order_id=?',
                (order.platform, order.external_order_id),
            ).fetchone()

            if not link:
                if not allow_create:
                    return {'action': 'skipped', 'local_order_id': None, 'conflicts': []}
                if dry_run:
                    preview_fields = ['project_name', 'platform_url', 'page_deadline']
                    if order.quoted_amount is not None:
                        preview_fields.append('quoted_amount')
                    if order.platform_status is not None:
                        preview_fields.append('platform_status')
                    return {'action': 'would_create', 'local_order_id': None,
                            'fields': preview_fields, 'conflicts': []}
                create_data = OrderCreate(
                    project_name=order.project_name,
                    source='mihuashi',
                    platform_url=order.platform_url,
                    page_deadline=fields['page_deadline'],
                ).model_dump(exclude_unset=True)
                local_id = db.create_order(create_data, conn=conn)
                conn.execute(
                    """INSERT INTO external_order_links
                       (platform, external_order_id, local_order_id, last_source_hash,
                        project_name_hash, platform_url_hash, page_deadline, quoted_amount,
                        platform_status, last_seen_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (order.platform, order.external_order_id, local_id, source_hash,
                     title_hash, url_hash, fields['page_deadline'],
                     format(order.quoted_amount, 'f') if order.quoted_amount is not None else None,
                     order.platform_status, now, now),
                )
                return {'action': 'created', 'local_order_id': local_id, 'conflicts': []}

            if link['local_order_id'] is None:
                return {'action': 'conflict', 'local_order_id': None,
                        'conflicts': ['local_order_deleted']}

            local = db.get_order(link['local_order_id'], conn=conn)
            if not local:
                return {'action': 'conflict', 'local_order_id': link['local_order_id'],
                        'conflicts': ['local_order_missing']}
            if int(local.get('is_archived') or 0):
                return {'action': 'conflict', 'local_order_id': local['id'],
                        'conflicts': ['local_order_archived']}

            updates = {}
            conflicts = []

            if title_hash != link['project_name_hash']:
                local_title_hash = _hash(local.get('project_name') or '')
                if local_title_hash == title_hash:
                    next_title_hash = title_hash
                elif link['project_name_hash'] is None or local_title_hash == link['project_name_hash']:
                    updates['project_name'] = order.project_name
                    next_title_hash = title_hash
                else:
                    conflicts.append('project_name')
                    next_title_hash = link['project_name_hash']
            else:
                next_title_hash = link['project_name_hash']

            if url_hash is not None and url_hash != link['platform_url_hash']:
                local_url = local.get('platform_url') or None
                if link['platform_url_hash'] is None:
                    if not local_url:
                        updates['platform_url'] = order.platform_url
                        next_url_hash = url_hash
                    else:
                        conflicts.append('platform_url')
                        next_url_hash = link['platform_url_hash']
                elif _hash(local_url) == link['platform_url_hash']:
                    updates['platform_url'] = order.platform_url
                    next_url_hash = url_hash
                else:
                    conflicts.append('platform_url')
                    next_url_hash = link['platform_url_hash']
            else:
                next_url_hash = link['platform_url_hash']

            remote_deadline = fields['page_deadline']
            old_deadline = link['page_deadline']
            if remote_deadline is not None and remote_deadline != old_deadline:
                if local.get('page_deadline') == remote_deadline:
                    next_deadline = remote_deadline
                elif (old_deadline is None and not local.get('page_deadline')) or local.get('page_deadline') == old_deadline:
                    updates['page_deadline'] = remote_deadline
                    next_deadline = remote_deadline
                else:
                    conflicts.append('page_deadline')
                    next_deadline = old_deadline
            else:
                next_deadline = old_deadline

            if updates and not dry_run:
                ok, error = db.update_order(local['id'], updates, conn=conn)
                if not ok:
                    raise RuntimeError('oimimo update rejected: {}'.format(error))

            quoted_amount = (format(order.quoted_amount, 'f')
                             if order.quoted_amount is not None else link['quoted_amount'])
            platform_status = (order.platform_status
                               if order.platform_status is not None else link['platform_status'])
            changed_fields = list(updates)
            if quoted_amount != link['quoted_amount']:
                changed_fields.append('quoted_amount')
            if platform_status != link['platform_status']:
                changed_fields.append('platform_status')
            action = 'conflict' if conflicts else ('updated' if changed_fields else 'unchanged')
            if dry_run:
                return {'action': action, 'local_order_id': local['id'],
                        'fields': changed_fields, 'conflicts': conflicts}
            conn.execute(
                """UPDATE external_order_links
                   SET last_source_hash=?, project_name_hash=?, platform_url_hash=?,
                       page_deadline=?, quoted_amount=?, platform_status=?, last_seen_at=?, updated_at=?
                   WHERE id=?""",
                (source_hash, next_title_hash, next_url_hash, next_deadline,
                 quoted_amount,
                 platform_status, now, now, link['id']),
            )
            return {'action': action, 'local_order_id': local['id'], 'conflicts': conflicts}
    finally:
        if dry_run:
            conn.close()

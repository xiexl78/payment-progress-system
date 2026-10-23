# -*- coding: utf-8 -*-
"""操作审计留痕（P0-02）

设计要点（按架构师 review-audit 意见修订）
-----------------------------------------
- **与业务同事务**：``record()`` 只 ``db.session.add(entry)``，不 flush、不 commit、
  不使用 SAVEPOINT；由路由在业务提交点统一 ``db.session.commit()``。
  这样审计与业务同生共死，不会出现"靠偶然行为落库"或"审计单独落库"。
- **关键动作失败即失败**：默认 ``critical=True``，审计写失败向上抛，业务一起回滚；
  非关键动作传 ``critical=False``，只记录日志并返回 False（尽量不阻塞业务）。
- **不建外键**：项目/用户/合同被物理删除后日志仍完整。
- **自动取操作人**：当前登录用户快照（id/姓名/角色）、IP、UA；X-Forwarded-For
  仅在 ``TRUST_PROXY_HEADERS=True``（可信反向代理）时采用，否则用 remote_addr。
- ``ts`` 统一 UTC。
"""

import json
import logging
from datetime import date, datetime
from decimal import Decimal

from flask import current_app, has_request_context, request, session

from .models import AuditLog, User, db

__all__ = ['record', 'snapshot', 'action_options', 'KNOWN_ACTIONS']

#: 系统内已知动作（审计页筛选下拉使用固定集合，避免被任意数据/合成记录污染）
KNOWN_ACTIONS = (
    'auth.login.success', 'auth.login.failed', 'auth.logout', 'auth.denied',
    'auth.login.locked', 'auth.login.force_change', 'auth.csrf_blocked',
    'project.create', 'project.edit', 'project.status', 'project.delete',
    'project.restore', 'project.trial', 'item.create',
    'node.apply', 'node.invoice', 'node.pay',
    'acceptance.add',
    'contract.upload', 'contract.confirm', 'contract.confirm.failed',
    'contract.modify', 'contract.modify.blocked', 'contract.rerun',
    'user.create', 'user.disable', 'user.enable', 'user.delete',
    'export.project', 'export.reports', 'export.audit', 'import.excel',
)

logger = logging.getLogger('payment.audit')
_UA_MAX = 256


def _audit_table_ready():
    """非关键审计写前的轻量存在性检查：表被删/未建时降级跳过。"""
    try:
        from sqlalchemy import inspect
        return inspect(db.engine).has_table(AuditLog.__tablename__)
    except Exception:
        return False


def _coerce(value):
    """把快照值转换为可 JSON 序列化的形式。"""
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, (bytes, bytearray)):
        return value.decode('utf-8', 'replace')
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def _dump(payload):
    if payload is None:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_coerce)
    except Exception:
        try:
            return json.dumps({'_repr': repr(payload)}, ensure_ascii=False)
        except Exception:
            return None


def snapshot(obj, fields=None):
    """生成对象快照 dict（默认取全部列；fields 可限定关注字段）。"""
    if obj is None:
        return None
    try:
        columns = obj.__table__.columns
    except AttributeError:
        return _coerce(obj)
    keys = list(fields) if fields else [c.name for c in columns]
    data = {}
    for key in keys:
        try:
            data[key] = _coerce(getattr(obj, key))
        except Exception:
            data[key] = None
    return data


def _current_actor():
    """返回 (actor_id, actor_name, actor_role)；无会话时全为 None。"""
    if not has_request_context():
        return None, None, None
    try:
        uid = session.get('user_id')
        if not uid:
            return None, None, None
        user = db.session.get(User, uid)
        if user is None:
            return uid, None, None
        return user.id, user.display_name, user.role
    except Exception:
        return None, None, None


def _request_meta():
    if not has_request_context():
        return None, None
    try:
        ip = request.remote_addr
        if current_app.config.get('TRUST_PROXY_HEADERS'):
            forwarded = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
            ip = forwarded or ip
        ua = (request.user_agent.string or '') if request.user_agent else ''
        return (ip or None), (ua[:_UA_MAX] or None)
    except Exception:
        return None, None


def record(action, object_type=None, object_id=None, project_id=None,
           before=None, after=None, result='ok', actor=None, critical=True):
    """把一条审计日志加入当前事务；由调用方 commit。

    ``critical=True``（默认）：写入过程出错直接抛出，业务随事务一起失败；
    ``critical=False``：记录日志并返回 False，尽量不影响业务。
    返回 True/False 表示是否已加入会话（不代表已提交）。
    """
    try:
        if not critical and not _audit_table_ready():
            logger.warning('审计表不可用，跳过低优先级审计：action=%s', action)
            return False
        actor_id, actor_name, actor_role = actor if actor else _current_actor()
        ip, ua = _request_meta()
        entry = AuditLog(
            ts=datetime.utcnow(),
            actor_id=actor_id,
            actor_name=actor_name or None,
            actor_role=actor_role or None,
            action=str(action)[:64],
            object_type=(str(object_type)[:32] if object_type else None),
            object_id=object_id if isinstance(object_id, int) else None,
            project_id=project_id if isinstance(project_id, int) else None,
            before_json=_dump(before),
            after_json=_dump(after),
            ip=ip,
            ua=ua,
            result=(str(result)[:16] if result else 'ok'),
        )
        db.session.add(entry)
        return True
    except Exception:
        logger.exception('写审计失败：action=%s object_type=%s', action, object_type)
        if critical:
            raise
        return False


def action_options(current=None):
    """审计页筛选下拉的动作列表：固定白名单 + 当前筛选值（便于回显）。"""
    options = list(KNOWN_ACTIONS)
    if current and current not in options:
        options.append(current)
    return options

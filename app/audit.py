# -*- coding: utf-8 -*-
"""操作审计留痕（P0-02）

设计要点
--------
- **只追加**：审计记录不提供更新/删除入口，业务代码只调用 :func:`record`；
- **不建外键**：项目/用户/合同被物理删除后，审计记录仍完整可查；
- **写日志失败不影响业务**：审计写入放在 SAVEPOINT 中，失败只回滚该
  SAVEPOINT 并写 stderr，业务事务继续；审计成功则与业务同事务提交；
- **自动取操作人**：从 Flask session 取 ``user_id`` 查询用户名快照，
  登录失败等无会话场景由调用方通过 ``before/after`` 记录尝试信息；
- ``ts`` 统一 UTC，页面展示时标注 UTC。

使用示例::

    from .audit import record, snapshot
    record('project.edit', 'project', project.id, project.id,
           before={'name': old_name}, after={'name': project.name})
"""

import json
import sys
import traceback
from datetime import date, datetime
from decimal import Decimal

from flask import has_request_context, request, session

from .models import AuditLog, User, db

__all__ = ['record', 'snapshot', 'action_options']

_UA_MAX = 256


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
    if not has_request_context():
        return None, None
    try:
        uid = session.get('user_id')
        if not uid:
            return None, None
        user = db.session.get(User, uid)
        if user is None:
            return uid, None
        return user.id, user.display_name
    except Exception:
        return None, None


def _request_meta():
    if not has_request_context():
        return None, None
    try:
        forwarded = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
        ip = forwarded or request.remote_addr
        ua = (request.user_agent.string or '') if request.user_agent else ''
        return (ip or None), (ua[:_UA_MAX] or None)
    except Exception:
        return None, None


def record(action, object_type=None, object_id=None, project_id=None,
           before=None, after=None, result='ok', actor=None):
    """写一条审计日志；任何异常都被吞掉并写 stderr，不影响业务事务。

    Parameters
    ----------
    action : str
        动作编码，如 ``project.create`` / ``node.pay`` / ``auth.login.failed``。
    object_type : str | None
        ``project`` / ``item`` / ``node`` / ``contract`` / ``user`` / ``auth``。
    object_id : int | None
        对象主键。
    project_id : int | None
        所属项目，便于按项目检索。
    before / after : dict | None
        关注字段快照（金额/状态/日期等）。
    result : str
        ``ok`` / ``failed`` / ``denied`` / ``blocked``。
    actor : tuple | None
        可选 ``(actor_id, actor_name)``；默认自动取当前登录用户。
    """
    try:
        actor_id, actor_name = actor if actor else _current_actor()
        ip, ua = _request_meta()
        entry = AuditLog(
            ts=datetime.utcnow(),
            actor_id=actor_id,
            actor_name=(actor_name or None),
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
        with db.session.begin_nested():
            db.session.add(entry)
            db.session.flush()
    except Exception:
        try:
            print('[audit] 写审计失败（已忽略，不影响业务）：\n%s'
                  % traceback.format_exc(), file=sys.stderr)
        except Exception:
            pass


def action_options():
    """返回数据库中已出现过的动作编码（供筛选下拉框）。"""
    try:
        rows = (db.session.query(AuditLog.action)
                .distinct().order_by(AuditLog.action).all())
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []

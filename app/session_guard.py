# -*- coding: utf-8 -*-
"""服务端会话管理（批次 4b）

Flask 默认是签名 Cookie 会话：登出后旧 Cookie 在过期前仍然可用。
本模块用 ``user_sessions`` 表登记每次登录的随机会话 token，实现：

- 登出：删除当前会话行 → 旧 Cookie 立即失效；
- 改密/重置/停用：删除该用户全部会话 → 其它设备/旧 Cookie 失效；
- 敏感操作（改金额等）：轮换当前会话 token（删旧建新）→ 旧 Cookie 失效；
- 会话 8 小时过期（``PERMANENT_SESSION_LIFETIME``）。

多进程部署时该表天然共享（SQLite/未来 MSSQL 均可），优于内存方案。
"""

import secrets
from datetime import datetime, timedelta

from flask import current_app, has_request_context, request, session

from .models import UserSession, db

SESSION_KEY = 'sid'
_UA_MAX = 256


def _lifetime():
    lifetime = current_app.config.get('PERMANENT_SESSION_LIFETIME')
    return lifetime if isinstance(lifetime, timedelta) else timedelta(hours=8)


def _meta():
    if not has_request_context():
        return None, None
    try:
        ip = request.remote_addr
        ua = (request.user_agent.string or '') if request.user_agent else ''
        return (ip or None), (ua[:_UA_MAX] or None)
    except Exception:
        return None, None


def _new_row(user_id):
    ip, ua = _meta()
    token = secrets.token_urlsafe(32)
    row = UserSession(token=token, user_id=user_id,
                      expires_at=datetime.utcnow() + _lifetime(), ip=ip, ua=ua)
    db.session.add(row)
    return token


def create_session(user):
    """登录成功：创建会话行并写入 session。"""
    token = _new_row(user.id)
    session[SESSION_KEY] = token
    session['user_id'] = user.id
    session.permanent = True
    return token


def current_token():
    return session.get(SESSION_KEY)


def load_valid_session(token, user_id=None):
    """校验 token：存在、未过期、属主匹配；失效返回 None。"""
    if not token:
        return None
    row = UserSession.query.filter_by(token=token).first()
    if row is None:
        return None
    if row.expires_at is not None and row.expires_at < datetime.utcnow():
        db.session.delete(row)
        db.session.commit()
        return None
    if user_id is not None and row.user_id != user_id:
        return None
    return row


def revoke_current():
    """登出：删除当前会话行（旧 Cookie 失效）。"""
    token = session.get(SESSION_KEY)
    if token:
        UserSession.query.filter_by(token=token).delete(synchronize_session=False)
    session.clear()


def revoke_user(user_id):
    """改密/重置/停用：删除该用户全部会话。"""
    UserSession.query.filter_by(user_id=user_id).delete(synchronize_session=False)


def rotate_current(user_id):
    """敏感操作后轮换当前会话：删除旧 token、建新 token（旧 Cookie 失效）。"""
    token = session.get(SESSION_KEY)
    if token:
        UserSession.query.filter_by(token=token).delete(synchronize_session=False)
    new_token = _new_row(user_id)
    session[SESSION_KEY] = new_token
    session['user_id'] = user_id
    session.permanent = True
    return new_token

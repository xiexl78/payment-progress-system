# -*- coding: utf-8 -*-
"""数据库健康检查（批次 5：P0-03 友好降级 / /healthz）"""

from sqlalchemy import inspect

from .models import db

REQUIRED_TABLES = ('users', 'projects', 'project_items', 'payment_nodes')


def database_status():
    """返回 ``{'ok': bool, 'error': str|None, 'missing_tables': [...], 'path': str}``。"""
    status = {'ok': False, 'error': None, 'missing_tables': [], 'path': None}
    try:
        try:
            status['path'] = str(db.engine.url.database or '')
        except Exception:
            status['path'] = None
        db.session.execute(db.text('SELECT 1'))
        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names())
        missing = [name for name in REQUIRED_TABLES if name not in tables]
        status['missing_tables'] = missing
        if missing:
            status['error'] = '关键数据表缺失：%s（请先初始化或恢复数据库）' % '、'.join(missing)
            return status
        status['ok'] = True
        return status
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        status['error'] = str(exc)[:240] or '数据库不可用'
        return status

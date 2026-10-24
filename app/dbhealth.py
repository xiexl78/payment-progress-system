# -*- coding: utf-8 -*-
"""数据库健康检查（批次 5：P0-03 友好降级 / /healthz）"""

from sqlalchemy import inspect

from .models import db

REQUIRED_TABLES = ('users', 'projects', 'project_items', 'payment_nodes', 'payment_invoices')

#: 表已存在但缺列（旧库/草案库升级失败）同样视为不可用，否则页面会 503 而 /healthz 仍报 ok
REQUIRED_COLUMNS = {
    'payment_invoices': ('node_id', 'project_id', 'invoice_no', 'amount'),
    'payment_nodes': ('applied_amount', 'change_note'),
}


def database_status():
    """返回 ``{'ok': bool, 'error': str|None, 'missing_tables': [...], 'missing_columns': [...], 'path': str}``。"""
    status = {'ok': False, 'error': None, 'missing_tables': [], 'missing_columns': [], 'path': None}
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
        missing_columns = []
        for table, columns in REQUIRED_COLUMNS.items():
            have = {col['name'] for col in inspector.get_columns(table)}
            for column in columns:
                if column not in have:
                    missing_columns.append('%s.%s' % (table, column))
        status['missing_columns'] = missing_columns
        if missing_columns:
            status['error'] = '数据表缺少字段：%s（请用新版本启动以自动升级，或恢复备份）' % '、'.join(missing_columns)
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

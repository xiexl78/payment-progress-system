# -*- coding: utf-8 -*-
"""旧库结构自动升级（幂等，仅补列/补表/补索引）

背景：批次 3/4a 给既有表新增了列（is_active、updated_*、confirmed_*、
actor_role）并新增 audit_logs 表。SQLite 的 ``db.create_all()`` 只建缺失的
**表**，不会给已存在的表补列；直接启动旧库会报 ``no such column``。

本模块在应用启动时做一次幂等检查：
- 只对已存在的表执行 ``ALTER TABLE ... ADD COLUMN``；
- 缺失 audit_logs 表则创建；缺失索引则补建；
- 空库/损坏库不处理（交给 ``init_db.py`` / 运维恢复），避免影响现有错误处理流程。

生产升级仍以 Alembic 为准（阶段 2，见 docs/roadmap.md）；本模块只是兼容兜底。
"""

from sqlalchemy import inspect, text

from .models import AuditLog, db

#: 既有表缺失列的补丁（DDL 片段按 SQLite 语法，MSSQL 迁移阶段由 Alembic 接管）
_COLUMN_PATCHES = {
    'users': {
        'is_active': 'BOOLEAN NOT NULL DEFAULT 1',
    },
    'projects': {
        'updated_at': 'DATETIME',
        'updated_by': 'INTEGER',
    },
    'project_items': {
        'updated_at': 'DATETIME',
        'updated_by': 'INTEGER',
    },
    'payment_nodes': {
        'updated_at': 'DATETIME',
        'updated_by': 'INTEGER',
    },
    'contract_files': {
        'confirmed_by': 'INTEGER',
        'confirmed_at': 'DATETIME',
    },
}

#: audit_logs 后续版本补充的列
_AUDIT_COLUMN_PATCHES = {
    'actor_role': 'VARCHAR(16)',
}


def upgrade_schema(app):
    """幂等升级现有数据库结构；返回 {'applied': [...], 'skipped': bool}。"""
    result = {'applied': [], 'skipped': False}
    with app.app_context():
        try:
            inspector = inspect(db.engine)
            tables = set(inspector.get_table_names())
        except Exception:
            app.logger.warning('数据库不可读，跳过结构自动升级（请检查数据库或备份）')
            result['skipped'] = True
            return result
        if not tables:
            # 空库（尚未 init）或损坏库：保持原行为，由 init_db/运维处理
            result['skipped'] = True
            return result

        applied = result['applied']
        try:
            with db.engine.begin() as conn:
                for table, columns in _COLUMN_PATCHES.items():
                    if table not in tables:
                        continue
                    existing = {col['name'] for col in inspector.get_columns(table)}
                    for name, ddl in columns.items():
                        if name not in existing:
                            conn.execute(text('ALTER TABLE %s ADD COLUMN %s %s'
                                              % (table, name, ddl)))
                            applied.append('%s.%s' % (table, name))
                if 'audit_logs' in tables:
                    existing = {col['name'] for col in inspector.get_columns('audit_logs')}
                    for name, ddl in _AUDIT_COLUMN_PATCHES.items():
                        if name not in existing:
                            conn.execute(text('ALTER TABLE audit_logs ADD COLUMN %s %s'
                                              % (name, ddl)))
                            applied.append('audit_logs.%s' % name)

            if 'audit_logs' not in tables:
                AuditLog.__table__.create(bind=db.engine, checkfirst=True)
                applied.append('audit_logs(create)')
            for index in AuditLog.__table__.indexes:
                index.create(bind=db.engine, checkfirst=True)
        except Exception as exc:
            app.logger.error('数据库结构自动升级失败：%s', exc, exc_info=True)
            raise RuntimeError('数据库结构升级失败，请先备份数据库后手动迁移：%s' % exc)

        if applied:
            app.logger.warning('数据库结构自动升级完成：%s', ', '.join(applied))
    return result

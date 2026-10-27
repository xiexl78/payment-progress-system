# -*- coding: utf-8 -*-
"""CR-05 数据迁移：可配置质保期 / 截图识别 / 节点令牌消费表。

做三件事（幂等，可重复执行）：
1. 结构升级：交给 ``create_app()`` 启动时的 ``app.schema.upgrade_schema()``
   （补 ``projects.warranty_months``、``contract_files.source``，并自动建
   ``contract_screenshots`` / ``node_action_tokens`` 两张新表）；
2. 回填历史项目：``warranty_months`` 为空的行填 36（保持现有质保到期日不变，
   不重算 ``warranty_due_date``）；
3. 先备份再动库：默认把库文件复制到 ``<数据目录上级>/backups/pre-cr05/``，
   并做 ``PRAGMA integrity_check``，备份不通过则拒绝继续。

用法：
    python scripts/migrate_cr05.py                  # 默认 instance/
    python scripts/migrate_cr05.py --data-dir <DIR> # 指定数据目录（如 data-dev）
    python scripts/migrate_cr05.py --dry-run       # 只报告将要做的变更
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app import create_app  # noqa: E402
from app.models import Project, db  # noqa: E402
from app.schema import upgrade_schema  # noqa: E402

DB_NAME = 'payment_progress.db'
BACKFILL_MONTHS = 36


def _print(msg=''):
    print(msg, flush=True)


def _err(msg):
    print('[错误] ' + msg, file=sys.stderr, flush=True)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='CR-05 数据迁移（结构升级 + 质保期回填 + 备份）',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', default=None,
                        help='数据目录（默认 DATA_DIR 或项目 instance/）')
    parser.add_argument('--backup-dir', default=None,
                        help='备份根目录（默认 <数据目录上级>/backups/pre-cr05）')
    parser.add_argument('--dry-run', action='store_true', help='只报告，不写库')
    parser.add_argument('--no-backup', action='store_true',
                        help='跳过备份（仅排障用，风险自负）')
    return parser.parse_args(argv)


def resolve_data_dir(args):
    data_dir = args.data_dir or os.environ.get('DATA_DIR') \
        or os.path.join(PROJECT_ROOT, 'instance')
    data_dir = os.path.abspath(data_dir)
    return data_dir, os.path.join(data_dir, DB_NAME)


def backup_db(db_path, backup_dir):
    """复制库文件并校验完整性；返回备份路径。"""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    target = os.path.join(backup_dir, '%s-cr05-%s.bak' % (DB_NAME, stamp))
    shutil.copy2(db_path, target)
    conn = sqlite3.connect(target)
    try:
        ok = conn.execute('PRAGMA integrity_check').fetchone()
    finally:
        conn.close()
    if not ok or ok[0] != 'ok':
        raise RuntimeError('备份文件完整性校验失败：%r（未修改数据库）' % (ok,))
    return target


def collect_state(db_path):
    """只读统计：有项目的行数 / 质保期为空的行数 / 缺列情况。"""
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'projects' not in tables:
            return {'has_column': False, 'projects': 0, 'empty': 0}
        cols = {row[1] for row in conn.execute('PRAGMA table_info(projects)')}
        total = conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
        if 'warranty_months' in cols:
            empty = conn.execute(
                'SELECT COUNT(*) FROM projects WHERE warranty_months IS NULL').fetchone()[0]
        else:
            empty = total
        return {'has_column': 'warranty_months' in cols,
                'projects': total, 'empty': empty}
    finally:
        conn.close()


def build_app(data_dir):
    db_path = os.path.join(data_dir, DB_NAME)
    return create_app({
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + db_path.replace('\\', '/'),
        'DATA_DIR': data_dir,
        'UPLOAD_DIR': os.path.join(data_dir, 'uploads'),
        # 自己调用 upgrade_schema 以拿到 applied 明细（启动钩子会静默执行一遍）
        'SKIP_SCHEMA_UPGRADE': True,
    })


def migrate(data_dir, db_path):
    """执行结构升级 + 回填；返回 {'applied': [...], 'backfilled': N}。"""
    app = build_app(data_dir)
    result = {'applied': [], 'backfilled': 0}
    with app.app_context():
        applied = upgrade_schema(app)
        result['applied'] = list(applied.get('applied') or [])
        updated = db.session.query(Project).filter(
            Project.warranty_months.is_(None)).update(
                {Project.warranty_months: BACKFILL_MONTHS},
                synchronize_session=False)
        db.session.commit()
        result['backfilled'] = int(updated or 0)
        db.session.remove()
        db.engine.dispose()
    return result


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    data_dir, db_path = resolve_data_dir(args)

    _print('=' * 66)
    _print('CR-05 数据迁移')
    _print('  数据目录: %s' % data_dir)
    _print('  数据库  : %s' % db_path)
    _print('=' * 66)

    if not os.path.exists(db_path):
        _err('数据库不存在：%s（新库直接启动应用即可建表）' % db_path)
        return 1

    state = collect_state(db_path)
    _print('  现有项目: %d 个，质保期为空: %d 个（将回填 %d 个月）'
           % (state['projects'], state['empty'], BACKFILL_MONTHS))
    _print('  warranty_months 列: %s' % ('已存在' if state['has_column'] else '缺失（将新增）'))

    if args.dry_run:
        _print('[dry-run] 未做任何修改。')
        return 0

    if args.no_backup:
        _print('[跳过备份] --no-backup')
    else:
        backup_dir = args.backup_dir or os.environ.get('PAYMENT_BACKUP_DIR') \
            or os.path.join(os.path.dirname(data_dir), 'backups', 'pre-cr05')
        try:
            target = backup_db(db_path, backup_dir)
        except Exception as exc:
            _err('%s；数据库未做任何修改。' % exc)
            return 1
        _print('[备份] %s（完整性校验通过）' % target)

    try:
        result = migrate(data_dir, db_path)
    except Exception as exc:
        _err('迁移失败：%s（可用备份回滚：备份目录下 *.bak 覆盖数据库）' % exc)
        return 1

    if result['applied']:
        _print('[结构] 已升级：%s' % ', '.join(result['applied']))
    else:
        _print('[结构] 无需升级（已是最新）')
    _print('[回填] warranty_months=36：%d 个项目' % result['backfilled'])
    _print('[完成] 请重启服务并用 /healthz、/projects、项目详情页自检。')
    return 0


if __name__ == '__main__':
    sys.exit(main())

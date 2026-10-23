# -*- coding: utf-8 -*-
"""数据库初始化脚本：建表 + 迁移《付款进度表（新增动设备监测）》种子数据 + 默认用户。

用法（在工程根目录执行）：
    python scripts/init_db.py
        仅在库/表不存在时初始化；已有数据不动。
    python scripts/init_db.py --no-seed
        生产初始化：只建表结构与账号，不写演示项目（见下方 seed 规则）。
    python scripts/init_db.py --seed
        强制写入演示种子项目（即使在生产模式；演示/测试用）。
    python scripts/init_db.py --force
        重建：数据体检 → 在线备份并校验 → 输入库文件名二次确认 → 删除并重建。
    python scripts/init_db.py --force --yes
        非交互重建（计划任务/自动化），仍强制自动备份。
    python scripts/init_db.py --force --no-backup --yes
        显式跳过备份（仅排障用，高危；会打印 WARN）。

演示数据（seed）规则：
- `--seed` / `--no-seed` 显式指定优先；
- 未指定时：`APP_ENV=production`（环境变量）默认**不写**演示项目；其余环境默认写入（兼容开发与测试）；
- 只认环境变量 `APP_ENV`，不读 create_app 配置默认值，避免测试被误判为生产。

安全约定：
- 备份失败、备份校验失败、-wal 非空（服务可能仍在运行）→ 默认中止，数据库保持原样；
- 删除前必须完成备份 + 二次确认；
- 默认口令从环境变量读取（PAYMENT_ADMIN_PASSWORD / PAYMENT_OPERATOR_PASSWORD /
  PAYMENT_VIEWER_PASSWORD），缺省仍用历史上线前账号，但会打印 WARN，上线前必须改密。

退出码：0 成功；1 中止或失败；2 参数错误。
"""
import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
from datetime import date, datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, PROJECT_ROOT)

from app import create_app, NODE_TEMPLATES as TEMPLATES  # noqa: E402
from app.models import db, User, Project, ProjectItem, PaymentNode  # noqa: E402

DB_NAME = 'payment_progress.db'
COUNT_TABLES = [
    'users', 'projects', 'project_items', 'payment_nodes',
    'payment_applications', 'payment_records', 'contract_files', 'acceptance_records',
]

SEED_USERS = [
    ('admin', '系统管理员', 'admin', 'PAYMENT_ADMIN_PASSWORD', 'admin123'),
    ('jbr', '经办人', 'operator', 'PAYMENT_OPERATOR_PASSWORD', '123456'),
    ('viewer', '查看人员', 'viewer', 'PAYMENT_VIEWER_PASSWORD', '123456'),
]


def _now():
    return datetime.now()


def _print(msg=''):
    print(msg)


def _warn(msg):
    print('[WARN] %s' % msg)


def _err(msg):
    print('[ERROR] %s' % msg, file=sys.stderr)


# ---------------- 数据库体检 ----------------

def _connect_readonly(db_path):
    uri = 'file:' + db_path.replace('\\', '/') + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def collect_stats(db_path):
    """只读统计现有库，用于删除前展示"将丢失的数据"；库不存在返回 None。"""
    if not os.path.exists(db_path):
        return None
    stats = {'bytes': os.path.getsize(db_path), 'counts': {}, 'paid_total': None}
    conn = None
    try:
        conn = _connect_readonly(db_path)
        for table in COUNT_TABLES:
            try:
                stats['counts'][table] = conn.execute(
                    'SELECT COUNT(*) FROM "%s"' % table).fetchone()[0]
            except sqlite3.Error:
                stats['counts'][table] = None
        for column in ('paid_amount', 'invoice_amount'):
            try:
                value = conn.execute(
                    'SELECT COALESCE(SUM(%s), 0) FROM payment_nodes' % column).fetchone()[0]
                stats['paid_total' if column == 'paid_amount' else 'invoice_total'] = str(value)
            except sqlite3.Error:
                pass
    finally:
        if conn is not None:
            conn.close()
    return stats


def _print_stats(stats):
    if not stats:
        _print('[init] 数据库不存在，无需备份/确认，直接初始化。')
        return
    counts = stats.get('counts') or {}
    _print('[init] 现有数据库：%s（%.1f KB）' % (stats.get('path', DB_NAME),
                                                (stats.get('bytes') or 0) / 1024.0))
    _print('[init] 将丢失的数据：%s' % ', '.join(
        '%s=%s' % (k, counts.get(k)) for k in COUNT_TABLES if counts.get(k)))
    if stats.get('paid_total') is not None:
        _print('[init] 节点已付合计=%s，已开票合计=%s' % (
            stats.get('paid_total'), stats.get('invoice_total')))


def check_wal(data_dir):
    wal = os.path.join(data_dir, DB_NAME + '-wal')
    if os.path.exists(wal) and os.path.getsize(wal) > 0:
        return os.path.getsize(wal)
    return 0


# ---------------- 自动备份 ----------------

def backup_db(db_path, backup_dir, stats, non_interactive):
    """用 sqlite_tool.py 做在线备份并校验；返回备份信息 dict，失败抛 RuntimeError。"""
    tool = os.path.join(PROJECT_ROOT, 'scripts', 'sqlite_tool.py')
    if not os.path.exists(tool):
        raise RuntimeError('缺少 scripts/sqlite_tool.py，无法自动备份')
    stamp = _now().strftime('%Y%m%d-%H%M%S')
    target_dir = os.path.join(backup_dir, stamp)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as exc:
        raise RuntimeError('备份目录不可用：%s（%s）' % (target_dir, exc))
    target_db = os.path.join(target_dir, DB_NAME)

    _print('[init] 正在自动备份到：%s' % target_dir)
    proc = subprocess.run(
        [sys.executable, tool, 'backup', '--src', db_path, '--dst', target_db],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or '').strip()
        raise RuntimeError('在线备份失败（退出码 %s）：%s' % (proc.returncode, detail))
    try:
        backup_result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError('在线备份输出无法解析：%s' % (proc.stdout or '')[:300])
    if not backup_result.get('ok'):
        raise RuntimeError('在线备份完整性校验失败')

    verify = subprocess.run(
        [sys.executable, tool, 'verify', '--db', target_db],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    if verify.returncode != 0:
        raise RuntimeError('备份校验失败：%s' % ((verify.stdout or verify.stderr or '').strip()[:300]))
    try:
        verify_result = json.loads(verify.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError('备份校验输出无法解析')

    # 行数必须与体检一致，否则备份不可信
    source_counts = (stats or {}).get('counts') or {}
    backup_counts = backup_result.get('counts') or {}
    for table in COUNT_TABLES:
        if source_counts.get(table) is not None and backup_counts.get(table) != source_counts[table]:
            raise RuntimeError('备份行数与源库不一致（%s: 源=%s 备份=%s）'
                               % (table, source_counts.get(table), backup_counts.get(table)))

    info = {
        'created_at': _now().isoformat(timespec='seconds'),
        'host': socket.gethostname(),
        'operator': os.environ.get('USERNAME') or os.environ.get('USER') or '',
        'mode': 'non-interactive' if non_interactive else 'interactive',
        'db_path': db_path,
        'backup_path': target_db,
        'backup_bytes': os.path.getsize(target_db),
        'integrity': verify_result.get('integrity'),
        'counts': backup_counts,
        'paid_total': (stats or {}).get('paid_total'),
        'source_stats': stats,
    }
    meta_path = os.path.join(target_dir, 'pre-reset.json')
    with open(meta_path, 'w', encoding='utf-8') as handle:
        json.dump(info, handle, ensure_ascii=False, indent=2)
    _print('[init] 备份完成并校验通过：%s（%.1f KB，integrity=%s）'
           % (target_db, info['backup_bytes'] / 1024.0, info['integrity']))
    return info


# ---------------- 二次确认 ----------------

def confirm_reset(db_name, backup_info, stats):
    """要求输入库文件名确认；返回 True 才允许删除。"""
    counts = (stats or {}).get('counts') or {}
    _print('')
    _print('=' * 68)
    _print('危险操作：即将删除并重建数据库')
    _print('  数据库文件 : %s' % db_name)
    _print('  现有数据   : %s' % ', '.join('%s=%s' % (k, counts.get(k)) for k in COUNT_TABLES))
    if backup_info:
        _print('  已自动备份 : %s' % backup_info.get('backup_path'))
        _print('  恢复示例   : powershell -File scripts\\restore.ps1 -BackupPath "%s" -TargetDir <数据目录> -Force'
               % os.path.dirname(backup_info.get('backup_path')))
    else:
        _print('  备份       : 已按要求跳过（--no-backup，高危）')
    _print('=' * 68)
    if not sys.stdin.isatty():
        _err('当前不在交互终端，无法进行二次确认；自动化请使用 --force --yes。数据库未做任何修改。')
        return False
    try:
        answer = input('请输入数据库文件名「%s」以确认删除重建（其他输入=取消）：' % db_name)
    except EOFError:
        answer = ''
    if answer.strip() != db_name:
        _err('确认输入不匹配，已取消；数据库未做任何修改。')
        return False
    return True


# ---------------- 默认口令与种子 ----------------

def resolve_password(env_key, default):
    value = os.environ.get(env_key)
    if value:
        return value, False
    _warn('%s 未设置，使用历史默认口令（上线前必须改密）' % env_key)
    return default, True


def seed_users():
    if User.query.count() != 0:
        _print('[init] 用户已存在，跳过默认用户创建')
        return
    created = []
    default_used = []
    for username, display_name, role, env_key, default in SEED_USERS:
        password, used_default = resolve_password(env_key, default)
        user = User(username=username, display_name=display_name, role=role)
        user.set_password(password)
        db.session.add(user)
        created.append('%s(%s)' % (username, '默认口令' if used_default else '来自环境变量'))
        if used_default:
            default_used.append(username)
    db.session.flush()
    _print('[init] 默认用户已创建：%s' % '、'.join(created))
    if default_used:
        _warn('账号 %s 使用历史默认口令，正式上线前必须改密'
              '（或设置 PAYMENT_*_PASSWORD 后重建）' % '、'.join(default_used))


def build_seed_project():
    """按 Excel《付款进度表（新增动设备监测）》建立种子项目"""
    project = Project(
        project_no='PJ-2025-DSJ',
        name='新增动设备监测项目',
        status='进行中',
        trial_date=None,          # 由迁移数据回填 2025-11-04
        acceptance_date=None,
        remark='由《付款进度表（新增动设备监测）.xlsx》迁移初始化',
    )
    db.session.add(project)
    db.session.flush()

    seed_items = [
        {'item_name': '设备费', 'tax_rate': '13%', 'original': 131000, 'additional': 0,
         'first': {'invoice': 39300, 'paid': 39300, 'status': '已付款'},
         'progress': {'invoice': 68120, 'paid': 68120, 'status': '已申请'}},
        {'item_name': '集成服务费', 'tax_rate': '6%', 'original': 28000, 'additional': 0,
         'first': {'invoice': 8400, 'paid': 8400, 'status': '已付款'},
         'progress': {'invoice': 14560, 'paid': 14560, 'status': '已申请'}},
        {'item_name': '两年物联网卡', 'tax_rate': '6%', 'original': 6000, 'additional': 0,
         'first': {'invoice': 1800, 'paid': 1800, 'status': '已付款'},
         'progress': {'invoice': 3120, 'paid': 3120, 'status': '已申请'}},
    ]

    for it in seed_items:
        item = ProjectItem(project_id=project.id, item_name=it['item_name'],
                           tax_rate=it['tax_rate'], original_amount=it['original'],
                           additional_amount=it['additional'])
        db.session.add(item)
        db.session.flush()
        for t in TEMPLATES:
            node = PaymentNode(project_id=project.id, item_id=item.id,
                               node_type=t['node_type'], node_name=t['node_name'],
                               ratio=t['ratio'], calc_base=t['calc_base'], status='未触发')
            node.recalc()
            key = t['node_type']
            if key in it:
                node.invoice_amount = it[key]['invoice']
                node.paid_amount = it[key]['paid']
                node.status = it[key]['status']
                node.trigger_date = None
            elif t['node_type'] in ('acceptance',):
                node.trigger_date = None
            db.session.add(node)
    return project


def seed_project():
    if Project.query.count() != 0:
        db.session.commit()
        _print('[init] 项目已存在，跳过种子数据')
        return
    project = build_seed_project()
    db.session.flush()
    # 回填试运行日期：按 Excel 中 2025-11-04
    project.trial_date = date(2025, 11, 4)
    for node in project.nodes:
        if node.node_type == 'progress':
            node.trigger_date = date(2025, 11, 4)
    db.session.commit()
    _print('[init] 种子项目「%s」已创建（%s）' % (project.name, project.project_no))


# ---------------- 主流程 ----------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='初始化/重建付款进度管理系统数据库',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('--force', action='store_true', help='删除并重建数据库（默认先自动备份+二次确认）')
    parser.add_argument('--yes', action='store_true', help='非交互确认（计划任务用；仍会强制自动备份）')
    parser.add_argument('--no-backup', action='store_true', help='跳过自动备份（仅排障；必须与 --yes 同用）')
    parser.add_argument('--allow-running', action='store_true',
                        help='即使 -wal 非空（服务可能在跑）也继续（仅排障）')
    parser.add_argument('--data-dir', default=None, help='数据目录（默认 DATA_DIR 或项目 instance/）')
    parser.add_argument('--backup-dir', default=None, help='备份根目录（默认 <数据目录上级>/backups/pre-reset）')
    seed_group = parser.add_mutually_exclusive_group()
    seed_group.add_argument('--no-seed', action='store_true',
                            help='不创建演示项目（生产初始化用；APP_ENV=production 时默认开启）')
    seed_group.add_argument('--seed', action='store_true',
                            help='强制创建演示项目（即使在生产模式；演示/测试用）')
    return parser.parse_args(argv)


def resolve_seed(args):
    """是否写入演示种子项目。

    优先级：--seed / --no-seed 显式指定 > APP_ENV=production 默认不种 > 其余环境默认种。
    注意：只认 APP_ENV 环境变量，未经 create_app 的配置默认值（避免测试环境被误判为生产）。
    """
    if args.seed:
        return True
    if args.no_seed:
        return False
    return os.environ.get('APP_ENV', '').strip().lower() != 'production'


def resolve_data_dir(args):
    """在创建 Flask app（可能触发自动迁移/写 WAL）之前解析数据目录与库文件路径。"""
    data_dir = args.data_dir or os.environ.get('DATA_DIR') \
        or os.path.join(PROJECT_ROOT, 'instance')
    data_dir = os.path.abspath(data_dir)
    return data_dir, os.path.join(data_dir, DB_NAME)


def build_app(data_dir):
    db_path = os.path.join(data_dir, DB_NAME)
    os.makedirs(data_dir, exist_ok=True)
    # 显式配置数据目录，避免 create_app 回退到 %ProgramData% 再做可写校验（初始化只关心 --data-dir）
    return create_app({
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + db_path.replace('\\', '/'),
        'DATA_DIR': data_dir,
        'UPLOAD_DIR': os.path.join(data_dir, 'uploads'),
    })


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.no_backup and not args.yes:
        _err('--no-backup 只能与 --yes 同用（避免误删无备份）')
        return 2

    data_dir, db_path = resolve_data_dir(args)
    backup_dir = args.backup_dir or os.environ.get('PAYMENT_BACKUP_DIR') \
        or os.path.join(os.path.dirname(data_dir), 'backups', 'pre-reset')
    do_seed = resolve_seed(args)

    _print('=' * 68)
    _print('付款进度管理系统 · 数据库初始化')
    _print('  数据库 : %s' % db_path)
    _print('  DATA_DIR: %s' % data_dir)
    _print('  演示数据: %s' % ('写入（种子项目）' if do_seed
                              else '跳过（--no-seed 或 APP_ENV=production）'))
    if args.force:
        _print('  模式   : --force（删除重建）')
    _print('=' * 68)

    if args.force and os.path.exists(db_path):
        stats = collect_stats(db_path)
        stats['path'] = db_path
        _print_stats(stats)
        # 必须在创建 app/连接数据库之前检查，否则自己的连接会写 WAL 造成误判
        wal_size = check_wal(data_dir)
        if wal_size and not args.allow_running:
            _err('检测到 %s-wal 非空（%d B），服务可能仍在运行；'
                 '请先停止服务再重建（排障可加 --allow-running）。数据库未修改。'
                 % (DB_NAME, wal_size))
            return 1
        backup_info = None
        if args.no_backup:
            _warn('已按要求跳过自动备份（--no-backup）；删除后不可恢复！')
        else:
            try:
                backup_info = backup_db(db_path, backup_dir, stats, args.yes)
            except RuntimeError as exc:
                _err('%s；数据库未做任何修改。' % exc)
                return 1
        if not args.yes and not confirm_reset(DB_NAME, backup_info, stats):
            return 1
        for suffix in ('', '-wal', '-shm'):
            target = db_path + suffix
            if os.path.exists(target):
                os.remove(target)
                _print('[init] 已删除：%s' % target)
    elif args.force:
        _print('[init] 数据库不存在，直接初始化（无需备份/确认）。')

    app = build_app(data_dir)
    with app.app_context():
        db.engine.dispose()
        db.create_all()
        _print('[init] 数据表创建完成')
        seed_users()
        if do_seed:
            seed_project()
        else:
            # 不写演示项目时也要提交账号创建
            db.session.commit()
            _print('[init] 已跳过演示项目（--no-seed 或 APP_ENV=production）：'
                   '只创建表结构与账号，不写入种子项目')

    _print('[init] 初始化完成')
    return 0


if __name__ == '__main__':
    sys.exit(main())

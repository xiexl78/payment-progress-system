# -*- coding: utf-8 -*-
"""付款进度管理系统 - SQLite 运维工具（供 backup.ps1 / restore.ps1 调用）

子命令：
  backup  --src <源库> --dst <目标库> [--pages N]
          使用 SQLite 在线备份 API 生成一致性快照（不复制正在写的库文件），
          目标库为单文件（DELETE 日志模式，无 -wal/-shm，便于归档），
          备份后执行 integrity_check / quick_check 并统计各表行数。
  verify  --db <库> [--expect-json <backup.json>]
          校验数据库可打开、完整性、schema 指纹；若给出 backup.json，
          则比对行数与 schema 指纹是否一致。
  info    --db <库>
          输出数据库状态信息（供人工排查）。

输出：stdout 只输出一个 JSON 对象；错误信息写 stderr。
退出码：0 成功 / 1 校验失败或异常 / 2 参数错误。

说明：源库以只读方式打开（mode=ro，失败时退回 query_only 连接），
本工具不会修改源数据库；在线备份期间源库可继续读写。
"""
import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

# 业务表（用于行数比对；缺表不视为错误，值记为 null）
TABLES = [
    'users', 'projects', 'project_items', 'payment_nodes',
    'payment_applications', 'payment_records', 'contract_files', 'acceptance_records',
]


def _configure_stdout():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass


def _connect_ro(path, timeout=15.0):
    """只读打开源库。WAL 库在只读 URI 下若无法创建 -shm 会失败，退回 query_only 连接。"""
    path = Path(path)
    try:
        uri = path.resolve().as_uri() + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
        conn.execute('PRAGMA busy_timeout=%d' % int(timeout * 1000))
        conn.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchall()
        return conn
    except sqlite3.Error:
        conn = sqlite3.connect(str(path), timeout=timeout)
        conn.execute('PRAGMA busy_timeout=%d' % int(timeout * 1000))
        conn.execute('PRAGMA query_only=ON')
        return conn


def _connect(path, timeout=15.0):
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.execute('PRAGMA busy_timeout=%d' % int(timeout * 1000))
    return conn


def _counts(conn):
    out = {}
    for table in TABLES:
        try:
            out[table] = conn.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0]
        except sqlite3.Error:
            out[table] = None
    return out


def _schema_sha256(conn):
    rows = conn.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(('|'.join(str(x) for x in row)).encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()


def _db_info(conn, path):
    info = {
        'ok': False,
        'integrity': None,
        'quick_check': None,
        'counts': {},
        'schema_sha256': None,
        'bytes': Path(path).stat().st_size if Path(path).exists() else 0,
    }
    try:
        info['integrity'] = conn.execute('PRAGMA integrity_check').fetchone()[0]
        info['quick_check'] = conn.execute('PRAGMA quick_check').fetchone()[0]
        info['counts'] = _counts(conn)
        info['schema_sha256'] = _schema_sha256(conn)
        info['ok'] = info['integrity'] == 'ok' and info['quick_check'] == 'ok'
    except sqlite3.Error as exc:
        info['error'] = str(exc)
    return info


def _remove_sqlite_sidecars(path):
    for suffix in ('-wal', '-shm', '-journal'):
        side = Path(str(path) + suffix)
        if side.exists():
            try:
                side.unlink()
            except OSError:
                pass


def cmd_backup(args):
    src, dst = Path(args.src), Path(args.dst)
    if not src.exists():
        print('source database not found: %s' % src, file=sys.stderr)
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    _remove_sqlite_sidecars(dst)
    if dst.exists():
        dst.unlink()

    started = time.time()
    source = _connect_ro(src)
    target = None
    last_error = None
    for attempt in range(1, 4):
        try:
            target = sqlite3.connect(str(dst), timeout=15.0)
            target.execute('PRAGMA journal_mode=DELETE')
            target.execute('PRAGMA synchronous=FULL')
            source.backup(target, pages=args.pages, sleep=0.05)
            target.commit()
            break
        except sqlite3.Error as exc:
            last_error = exc
            if target is not None:
                try:
                    target.close()
                except sqlite3.Error:
                    pass
                target = None
            _remove_sqlite_sidecars(dst)
            if dst.exists():
                dst.unlink()
            time.sleep(float(attempt))
    if target is None:
        print('online backup failed after retries: %s' % last_error, file=sys.stderr)
        return 1
    target.close()

    # 在备份文件上复核 + 统计（此时备份已独立于源库）
    check = _connect(dst)
    try:
        info = _db_info(check, dst)
    finally:
        check.close()
    src_counts = None
    src_info = None
    try:
        src_info = _db_info(source, src)
        src_counts = src_info['counts']
    finally:
        source.close()

    result = {
        'ok': bool(info['ok']),
        'integrity': info['integrity'],
        'quick_check': info['quick_check'],
        'bytes': info['bytes'],
        'schema_sha256': info['schema_sha256'],
        'counts': info['counts'],
        'source': {
            'bytes': src_info['bytes'] if src_info else None,
            'counts': src_counts,
        },
        # 源库在此次备份期间若仍有写入，行数可能与快照不同，仅作参考
        'source_may_have_changed': bool(
            src_counts is not None and info['counts'] != src_counts
        ),
        'seconds': round(time.time() - started, 3),
    }
    if not result['ok']:
        print(json.dumps(result, ensure_ascii=False))
        print('backup database failed integrity check', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _load_expect(path):
    with open(path, 'r', encoding='utf-8') as handle:
        data = json.load(handle)
    return data


def cmd_verify(args):
    db = Path(args.db)
    if not db.exists():
        print('database not found: %s' % db, file=sys.stderr)
        return 1
    conn = _connect(db)
    try:
        info = _db_info(conn, db)
    finally:
        conn.close()

    result = {'ok': False, 'integrity': info['integrity'],
              'quick_check': info['quick_check'], 'bytes': info['bytes'],
              'schema_sha256': info['schema_sha256'], 'counts': info['counts'],
              'error': info.get('error'), 'mismatches': []}
    if not info['ok']:
        if info.get('error'):
            result['mismatches'].append('数据库无法正常读取: %s' % info['error'])
        else:
            result['mismatches'].append('integrity_check=%s quick_check=%s'
                                        % (info['integrity'], info['quick_check']))
        print(json.dumps(result, ensure_ascii=False))
        return 1

    if args.expect_json:
        expect = _load_expect(args.expect_json)
        exp_db = (expect or {}).get('db') or {}
        exp_counts = exp_db.get('counts') or {}
        for table, want in exp_counts.items():
            got = info['counts'].get(table)
            if got != want:
                result['mismatches'].append('counts.%s: expected %s, got %s' % (table, want, got))
        if exp_db.get('schema_sha256') and exp_db['schema_sha256'] != info['schema_sha256']:
            result['mismatches'].append('schema_sha256 mismatch')
        exp_bytes = exp_db.get('bytes')
        if exp_bytes is not None and exp_bytes != info['bytes']:
            # 行数一致而字节数不同，通常说明备份文件被改动过，按失败处理
            result['mismatches'].append('bytes: expected %s, got %s' % (exp_bytes, info['bytes']))
        result['ok'] = not result['mismatches']
    else:
        result['ok'] = True

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


def cmd_info(args):
    db = Path(args.db)
    if not db.exists():
        print('database not found: %s' % db, file=sys.stderr)
        return 1
    conn = _connect(db)
    try:
        info = _db_info(conn, db)
    finally:
        conn.close()
    print(json.dumps(info, ensure_ascii=False))
    return 0 if info['ok'] else 1


def build_parser():
    parser = argparse.ArgumentParser(description='SQLite 在线备份 / 校验工具')
    sub = parser.add_subparsers(dest='command')

    p_backup = sub.add_parser('backup', help='在线备份源库到目标文件')
    p_backup.add_argument('--src', required=True)
    p_backup.add_argument('--dst', required=True)
    p_backup.add_argument('--pages', type=int, default=256,
                          help='每次备份步进的页数（默认 256，越小对源库写入越友好）')
    p_backup.set_defaults(func=cmd_backup)

    p_verify = sub.add_parser('verify', help='校验数据库完整性（可比对 backup.json）')
    p_verify.add_argument('--db', required=True)
    p_verify.add_argument('--expect-json', default=None)
    p_verify.set_defaults(func=cmd_verify)

    p_info = sub.add_parser('info', help='输出数据库状态信息')
    p_info.add_argument('--db', required=True)
    p_info.set_defaults(func=cmd_info)
    return parser


def main(argv=None):
    _configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, 'func', None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())

# -*- coding: utf-8 -*-
"""CR-07 ①：清理超期未绑定项目的「截图识别草稿」。

口径（docs/change-request-07.md §3.6 / Q3）：
- 候选 = ``contract_files`` 中 ``project_id IS NULL`` 且
  ``status IN ('uploaded','recognized')`` 且 ``created_at < now - N 天``；
- 默认 dry-run：只列出候选、占用文件数与体积，不动库也不动盘；
- ``--apply`` 才真正删除：先删 ``contract_screenshots`` 行、再删
  ``contract_files`` 行，随后 best-effort 删除截图原图 / 合同文件 /
  ``uploads/shots/<id>/`` 目录；
- 每个被清理的草稿写一条审计 ``contract.draft.purge``
  （object_type=contract，after 含 reason/天数/文件数/字节数/删除结果）；
- 保留天数默认取配置 ``DRAFT_RETENTION_DAYS``（7），可用 ``--days`` 覆盖。

用法：
    python scripts/cleanup_contract_drafts.py                 # dry-run（默认数据目录）
    python scripts/cleanup_contract_drafts.py --days 3        # 换成 3 天口径的 dry-run
    python scripts/cleanup_contract_drafts.py --apply         # 真删
    python scripts/cleanup_contract_drafts.py --data-dir data-dev --apply
    python scripts/cleanup_contract_drafts.py --db D:/tmp/x.db --apply
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app import create_app  # noqa: E402
from app.audit import record as audit_record  # noqa: E402
from app.models import ContractFile, ContractScreenshot, db  # noqa: E402

DB_NAME = 'payment_progress.db'
CANDIDATE_STATUS = ('uploaded', 'recognized')
ACTOR = (None, 'cleanup_contract_drafts.py', 'system')


def _print(msg=''):
    print(msg, flush=True)


def _err(msg):
    print('[错误] ' + msg, file=sys.stderr, flush=True)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='清理超期未绑定项目的截图识别草稿（默认 dry-run）',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', default=None,
                        help='数据目录（默认 DATA_DIR 或项目 instance/）')
    parser.add_argument('--db', default=None,
                        help='直接指定 SQLite 数据库文件（优先于 --data-dir）')
    parser.add_argument('--days', type=int, default=None,
                        help='保留天数（默认取配置 DRAFT_RETENTION_DAYS，缺省 7）')
    parser.add_argument('--apply', action='store_true',
                        help='真正删除；不加则为 dry-run（只报告）')
    return parser.parse_args(argv)


def resolve_paths(args):
    if args.db:
        db_path = os.path.abspath(args.db)
        data_dir = os.path.dirname(db_path)
    else:
        data_dir = args.data_dir or os.environ.get('DATA_DIR') \
            or os.path.join(PROJECT_ROOT, 'instance')
        data_dir = os.path.abspath(data_dir)
        db_path = os.path.join(data_dir, DB_NAME)
    return data_dir, db_path


def build_app(data_dir, db_path):
    upload_dir = os.path.join(data_dir, 'uploads')
    return create_app({
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + db_path.replace('\\', '/'),
        'DATA_DIR': data_dir,
        'UPLOAD_DIR': upload_dir,
        'SKIP_SCHEMA_UPGRADE': True,
    })


def _abs_path(stored_path, upload_dir):
    """``stored_path`` 相对 UPLOAD_DIR（兼容历史绝对路径）。"""
    raw = (stored_path or '').replace('\\', '/').strip()
    if not raw:
        return None
    if os.path.isabs(raw):
        return raw
    name = raw.split('/', 1)[1] if raw.startswith('uploads/') else raw
    return os.path.join(upload_dir, name)


def collect_candidates(app, cutoff):
    """返回候选草稿的描述列表（不修改任何数据）。"""
    upload_dir = app.config.get('UPLOAD_DIR')
    rows = db.session.query(ContractFile).filter(
        ContractFile.project_id.is_(None),
        ContractFile.status.in_(CANDIDATE_STATUS),
        ContractFile.created_at.isnot(None),
        ContractFile.created_at < cutoff,
    ).order_by(ContractFile.id.asc()).all()
    candidates = []
    for draft in rows:
        files = []
        for shot in list(draft.screenshots):
            path = _abs_path(shot.stored_path, upload_dir)
            if path and os.path.isfile(path):
                files.append(path)
        path = _abs_path(draft.stored_path, upload_dir)
        if path and os.path.isfile(path):
            files.append(path)
        shot_dir = os.path.join(upload_dir, 'shots', str(draft.id))
        size = 0
        for path in files:
            try:
                size += os.path.getsize(path)
            except OSError:
                pass
        candidates.append({
            'id': draft.id,
            'file_name': draft.file_name,
            'status': draft.status,
            'source': draft.source,
            'created_at': draft.created_at,
            'screenshots': len(list(draft.screenshots)),
            'files': files,
            'shot_dir': shot_dir if os.path.isdir(shot_dir) else None,
            'bytes': size,
        })
    return candidates


def apply_cleanup(app, candidates, days):
    """逐条删除并审计；返回汇总 {'purged': N, 'files': N, 'bytes': N, 'errors': [...]}。"""
    result = {'purged': 0, 'files': 0, 'bytes': 0, 'errors': []}
    for item in candidates:
        draft = db.session.get(ContractFile, item['id'])
        if draft is None or draft.project_id is not None:
            continue  # 期间被绑定 → 跳过（防误删）
        removed, file_errors = [], []
        db.session.query(ContractScreenshot).filter(
            ContractScreenshot.contract_id == draft.id).delete(synchronize_session=False)
        db.session.delete(draft)
        db.session.flush()
        for path in item['files']:
            try:
                os.remove(path)
                removed.append(path)
            except OSError as exc:
                app.logger.warning('清理草稿文件失败：%s（%s）', path, exc)
                file_errors.append({'path': path, 'error': str(exc)[:120]})
        if item['shot_dir']:
            try:
                for name in os.listdir(item['shot_dir']):
                    try:
                        os.remove(os.path.join(item['shot_dir'], name))
                    except OSError:
                        pass
                os.rmdir(item['shot_dir'])
            except OSError as exc:
                app.logger.warning('清理草稿目录失败：%s（%s）', item['shot_dir'], exc)
                file_errors.append({'path': item['shot_dir'], 'error': str(exc)[:120]})
        audit_record(
            'contract.draft.purge', 'contract', draft.id,
            before={'file_name': draft.file_name, 'status': draft.status,
                    'source': draft.source, 'screenshots': item['screenshots']},
            after={'reason': 'retention_expired', 'days': days,
                   'screenshots': item['screenshots'],
                   'files': item['files'], 'bytes': item['bytes'],
                   'deleted': removed, 'file_errors': file_errors},
            result='ok' if not file_errors else 'partial',
            actor=ACTOR)
        db.session.commit()
        result['purged'] += 1
        result['files'] += len(removed)
        result['bytes'] += item['bytes']
        if file_errors:
            result['errors'].extend(file_errors)
    return result


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    data_dir, db_path = resolve_paths(args)

    _print('=' * 66)
    _print('CR-07 ① 清理超期未绑定截图识别草稿')
    _print('  数据目录: %s' % data_dir)
    _print('  数据库  : %s' % db_path)
    _print('  模式    : %s' % ('APPLY（真删）' if args.apply else 'dry-run（只报告）'))
    _print('=' * 66)

    if not os.path.exists(db_path):
        _err('数据库不存在：%s' % db_path)
        return 1

    app = build_app(data_dir, db_path)
    with app.app_context():
        days = args.days if args.days is not None \
            else int(app.config.get('DRAFT_RETENTION_DAYS') or 7)
        if days < 1:
            _err('保留天数必须 ≥ 1（当前 %r）' % days)
            return 1
        cutoff = datetime.utcnow() - timedelta(days=days)
        _print('  保留天数: %d 天（早于 %s UTC 的草稿视为超期）'
               % (days, cutoff.strftime('%Y-%m-%d %H:%M')))

        candidates = collect_candidates(app, cutoff)
        total_bytes = sum(c['bytes'] for c in candidates)
        total_files = sum(len(c['files']) for c in candidates)
        _print('  候选草稿: %d 个；占用文件: %d 个；合计 %d 字节（%.1f KB）'
               % (len(candidates), total_files, total_bytes, total_bytes / 1024.0))
        for c in candidates:
            _print('   - #%d %s（%s，source=%s，%s；截图 %d 张，文件 %d 个，%d 字节）'
                   % (c['id'], c['file_name'], c['status'], c['source'],
                      c['created_at'].strftime('%Y-%m-%d %H:%M'),
                      c['screenshots'], len(c['files']), c['bytes']))

        if not candidates:
            _print('[完成] 没有超期草稿需要清理。')
            return 0
        if not args.apply:
            _print('[dry-run] 未做任何修改；确认无误后加 --apply 执行删除。')
            return 0

        result = apply_cleanup(app, candidates, days)
        _print('[清理] 草稿 %d 个；删除文件 %d 个；释放 %d 字节（%.1f KB）'
               % (result['purged'], result['files'], result['bytes'],
                  result['bytes'] / 1024.0))
        if result['errors']:
            _print('[警告] %d 个文件/目录删除失败（DB 行已删除，可人工清理）：'
                   % len(result['errors']))
            for err in result['errors']:
                _print('   ! %s（%s）' % (err['path'], err['error']))
            return 2
        _print('[完成] 已清理并逐条写入审计 contract.draft.purge。')
        return 0


if __name__ == '__main__':
    sys.exit(main())

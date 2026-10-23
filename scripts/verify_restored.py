# -*- coding: utf-8 -*-
"""付款进度管理系统 - 恢复后冒烟校验（只读）

用途：从备份恢复数据库后，在不启动正式服务的前提下，用 Flask test client
      直接对"恢复出来的库"做一次页面级冒烟，确认数据可读、可登录、可渲染。

用法：
    python scripts/verify_restored.py --db D:\\PaymentSystem\\data\\payment_progress.db
    python scripts/verify_restored.py --db <库> --username admin --password admin123

退出码：0 全部通过 / 1 存在失败项 / 2 参数或环境错误。

注意：
- 本脚本只读数据库（走应用正常查询路径），不会写业务数据；
  SQLite 会因 WAL 模式创建 -wal/-shm 旁文件，属正常现象。
- 它不等于完整 e2e：完整 e2e 需要启动服务（http)并跑 scripts/e2e_test.py。
  恢复验收标准见 docs/backup-restore.md。
"""
import argparse
import os
import sys
from pathlib import Path

RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', name,
                           (' - ' + detail) if (detail and not cond) else ''))


def main(argv=None):
    parser = argparse.ArgumentParser(description='恢复后冒烟校验（只读）')
    parser.add_argument('--db', required=True, help='待校验的数据库文件')
    parser.add_argument('--app-dir', default=None, help='应用根目录（默认取 scripts 的上级）')
    parser.add_argument('--username', default='admin')
    parser.add_argument('--password', default='admin123')
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print('数据库不存在: %s' % db_path, file=sys.stderr)
        return 2

    app_dir = Path(args.app_dir).resolve() if args.app_dir else Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(app_dir))

    try:
        from app import create_app
        from app.models import db as sa_db, Project, PaymentNode
    except Exception as exc:  # noqa: BLE001
        print('无法导入应用（请确认 --app-dir 指向项目根目录）: %s' % exc, file=sys.stderr)
        return 2

    try:
        app = create_app({
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(db_path).replace('\\', '/'),
            'TESTING': True,
            'WTF_CSRF_ENABLED': False,
        })
        client = app.test_client()
        with app.app_context():
            project_count = Project.query.count()
            node_count = PaymentNode.query.count()
            first_project = Project.query.order_by(Project.id).first()
            first_project_no = first_project.project_no if first_project else None
            first_project_id = first_project.id if first_project else None
    except Exception as exc:  # noqa: BLE001
        print('应用初始化失败（数据库可能损坏或版本不匹配）: %s' % exc, file=sys.stderr)
        return 2

    print('数据库: %s' % db_path)
    print('记录数: projects=%d, payment_nodes=%d' % (project_count, node_count))
    print('冒烟检查:')
    check('数据库可查询（projects 表）', project_count >= 0)
    check('存在项目数据', project_count > 0, 'projects=%d' % project_count)

    resp = client.post('/login', data={'username': args.username, 'password': args.password},
                       follow_redirects=False)
    login_ok = resp.status_code == 302
    check('登录成功', login_ok, 'HTTP %s（账号 %s）' % (resp.status_code, args.username))

    if login_ok:
        resp = client.get('/projects')
        check('项目列表页 200', resp.status_code == 200, 'HTTP %s' % resp.status_code)
        if first_project_no:
            check('项目列表包含首个项目编号', first_project_no in resp.get_data(as_text=True))
        resp = client.get('/reports')
        check('报表页 200', resp.status_code == 200, 'HTTP %s' % resp.status_code)
        resp = client.get('/contracts')
        check('合同列表页 200', resp.status_code == 200, 'HTTP %s' % resp.status_code)
        if first_project_id:
            resp = client.get('/projects/%d' % first_project_id)
            check('项目详情页 200', resp.status_code == 200, 'HTTP %s' % resp.status_code)
            body = resp.get_data(as_text=True)
            check('项目详情含付款节点区块', '付款节点管理' in body)
        client.get('/logout')

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print('\n结果: %d/%d 通过' % (passed, total))
    if passed != total:
        print('恢复冒烟校验失败', file=sys.stderr)
        return 1
    print('恢复冒烟校验全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())

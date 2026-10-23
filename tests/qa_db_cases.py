# -*- coding: utf-8 -*-
"""数据库 / 恢复 / 备份 / 工作目录 回归用例（全部在临时目录进行，不碰项目实例库）。"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from sqlalchemy import text

import qa_common as C
from qa_common import check, q1, make_app, seed_db, login, SAMPLE_TXT, PROJECT_ROOT


def _dispose(app):
    with app.app_context():
        C.db.session.remove()
        C.db.engine.dispose()


def _login_client(app, user='admin', pwd='admin123'):
    client = app.test_client()
    r = client.post('/login', data={'username': user, 'password': pwd}, follow_redirects=False)
    return client, r


def _case_restart_and_session(tmp):
    print('\n########## 1. 服务重启：数据与会话 ##########')
    dbp = os.path.join(tmp, 'restart.db')
    seed_db(dbp)
    app1 = make_app(dbp)
    c1, r = _login_client(app1)
    check('重启', '登录成功', r.status_code == 302)
    cookie = c1.get_cookie('session')
    check('重启', '登录后获得会话 Cookie', cookie is not None)
    _dispose(app1)
    # 模拟重启：新应用实例、同库同密钥
    app2 = make_app(dbp)
    c2 = app2.test_client()
    c2.set_cookie('session', cookie.value, domain='localhost')
    r = c2.get('/projects')
    check('重启', '重启后业务数据可用', r.status_code == 200 and 'PJ-2025-DSJ' in r.get_data(as_text=True))
    check('重启', '重启后会话仍有效（服务端会话持久化，策略已接受）',
          r.status_code == 200 and 'PJ-2025-DSJ' in r.get_data(as_text=True),
          '旧 Cookie 失效 code=%s' % r.status_code)
    _dispose(app2)


def _case_missing_and_corrupt_db(tmp):
    print('\n########## 2. 数据库缺失 / 损坏 ##########')
    missing = os.path.join(tmp, 'missing.db')
    app = make_app(missing)
    client = app.test_client()
    r = client.post('/login', data={'username': 'admin', 'password': 'admin123'})
    check('恢复', '数据库文件缺失时应可恢复（自愈或友好提示）',
          r.status_code != 500, '登录直接 %s 默认错误页' % r.status_code)
    _dispose(app)

    corrupt = os.path.join(tmp, 'corrupt.db')
    with open(corrupt, 'wb') as f:
        f.write(b'not a sqlite database' * 100)
    app = make_app(corrupt)
    client = app.test_client()
    r = client.post('/login', data={'username': 'admin', 'password': 'admin123'})
    check('恢复', '数据库损坏时应可恢复（自愈或友好提示）',
          r.status_code != 500, '登录直接 %s 默认错误页' % r.status_code)
    _dispose(app)

    # 运行中文件锁保护
    locked_db = os.path.join(tmp, 'locked.db')
    seed_db(locked_db)
    app = make_app(locked_db)
    with app.app_context():
        C.db.session.execute(text('SELECT 1'))
        C.db.session.commit()
        try:
            os.remove(locked_db)
            blocked = False
        except Exception:
            blocked = True
        check('恢复', '服务运行中数据库文件受系统锁保护', blocked, '未加锁时可被运行中删除')
    _dispose(app)


def _case_backup(tmp):
    print('\n########## 3. 备份与恢复可用性 ##########')
    hot = os.path.join(tmp, 'hot.db')
    seed_db(hot)
    app = make_app(hot)
    with app.app_context():
        node = C.db.session.execute(text('SELECT id FROM payment_nodes ORDER BY id LIMIT 1')).fetchone()[0]
        C.db.session.add(C.PaymentRecord(node_id=node, paid_amount=4321.0, pay_method='QA备份用例'))
        C.db.session.commit()
        live = q1(hot, 'SELECT COUNT(*) FROM payment_records')
        # SQLite backup API
        api_backup = os.path.join(tmp, 'backup_api.db')
        src = sqlite3.connect(hot)
        dst = sqlite3.connect(api_backup)
        src.backup(dst)
        dst.close()
        src.close()
        cnt_api = q1(api_backup, 'SELECT COUNT(*) FROM payment_records')
        check('备份', 'SQLite backup API 在线备份数据完整', cnt_api == live, 'API=%d 实时=%d' % (cnt_api, live))
    _dispose(app)
    # 备份脚本必须走 SQLite Online Backup API，而非直接复制活动 .db
    try:
        ps = open(os.path.join(PROJECT_ROOT, 'scripts', 'backup.ps1'), encoding='utf-8').read()
        tool = open(os.path.join(PROJECT_ROOT, 'scripts', 'sqlite_tool.py'), encoding='utf-8').read()
        check('备份', 'scripts/backup.ps1 走 SQLite Online Backup API（非直接复制 .db）',
              ('sqlite_tool' in ps or 'backup' in ps.lower())
              and ('.backup(' in tool or 'def backup' in tool),
              'backup.ps1/sqlite_tool.py 未使用 Online Backup API')
    except Exception as e:
        check('备份', 'scripts/backup.ps1 走 SQLite Online Backup API（非直接复制 .db）', False, repr(e))

    # 停服整库复制 → 恢复
    restored = os.path.join(tmp, 'restored.db')
    for suffix in ('', '-wal', '-shm'):
        if os.path.exists(hot + suffix):
            shutil.copy2(hot + suffix, restored + suffix)
    app = make_app(restored)
    client, r = _login_client(app)
    page = client.get('/projects').get_data(as_text=True)
    check('备份', '停服整库复制后可登录且种子数据完整',
          r.status_code == 302 and 'PJ-2025-DSJ' in page,
          '另需人工备份调度与保留策略（系统内无备份入口）')
    _dispose(app)

    # init_db --force 是否要求确认
    src_code = open(os.path.join(PROJECT_ROOT, 'scripts', 'init_db.py'), encoding='utf-8').read()
    has_confirm = any(k in src_code for k in ('input(', 'confirm', '确认'))
    check('备份', 'init_db --force 删库前应二次确认/自动备份',
          has_confirm, '直接 os.remove 删库重建')


def _case_wal(tmp):
    print('\n########## 4. SQLite 配置 ##########')
    dbp = os.path.join(tmp, 'wal.db')
    seed_db(dbp)
    app = make_app(dbp)
    with app.app_context():
        jm = C.db.session.execute(text('PRAGMA journal_mode')).fetchone()[0]
    check('配置', '数据库运行于 WAL 模式', str(jm).lower() == 'wal', jm)
    _dispose(app)


def _case_cwd(tmp):
    print('\n########## 5. 工作目录依赖（服务以任意 CWD 启动） ##########')
    dbp = os.path.join(tmp, 'cwd.db')
    seed_db(dbp)
    upload_root = os.path.join(tmp, 'cwd_project')
    upload_dir = os.path.join(upload_root, 'instance', 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    other_root = os.path.join(tmp, 'other_cwd')
    os.makedirs(other_root, exist_ok=True)

    import app.routes as routes_module
    orig = routes_module.contract_upload_dir
    routes_module.contract_upload_dir = lambda: upload_dir
    old_cwd = os.getcwd()
    app = make_app(dbp)
    try:
        client, _ = _login_client(app)
        with open(SAMPLE_TXT, 'rb') as f:
            r = client.post('/contracts/upload', data={'file': (f, '合同样例-设备采购.txt')},
                            content_type='multipart/form-data', follow_redirects=False)
        loc = r.headers.get('Location', '')
        ct_id = int(loc.rstrip('/').split('/')[-2]) if '/confirm' in loc else None
        check('目录', '临时环境中上传合同成功', ct_id is not None, 'loc=%s' % loc)
        if ct_id:
            os.chdir(other_root)
            r = client.post('/contracts/%d/rerun' % ct_id, follow_redirects=True)
            text_body = r.get_data(as_text=True)
            check('目录', 'CWD≠项目根时「重新识别」也应正常',
                  ('已重新识别' in text_body) or ('已更新识别结果' in text_body),
                  '实际报「合同文件已丢失」（相对路径依赖 CWD）')
            os.chdir(upload_root)
            r = client.post('/contracts/%d/rerun' % ct_id, follow_redirects=True)
            text_body = r.get_data(as_text=True)
            check('目录', 'CWD=项目根时「重新识别」正常',
                  ('已重新识别' in text_body) or ('已更新识别结果' in text_body))
    finally:
        os.chdir(old_cwd)
        routes_module.contract_upload_dir = orig
        _dispose(app)


def _case_initdb():
    print('\n########## 6. init_db 删库保护与备份恢复（批次 5 验收） ##########')
    tmp = tempfile.mkdtemp(prefix='payment_qa_initdb_')
    dst = os.path.join(tmp, 'proj')
    shutil.copytree(PROJECT_ROOT, dst,
                    ignore=shutil.ignore_patterns('instance', '__pycache__', 'tests',
                                                  'docs', 'backups', '*.pyc'))
    env = os.environ.copy()
    env['SECRET_KEY'] = 'qa-initdb-secret-not-default'
    env['PAYMENT_BACKUP_DIR'] = os.path.join(tmp, 'backups')
    env.pop('MAX_UPLOAD_MB', None)
    dbp = os.path.join(dst, 'instance', 'payment_progress.db')
    log = os.path.join(tmp, 'initdb.log')

    def run(args, extra_env=None):
        run_env = dict(env)
        if extra_env:
            run_env.update(extra_env)
        with open(log, 'a', encoding='utf-8') as f:
            r = subprocess.run([sys.executable, 'scripts/init_db.py'] + args,
                               cwd=dst, env=run_env, stdin=subprocess.DEVNULL,
                               stdout=f, stderr=subprocess.STDOUT, timeout=180)
        return r.returncode

    def count(table):
        conn = sqlite3.connect(dbp)
        try:
            return conn.execute('SELECT COUNT(*) FROM %s' % table).fetchone()[0]
        finally:
            conn.close()

    def marker():
        if not os.path.exists(dbp):
            return -1
        conn = sqlite3.connect(dbp)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM projects WHERE project_no='QA-INITDB-MARKER'").fetchone()[0]
        except sqlite3.Error:
            return -2
        finally:
            conn.close()

    try:
        check('初始化', '首次初始化成功',
              run([]) == 0 and os.path.exists(dbp), '详见 %s' % log)
        conn = sqlite3.connect(dbp)
        conn.execute("INSERT INTO projects (project_no, name, status, remark, created_at) "
                     "VALUES ('QA-INITDB-MARKER', '备份标记项目', '进行中', '', datetime('now'))")
        conn.commit()
        conn.close()
        check('初始化', '--force 无确认应中止且库不丢',
              run(['--force']) != 0 and marker() == 1, '库被删或未中止')
        check('初始化', '--no-backup 未与 --yes 同用应中止',
              run(['--force', '--no-backup']) != 0 and marker() == 1, '非法组合未中止')

        rc = run(['--force', '--yes'])
        check('初始化', '--force --yes 成功并重建种子库',
              rc == 0 and marker() == 0, 'returncode=%s' % rc)
        backups = []
        for root, _, files in os.walk(tmp):
            for fn in files:
                p = os.path.join(root, fn)
                if fn.endswith('.db') and os.path.abspath(p) != os.path.abspath(dbp):
                    backups.append(p)
        check('初始化', '--force --yes 生成备份产物', bool(backups),
              '未找到备份文件，日志见 %s' % log)
        if backups:
            src = max(backups, key=os.path.getmtime)
            for suffix in ('-wal', '-shm'):
                if os.path.exists(dbp + suffix):
                    os.remove(dbp + suffix)
            shutil.copy2(src, dbp)
            check('初始化', '备份产物可恢复且数据完整（含标记项目）',
                  marker() == 1, '备份=%s' % src)
        # --no-backup 与 --yes 同用应允许，但本次不校验「无备份」细节
        check('初始化', '--force --yes --no-backup 组合可成功',
              run(['--force', '--yes', '--no-backup']) == 0)
        # 生产初始化：--no-seed 不建演示项目，只建账号；APP_ENV=production 默认不种
        rc = run(['--force', '--yes', '--no-seed'])
        check('初始化', '--no-seed 生产初始化不建演示项目、只建账号',
              rc == 0 and count('projects') == 0 and count('users') >= 3,
              'rc=%s projects=%s users=%s' % (rc, count('projects'), count('users')))
        rc = run(['--force', '--yes'], extra_env={'APP_ENV': 'production'})
        check('初始化', 'APP_ENV=production 默认不建种子项目（生产默认 no-seed）',
              rc == 0 and count('projects') == 0, 'rc=%s projects=%s' % (rc, count('projects')))
        rc = run(['--force', '--yes', '--seed'], extra_env={'APP_ENV': 'production'})
        check('初始化', '显式 --seed 优先于生产默认（仍可建演示项目）',
              rc == 0 and count('projects') >= 1, 'rc=%s projects=%s' % (rc, count('projects')))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



def _case_schema_upgrade(tmp):
    print('\n########## 7. 旧库缺列自动升级（幂等、数据不丢） ##########')
    dbp = os.path.join(tmp, 'old_schema.db')
    seed_db(dbp)
    conn = sqlite3.connect(dbp)
    conn.execute("INSERT INTO projects (project_no, name, status, remark, created_at) "
                 "VALUES ('QA-OLD-MARKER', '旧库标记项目', '进行中', '', datetime('now'))")
    conn.commit()
    drops = [('users', 'is_active'), ('users', 'must_change_password'), ('users', 'password_updated_at'),
             ('projects', 'updated_at'), ('projects', 'updated_by'),
             ('project_items', 'updated_at'), ('project_items', 'updated_by'),
             ('payment_nodes', 'updated_at'), ('payment_nodes', 'updated_by'),
             ('contract_files', 'confirmed_by'), ('contract_files', 'confirmed_at')]
    dropped = 0
    for table, col in drops:
        try:
            conn.execute('ALTER TABLE %s DROP COLUMN %s' % (table, col))
            dropped += 1
        except sqlite3.Error:
            pass
    for tbl in ('audit_logs', 'user_sessions'):
        try:
            conn.execute('DROP TABLE %s' % tbl)
        except sqlite3.Error:
            pass
    conn.commit()
    conn.close()
    try:
        app = make_app(dbp)
        client, _ = _login_client(app)
        page = client.get('/projects').get_data(as_text=True)
        check('升级', '旧库缺列启动时自动补齐且应用可用',
              dropped >= 6 and 'QA-OLD-MARKER' in page and 'PJ-2025-DSJ' in page,
              '补列前删除列数=%d' % dropped)
        conn = sqlite3.connect(dbp)
        users_cols = {r[1] for r in conn.execute('PRAGMA table_info(users)')}
        nodes_cols = {r[1] for r in conn.execute('PRAGMA table_info(payment_nodes)')}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        check('升级', '升级后补齐 is_active/updated_at 且重建 audit_logs',
              'is_active' in users_cols and 'updated_at' in nodes_cols and 'audit_logs' in tables,
              'users=%s nodes=%s' % (sorted(users_cols), sorted(nodes_cols)))
        _dispose(app)
        try:
            app2 = make_app(dbp)
            st = q1(dbp, "SELECT COUNT(*) FROM projects WHERE project_no='QA-OLD-MARKER'")
            _dispose(app2)
            check('升级', '升级幂等（重复启动不报错、数据仍在）', st == 1, 'marker=%s' % st)
        except Exception as e:
            check('升级', '升级幂等（重复启动不报错、数据仍在）', False, repr(e))
    finally:
        pass



def _subprocess_app(tmp, env_extra, code='from app import create_app; a = create_app(); print(a.config["MAX_CONTENT_LENGTH"])', pop=('SECRET_KEY', 'PAYMENT_CONFIG_FILE', 'MAX_UPLOAD_MB', 'DATA_DIR')):
    env = os.environ.copy()
    for k in pop:
        env.pop(k, None)
    env.update(env_extra)
    env['PYTHONIOENCODING'] = 'utf-8'   # 子进程统一 UTF-8，避免 cp936 混入
    env['PYTHONUTF8'] = '1'
    out = os.path.join(tmp, 'subproc_out.txt')
    with open(out, 'w', encoding='utf-8') as f:
        r = subprocess.run([sys.executable, '-c', code], cwd=PROJECT_ROOT, env=env,
                           stdout=f, stderr=subprocess.STDOUT, timeout=120)
    try:
        text = open(out, encoding='utf-8', errors='replace').read()
    except Exception:
        text = ''
    return r.returncode, text


def _case_config_and_health(tmp):
    print('\n########## 8. 配置优先级 / 启动 fail-fast / 健康检查（批次 5） ##########')
    dbp = os.path.join(tmp, 'cfg.db')
    seed_db(dbp)

    # 1) 显式 config 覆盖环境变量（已支持）
    old = os.environ.get('MAX_UPLOAD_MB')
    os.environ['MAX_UPLOAD_MB'] = '25'
    try:
        app = make_app(dbp, MAX_CONTENT_LENGTH=7 * 1024 * 1024)
        check('配置', '显式 config 覆盖环境变量',
              app.config['MAX_CONTENT_LENGTH'] == 7 * 1024 * 1024, app.config['MAX_CONTENT_LENGTH'])
        _dispose(app)
    finally:
        if old is None:
            os.environ.pop('MAX_UPLOAD_MB', None)
        else:
            os.environ['MAX_UPLOAD_MB'] = old

    # 2) 环境变量覆盖默认
    rc, text = _subprocess_app(tmp, {'SECRET_KEY': 'qa-cfg-env-secret', 'MAX_UPLOAD_MB': '25'})
    check('配置', '环境变量覆盖默认配置（MAX_UPLOAD_MB=25）',
          rc == 0 and '26214400' in text, 'rc=%s out=%s' % (rc, text[-160:]))

    # 3) PAYMENT_CONFIG_FILE 优先级（config 文件 > 默认）
    cfg_file = os.path.join(tmp, 'payment.conf')
    with open(cfg_file, 'w', encoding='utf-8') as f:
        f.write('MAX_UPLOAD_MB=30\n')
    rc, text = _subprocess_app(tmp, {'SECRET_KEY': 'qa-cfg-file-secret',
                                     'PAYMENT_CONFIG_FILE': cfg_file})
    check('配置', 'PAYMENT_CONFIG_FILE 配置生效（30MB）',
          rc == 0 and '31457280' in text, 'rc=%s out=%s' % (rc, text[-160:]))

    # 4) 缺 SECRET_KEY 启动 fail-fast
    rc, text = _subprocess_app(tmp, {})
    check('配置', '缺 SECRET_KEY 启动 fail-fast（明确报错）',
          rc != 0 and 'SECRET_KEY' in text, 'rc=%s out=%s' % (rc, text[-160:]))

    # 5) DATA_DIR 非法/不可写启动 fail-fast
    fake_dir = os.path.join(tmp, 'not_a_dir')
    with open(fake_dir, 'w', encoding='utf-8') as f:
        f.write('x')
    rc, text = _subprocess_app(tmp, {'SECRET_KEY': 'qa-cfg-data-secret', 'DATA_DIR': fake_dir})
    check('配置', 'DATA_DIR 非法时启动明确报错',
          rc != 0 and any(k in text for k in ('DATA_DIR', '数据目录')), 'rc=%s out=%s' % (rc, text[-160:]))

    # 6) 缺库/坏库：登录页中文提示 + /healthz
    missing = os.path.join(tmp, 'miss_cfg.db')
    app = make_app(missing)
    c = app.test_client()
    page = c.get('/login').get_data(as_text=True)
    check('健康检查', '数据库缺失时登录页中文提示（非 500）',
          any(k in page for k in ('数据库', '初始化', 'init_db', '备份')), '登录页无提示')
    hz = c.get('/healthz')
    check('健康检查', '数据库缺失时 /healthz 返回明确状态',
          hz.status_code in (200, 503) and any(k in hz.get_data(as_text=True).lower() for k in ('db', 'database', 'unhealthy', 'ok')),
          'code=%s' % hz.status_code)
    _dispose(app)

    corrupt = os.path.join(tmp, 'corrupt_cfg.db')
    with open(corrupt, 'wb') as f:
        f.write(b'not a database' * 50)
    app = make_app(corrupt)
    c = app.test_client()
    page = c.get('/login').get_data(as_text=True)
    check('健康检查', '数据库损坏时登录页中文提示（非 500）',
          any(k in page for k in ('数据库', '损坏', '备份', '恢复')), '登录页无提示')
    _dispose(app)


def run():
    tmp = tempfile.mkdtemp(prefix='payment_qa_db_')
    print('数据库专项临时目录: %s' % tmp)
    try:
        _case_restart_and_session(tmp)
        _case_missing_and_corrupt_db(tmp)
        _case_backup(tmp)
        _case_wal(tmp)
        _case_cwd(tmp)
        _case_initdb()
        _case_schema_upgrade(tmp)
        _case_config_and_health(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# -*- coding: utf-8 -*-
"""QA 回归公共设施。

- 所有用例默认使用临时目录数据库 + 随机端口，结束即清理；
- 绝不读写项目内的 instance/payment_progress.db；
- 结果分四类：PASS / FAIL / XFAIL（已知缺陷，预期失败）/ XPASS（缺陷疑似已修复）。
"""
import importlib.util
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

INSTANCE_DB = os.path.abspath(os.path.join(PROJECT_ROOT, 'instance', 'payment_progress.db'))
SAMPLE_TXT = os.path.join(PROJECT_ROOT, 'scripts', 'samples', '合同样例-设备采购.txt')
SAMPLE_DOCX = os.path.join(PROJECT_ROOT, 'scripts', 'samples', '合同样例-设备采购.docx')
DEFAULT_SECRET = 'payment-progress-secret-key-change-in-prod'

from werkzeug.serving import make_server  # noqa: E402

from app import create_app  # noqa: E402
from app.models import (db, User, Project, ProjectItem, PaymentNode,  # noqa: E402
                        PaymentApplication, PaymentRecord, AcceptanceRecord, ContractFile)
import app.routes as routes_module  # noqa: E402

RESULTS = []
COUNTS = {'PASS': 0, 'FAIL': 0, 'XFAIL': 0, 'XPASS': 0}

RATIOS = {'first': Decimal('0.30'), 'progress': Decimal('0.52'),
          'acceptance': Decimal('0.15'), 'warranty': Decimal('0.03')}

#: 新建项目表单的 4 个比例（百分数，CR-04 起必填、无默认值）
RATIO_FORM = {'ratio_first': '30', 'ratio_progress': '52',
              'ratio_acceptance': '15', 'ratio_warranty': '3'}

#: 当前活动测试库路径（LiveServer 启动时写入，供 post() 取节点一次性 token）
ACTIVE_DB = None


def check(section, name, ok, detail='', xfail=None):
    """登记一条检查结果。

    xfail：已知缺陷编号（如 'P0-01'）。带 xfail 的用例表示「期望的正确行为」，
    当前预期失败；若意外通过则记为 XPASS，需要更新用例与基线。
    """
    ok = bool(ok)
    if xfail:
        kind = 'XPASS' if ok else 'XFAIL'
    else:
        kind = 'PASS' if ok else 'FAIL'
    COUNTS[kind] += 1
    RESULTS.append({'section': section, 'name': name, 'kind': kind, 'ok': ok,
                    'xfail': xfail, 'detail': str(detail)[:800]})
    tag = '[%s%s]' % (kind, (' ' + xfail) if xfail else '')
    line = '  %-12s %s' % (tag, name)
    if detail and kind != 'PASS':
        line += ' | ' + str(detail)[:220]
    print(line)
    return ok


# ---------- 数据库 ----------

def make_app(db_path=None, **extra):
    cfg = {}
    if db_path:
        cfg['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
    cfg.update(extra)
    return create_app(cfg)


def _load_init_db():
    """复用生产 init_db.py 的种子数据构建逻辑（只调用函数，不执行 main）。"""
    path = os.path.join(PROJECT_ROOT, 'scripts', 'init_db.py')
    spec = importlib.util.spec_from_file_location('qa_init_db', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed_db(db_path):
    """在指定（临时）数据库文件里建表并写入默认用户 + 种子项目。"""
    if os.path.abspath(db_path) == INSTANCE_DB:
        raise RuntimeError('拒绝在项目实例库上执行测试种子，请使用临时目录数据库')
    app = make_app(db_path)
    with app.app_context():
        db.create_all()
        users = [
            ('admin', '系统管理员', 'admin123', 'admin'),
            ('jbr', '经办人', '123456', 'operator'),
            ('viewer', '查看人员', '123456', 'viewer'),
        ]
        for username, display, pwd, role in users:
            u = User(username=username, display_name=display, role=role)
            u.set_password(pwd)
            db.session.add(u)
        db.session.flush()
        project = _load_init_db().build_seed_project()
        db.session.flush()
        project.trial_date = date(2025, 11, 4)
        for node in project.nodes:
            if node.node_type == 'progress':
                node.trigger_date = date(2025, 11, 4)
        db.session.commit()
        db.session.remove()
        db.engine.dispose()


class LiveServer:
    """在临时目录起一个真实 HTTP 服务（随机端口），结束后清理。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix='payment_qa_')
        self.db_path = os.path.join(self.tmp, 'qa.db')
        self.upload_dir = os.path.join(self.tmp, 'instance', 'uploads')
        os.makedirs(self.upload_dir)
        seed_db(self.db_path)
        assert os.path.abspath(self.db_path) != INSTANCE_DB
        global ACTIVE_DB
        ACTIVE_DB = self.db_path
        self.app = make_app(self.db_path, SECRET_KEY=DEFAULT_SECRET)
        self._orig_upload_dir = routes_module.contract_upload_dir
        routes_module.contract_upload_dir = lambda: self.upload_dir
        self.server = make_server('127.0.0.1', 0, self.app, threaded=True)
        self.port = self.server.server_port
        self.base = 'http://127.0.0.1:%d' % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        try:
            self.server.shutdown()
            self.thread.join(timeout=5)
        finally:
            routes_module.contract_upload_dir = self._orig_upload_dir
            shutil.rmtree(self.tmp, ignore_errors=True)


# ---------- SQL / HTTP 辅助 ----------

def q(db_path, sql, args=()):
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def q1(db_path, sql, args=()):
    rows = q(db_path, sql, args)
    return rows[0][0] if rows else None


def login(base, user, pwd):
    import requests
    sess = requests.Session()
    r = sess.post(base + '/login', data={'username': user, 'password': pwd},
                  allow_redirects=False, timeout=30)
    return sess, r


#: 项目详情页里每个节点的三个表单：action 值 + 一次性 confirm_token
NODE_FORM_RE = re.compile(
    r'action="/nodes/(\d+)/update".*?name="action" value="([^"]+)"'
    r'.*?name="confirm_token" value="([^"]+)"', re.S)


def node_tokens(sess, base, project_id):
    """GET 项目详情页并解析 ``{(node_id, action): token}``（CR-04 一次性凭据）。"""
    html = sess.get(base + '/projects/%d' % project_id, timeout=60).text
    return {(int(nid), action): token
            for nid, action, token in NODE_FORM_RE.findall(html)}


def _inject_node_token(sess, base, path, data):
    """节点动作自动补齐详情页签发的 confirm_token。

    CR-04 起 ``/nodes/<id>/update`` 需要一次性 token；用例不需要逐处手写。
    显式传了 ``confirm_token``（含空串/伪造值）时不做补齐，便于测令牌语义。
    """
    match = re.match(r'^/nodes/(\d+)/update$', path or '')
    if not match or not isinstance(data, dict) or 'confirm_token' in data:
        return data
    if not ACTIVE_DB:
        return data
    node_id = int(match.group(1))
    project_id = q1(ACTIVE_DB, 'SELECT project_id FROM payment_nodes WHERE id=?',
                    (node_id,))
    if not project_id:
        return data
    tokens = node_tokens(sess, base, project_id)
    token = tokens.get((node_id, (data.get('action') or '').strip()))
    if token:
        data = dict(data)
        data['confirm_token'] = token
    return data


_APPLY_SEQ = [0]


def next_apply_no(prefix='QA'):
    """CR-05 需求③：申请单号人工必填后，用例默认生成全局唯一单号。"""
    import uuid
    _APPLY_SEQ[0] += 1
    return '%s-%d-%s' % (prefix, _APPLY_SEQ[0], uuid.uuid4().hex[:8])


def _inject_apply_no(data):
    """action=apply 时自动补唯一单号（显式传了就不动，便于测单号语义）。"""
    if isinstance(data, dict) and (data.get('action') or '').strip() == 'apply' \
            and not data.get('apply_no'):
        data = dict(data)
        data['apply_no'] = next_apply_no()
    return data


def post(sess, base, path, data=None, files=None, **kw):
    data = _inject_apply_no(data)
    data = _inject_node_token(sess, base, path, data)
    kw.setdefault('allow_redirects', False)
    kw.setdefault('timeout', 120)
    return sess.post(base + path, data=data, files=files, **kw)


#: 浏览器单 Cookie 上限（Werkzeug 超过 4093 字节即告警，浏览器会静默丢弃）
COOKIE_LIMIT = 4093


def browser_get(sess, url, limit=COOKIE_LIMIT):
    """GET 并按真实浏览器规则处理会话 Cookie：新 Cookie 超限时静默丢弃（保留旧值）。

    返回 ``(response, dropped)``。用于复现「令牌失效、刷新也不恢复」类问题：
    浏览器丢弃新 Cookie 后服务端会话停在旧快照，页面渲染出的令牌随之失效。
    """
    before = sess.cookies.get('session')
    resp = sess.get(url, timeout=60)
    after = sess.cookies.get('session')
    dropped = after is not None and len(after) > limit
    if dropped and before is not None:
        host = resp.url.split('/')[2].rsplit(':', 1)[0]
        sess.cookies.set('session', before, domain=host, path='/')
    return resp, dropped


def d2(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

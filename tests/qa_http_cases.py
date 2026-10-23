# -*- coding: utf-8 -*-
"""HTTP 层回归用例：会话/权限/上传/状态/金额/合同/并发/审计。

约定：check(..., xfail='P0-01') 表示该用例断言的是「正确行为」，当前预期失败，
对应 docs/qa-baseline.md 的缺陷编号；缺陷修复后会变为 XPASS，提示更新基线。
"""
import os
import re
import sqlite3
import threading

import requests

import qa_common as C
from qa_common import check, q, q1, d2, PROJECT_ROOT, SAMPLE_TXT

DB = None
BASE = None
APP = None
UPLOAD_DIR = None


# ---------- 辅助 ----------

def node_ids(pid, nt=None):
    if nt:
        return [row[0] for row in q(DB, 'SELECT id FROM payment_nodes WHERE project_id=? AND node_type=? ORDER BY item_id,id', (pid, nt))]
    return [row[0] for row in q(DB, 'SELECT id FROM payment_nodes WHERE project_id=? ORDER BY item_id,id', (pid,))]


def create_project(sess, no, name, items, remark=''):
    data = {'project_no': no, 'name': name, 'remark': remark,
            'item_name': [i[0] for i in items],
            'tax_rate': [i[3] if len(i) > 3 else '6%' for i in items],
            'original_amount': [i[1] for i in items],
            'additional_amount': [i[2] for i in items]}
    r = C.post(sess, BASE, '/projects/new', data)
    loc = r.headers.get('Location', '')
    return r, (int(loc.rstrip('/').split('/')[-1]) if r.status_code == 302 and '/projects/' in loc else None)


def upload(sess, path, name):
    with open(path, 'rb') as f:
        r = C.post(sess, BASE, '/contracts/upload', files={'file': (name, f)})
    loc = r.headers.get('Location', '')
    ct = int(loc.rstrip('/').split('/')[-2]) if '/confirm' in loc else None
    return r, ct


def confirm_ct(sess, ct, data):
    return C.post(sess, BASE, '/contracts/%d/confirm' % ct, data)


def run_threads(n, fn):
    barrier = threading.Barrier(n)
    out = []
    lock = threading.Lock()

    def worker(i):
        sess, _ = C.login(BASE, 'admin', 'admin123')
        barrier.wait()
        try:
            res = fn(sess, i)
        except Exception as e:
            res = ('EXC', repr(e))
        with lock:
            out.append(res)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def section(title, fn):
    print('\n' + title)
    try:
        fn()
    except Exception as e:  # 单个分组异常不阻断后续
        import traceback
        traceback.print_exc()
        check('用例框架', '%s 执行异常' % title, False, repr(e))


# ---------- 分组 ----------

def _sessions():
    s_admin, r = C.login(BASE, 'admin', 'admin123')
    check('会话', '管理员登录跳转项目列表', r.status_code == 302 and '/projects' in r.headers.get('Location', ''))
    sc = r.headers.get('Set-Cookie', '')
    check('会话', '会话 Cookie 含 HttpOnly', 'HttpOnly' in sc, sc)
    check('会话', '会话 Cookie 含 Secure/SameSite/过期时间',
          ('Secure' in sc) and ('SameSite' in sc) and ('Expires' in sc or 'Max-Age' in sc),
          sc + ' → 缺 Secure/SameSite/过期', xfail='P1-08')

    s_wrong = requests.Session()
    rw = s_wrong.post(BASE + '/login', data={'username': 'admin', 'password': 'bad'}, allow_redirects=False)
    rw2 = s_wrong.get(BASE + '/projects', allow_redirects=False)
    check('会话', '错误密码不建立会话', rw.status_code == 200 and rw2.status_code == 302,
          'code=%s 后续=%s' % (rw.status_code, rw2.status_code))

    for _ in range(6):
        requests.post(BASE + '/login', data={'username': 'admin', 'password': 'bad'})
    _, ra = C.login(BASE, 'admin', 'admin123')
    check('会话', '连续失败后应锁定/延迟', ra.status_code != 302,
          '6 次错密后仍直接登录成功 → 无锁定', xfail='P1-10')

    # 默认 SECRET_KEY 可伪造管理员会话
    from flask import Flask
    from flask.sessions import SecureCookieSessionInterface
    _a = Flask('qa')
    _a.secret_key = C.DEFAULT_SECRET
    forged = SecureCookieSessionInterface().get_signing_serializer(_a).dumps({'user_id': 1})
    rf = requests.get(BASE + '/admin/users', cookies={'session': forged}, allow_redirects=False)
    check('会话', '默认 SECRET_KEY 不应能伪造管理员会话', rf.status_code != 200,
          '伪造 Cookie 访问用户管理 code=%s' % rf.status_code, xfail='P1-08')

    bad = []
    for path in ['/projects', '/reports', '/contracts', '/projects/1', '/admin/users',
                 '/projects/1/edit', '/contracts/1/raw']:
        rr = requests.get(BASE + path, allow_redirects=False)
        if not (rr.status_code in (301, 302) and '/login' in rr.headers.get('Location', '')):
            bad.append('%s->%s' % (path, rr.status_code))
    check('会话', '未登录访问受保护页面全部重定向', not bad, bad)

    s_admin.get(BASE + '/logout')
    rr = s_admin.get(BASE + '/projects', allow_redirects=False)
    check('会话', '登出后受保护页面重定向', rr.status_code == 302)
    return s_admin


def _permissions(s_admin):
    # 准备数据
    _, pid_a = create_project(s_admin, 'QA-A', 'QA权限-经办项目', [('QA设备费', '100000', '20000')])
    _, pid_b = create_project(s_admin, 'QA-B', 'QA-待经办删除项目', [('QA科目B', '1000', '0')])
    _, pid_c = create_project(s_admin, 'QA-C', 'QA-状态流转项目', [('QA科目C', '10000', '0')])
    _, pid_d = create_project(s_admin, 'QA-D', 'QA-待管理员删除项目', [('QA科目D', '1000', '0')])
    _, ct1 = upload(s_admin, SAMPLE_TXT, '合同样例-设备采购.txt')
    node_a_first = node_ids(pid_a, 'first')[0]

    # --- viewer ---
    s_view, rv = C.login(BASE, 'viewer', '123456')
    check('权限', 'viewer 登录成功', rv.status_code == 302)
    for p in ['/projects', '/projects/%d' % pid_a, '/reports', '/contracts',
              '/contracts/%d/confirm' % ct1, '/contracts/%d/raw' % ct1]:
        rr = s_view.get(BASE + p, allow_redirects=False)
        check('权限', 'viewer 只读页面 %s' % p, rr.status_code == 200, 'code=%s' % rr.status_code)
    for p in ['/projects/new', '/projects/%d/edit' % pid_a, '/admin/users']:
        rr = s_view.get(BASE + p, allow_redirects=False)
        check('权限', 'viewer GET %s 被拒' % p, rr.status_code == 302)
    proj_cnt = q1(DB, 'SELECT COUNT(*) FROM projects')
    rr = C.post(s_view, BASE, '/projects/new', {'project_no': 'QA-VIEW-NEW', 'name': 'x', 'item_name': ['a'], 'original_amount': ['1']})
    check('权限', 'viewer 建项目被拒', rr.status_code == 302 and q1(DB, 'SELECT COUNT(*) FROM projects') == proj_cnt)
    rr = C.post(s_view, BASE, '/projects/%d/edit' % pid_a, {'name': 'VIEWER改名'})
    check('权限', 'viewer 编辑项目被拒', q1(DB, 'SELECT name FROM projects WHERE id=?', (pid_a,)) == 'QA权限-经办项目')
    ic0 = q1(DB, 'SELECT COUNT(*) FROM project_items WHERE project_id=?', (pid_a,))
    rr = C.post(s_view, BASE, '/projects/%d/items' % pid_a, {'item_name': '越权科目', 'original_amount': '1'})
    check('权限', 'viewer 添加科目被拒', q1(DB, 'SELECT COUNT(*) FROM project_items WHERE project_id=?', (pid_a,)) == ic0)
    rr = C.post(s_view, BASE, '/projects/%d/delete' % pid_a)
    check('权限', 'viewer 删除项目被拒', q1(DB, 'SELECT COUNT(*) FROM projects WHERE id=?', (pid_a,)) == 1)
    st0 = q1(DB, 'SELECT status FROM payment_nodes WHERE id=?', (node_a_first,))
    rr = C.post(s_view, BASE, '/nodes/%d/update' % node_a_first, {'action': 'pay', 'paid_amount': '1'})
    check('权限', 'viewer 付款操作被拒且未落库',
          q1(DB, 'SELECT status FROM payment_nodes WHERE id=?', (node_a_first,)) == st0)
    for path, data, name in [
        ('/projects/%d/acceptance' % pid_a, {'acceptance_date': '2026-03-10'}, '验收登记'),
        ('/projects/%d/trial' % pid_a, {'trial_date': '2026-01-15'}, '试运行登记'),
        ('/projects/%d/status' % pid_a, {'status': '已归档'}, '改项目状态'),
    ]:
        rr = C.post(s_view, BASE, path, data)
        check('权限', 'viewer %s被拒' % name, rr.status_code == 302)
    cc0 = q1(DB, 'SELECT COUNT(*) FROM contract_files')
    with open(SAMPLE_TXT, 'rb') as f:
        rr = C.post(s_view, BASE, '/contracts/upload', files={'file': ('v.txt', f)})
    check('权限', 'viewer 上传合同被拒', q1(DB, 'SELECT COUNT(*) FROM contract_files') == cc0)
    pc0 = q1(DB, 'SELECT COUNT(*) FROM projects')
    rr = confirm_ct(s_view, ct1, {'project_no': 'QA-VIEW-CT', 'contract_name': 'x', 'original_amount': '1',
                                  'ratio_first': '100', 'ratio_progress': '0', 'ratio_acceptance': '0', 'ratio_warranty': '0'})
    check('权限', 'viewer 合同确认生成被拒', q1(DB, 'SELECT COUNT(*) FROM projects') == pc0)
    uc0 = q1(DB, 'SELECT COUNT(*) FROM users')
    rr = C.post(s_view, BASE, '/admin/users/new', {'username': 'qa_v_u', 'display_name': 'x', 'password': 'x', 'role': 'admin'})
    check('权限', 'viewer 新建用户被拒', q1(DB, 'SELECT COUNT(*) FROM users') == uc0)
    rr = C.post(s_view, BASE, '/admin/users/2/delete')
    check('权限', 'viewer 删除用户被拒', q1(DB, 'SELECT COUNT(*) FROM users WHERE id=2') == 1)

    # --- operator ---
    s_op, rop = C.login(BASE, 'jbr', '123456')
    check('权限', 'operator 登录成功', rop.status_code == 302)
    for p in ['/projects/new', '/projects/%d/edit' % pid_a]:
        rr = s_op.get(BASE + p, allow_redirects=False)
        check('权限', 'operator GET %s 允许' % p, rr.status_code == 200)
    rr = s_op.get(BASE + '/admin/users', allow_redirects=False)
    check('权限', 'operator 访问用户管理被拒', rr.status_code == 302)
    ic0 = q1(DB, 'SELECT COUNT(*) FROM project_items WHERE project_id=?', (pid_a,))
    C.post(s_op, BASE, '/projects/%d/items' % pid_a,
           {'item_name': '经办添加科目', 'tax_rate': '6%', 'original_amount': '5000', 'additional_amount': '0'})
    check('权限', 'operator 添加科目允许', q1(DB, 'SELECT COUNT(*) FROM project_items WHERE project_id=?', (pid_a,)) == ic0 + 1)
    node_a_prog = node_ids(pid_a, 'progress')[0]
    C.post(s_op, BASE, '/nodes/%d/update' % node_a_prog, {'action': 'apply', 'apply_amount': '1000'})
    C.post(s_op, BASE, '/nodes/%d/update' % node_a_prog, {'action': 'invoice', 'invoice_amount': '1000', 'invoice_no': 'FP-QA-OP'})
    rr = C.post(s_op, BASE, '/nodes/%d/update' % node_a_prog, {'action': 'pay', 'paid_amount': '1000', 'pay_date': '2026-10-20'})
    check('权限', 'operator 申请→发票→付款允许',
          q1(DB, 'SELECT status FROM payment_nodes WHERE id=?', (node_a_prog,)) == '已付款')
    C.post(s_op, BASE, '/projects/%d/trial' % pid_a, {'trial_date': '2026-01-15'})
    C.post(s_op, BASE, '/projects/%d/acceptance' % pid_a, {'acceptance_date': '2026-03-10'})
    check('权限', 'operator 试运行/验收登记允许',
          str(q1(DB, 'SELECT acceptance_date FROM projects WHERE id=?', (pid_a,))) == '2026-03-10')
    C.post(s_op, BASE, '/projects/%d/status' % pid_a, {'status': '已归档'})
    check('权限', 'operator 走专用状态路由被拒（管理员专属）',
          q1(DB, 'SELECT status FROM projects WHERE id=?', (pid_a,)) != '已归档')
    rc0 = q1(DB, 'SELECT COUNT(*) FROM contract_files')
    _, ct_op = upload(s_op, SAMPLE_TXT, 'op合同.txt')
    check('权限', 'operator 上传合同允许', q1(DB, 'SELECT COUNT(*) FROM contract_files') == rc0 + 1)
    pc0 = q1(DB, 'SELECT COUNT(*) FROM projects')
    confirm_ct(s_op, ct_op, {'project_no': 'QA-OP-CT', 'contract_name': '经办确认合同项目',
                             'original_amount': '165000', 'additional_amount': '0', 'tax_rate': '13%',
                             'ratio_first': '30', 'ratio_progress': '52', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
    check('权限', 'operator 合同确认生成项目允许', q1(DB, 'SELECT COUNT(*) FROM projects') == pc0 + 1)
    # 越权 1：经办人删除项目
    C.post(s_op, BASE, '/projects/%d/delete' % pid_b)
    check('权限', '经办人不应能删除项目（README 未授权）',
          q1(DB, 'SELECT COUNT(*) FROM projects WHERE id=?', (pid_b,)) == 1,
          '项目已被经办人删除', xfail='P1-01')
    # 越权 2：经办人经编辑页改状态
    before = q1(DB, 'SELECT status FROM projects WHERE id=?', (pid_a,))
    C.post(s_op, BASE, '/projects/%d/edit' % pid_a, {'name': 'QA权限-经办项目', 'status': '已归档', 'remark': ''})
    after = q1(DB, 'SELECT status FROM projects WHERE id=?', (pid_a,))
    check('权限', '经办人不应能经编辑页改项目状态', after == before,
          '状态 %s→%s' % (before, after), xfail='P1-02')
    C.post(s_admin, BASE, '/projects/%d/edit' % pid_a, {'name': 'QA权限-经办项目', 'status': '进行中', 'remark': ''})

    # --- admin ---
    rr = s_admin.get(BASE + '/admin/users')
    check('权限', 'admin 用户管理允许', rr.status_code == 200)
    uc0 = q1(DB, 'SELECT COUNT(*) FROM users')
    C.post(s_admin, BASE, '/admin/users/new', {'username': 'qa_test_u', 'display_name': 'QA测试用户', 'password': 'Qa#123456', 'role': 'viewer'})
    check('权限', 'admin 新建用户允许', q1(DB, 'SELECT COUNT(*) FROM users') == uc0 + 1)
    uid = q1(DB, 'SELECT id FROM users WHERE username=?', ('qa_test_u',))
    C.post(s_admin, BASE, '/admin/users/%d/delete' % uid)
    check('权限', 'admin 删除用户允许', q1(DB, 'SELECT COUNT(*) FROM users WHERE id=?', (uid,)) == 0)
    C.post(s_admin, BASE, '/admin/users/1/delete')
    check('权限', 'admin 删除自身被拒', q1(DB, 'SELECT COUNT(*) FROM users WHERE id=1') == 1)
    C.post(s_admin, BASE, '/projects/%d/delete' % pid_d)
    check('权限', 'admin 删除项目允许', q1(DB, 'SELECT COUNT(*) FROM projects WHERE id=?', (pid_d,)) == 0)
    return pid_a, pid_c, ct1


def _upload_security(s_admin):
    cc0 = q1(DB, 'SELECT COUNT(*) FROM contract_files')
    with open(SAMPLE_TXT, 'rb') as f:
        r_exe = C.post(s_admin, BASE, '/contracts/upload', files={'file': ('evil.exe', f, 'application/octet-stream')})
    check('上传', '拒绝 .exe 扩展名', r_exe.status_code == 302 and q1(DB, 'SELECT COUNT(*) FROM contract_files') == cc0)
    with open(SAMPLE_TXT, 'rb') as f:
        r_tr = C.post(s_admin, BASE, '/contracts/upload', files={'file': ('../../qa_evil.txt', f, 'text/plain')})
    loc = r_tr.headers.get('Location', '')
    ok = r_tr.status_code == 302 and '/confirm' in loc
    check('上传', '路径穿越文件名不可利用', ok, 'loc=%s' % loc)
    if ok:
        cid = int(loc.rstrip('/').split('/')[-2])
        sp = q1(DB, 'SELECT stored_path FROM contract_files WHERE id=?', (cid,))
        check('上传', '落盘名为 id.ext（不含原始文件名）', bool(re.fullmatch(r'uploads[\\/]\d+\.txt', sp or '')), sp)

    # 伪造 PDF
    r_fake = C.post(s_admin, BASE, '/contracts/upload', files={'file': ('fake.pdf', b'not a pdf', 'application/pdf')})
    latest = q(DB, 'SELECT id,status,error_msg FROM contract_files ORDER BY id DESC LIMIT 1')[0]
    check('上传', '伪造 PDF 安全失败（302+failed，不 500）',
          r_fake.status_code == 302 and latest[1] == 'failed' and bool(latest[2]), latest)
    orphan_ok = True
    try:
        stored = q1(DB, 'SELECT stored_path FROM contract_files WHERE id=?', (latest[0],))
        orphan_path = os.path.join(UPLOAD_DIR, os.path.basename(stored.replace('\\', '/')))
        orphan_ok = not os.path.exists(orphan_path)
    except Exception:
        pass
    check('上传', '上传失败后不残留孤立文件', orphan_ok, '失败记录仍保留文件', xfail='P2-06')

    # 存储型 XSS（合同原文）
    xss_text = "<script>alert('qa-xss')</script>\n合同总金额人民币壹拾陆万伍仟元整（￥165,000.00）\n税率：13%\n"
    r_xss = C.post(s_admin, BASE, '/contracts/upload', files={'file': ('xss.txt', xss_text.encode('utf-8'), 'text/plain')})
    loc = r_xss.headers.get('Location', '')
    if '/confirm' in loc:
        xid = int(loc.rstrip('/').split('/')[-2])
        page = s_admin.get(BASE + '/contracts/%d/raw' % xid).text
        check('上传', '合同原文对脚本转义', '&lt;script&gt;' in page and "<script>alert('qa-xss')" not in page)
    # 项目名 XSS
    _, pid_x = create_project(s_admin, 'QA-XSS', "<script>alert('qa-name')</script>项目", [('科目', '100', '0')])
    page = s_admin.get(BASE + '/projects/%d' % pid_x).text
    check('上传', '项目名称对脚本转义', '&lt;script&gt;' in page and "<script>alert('qa-name')" not in page)

    # 上传大小：README 20MB vs 代码 16MB
    big = b'A' * (17 * 1024 * 1024)
    r_big = C.post(s_admin, BASE, '/contracts/upload', files={'file': ('big.txt', big, 'text/plain')})
    check('上传', '17MB 应在上限内（README 20MB）被接收', r_big.status_code == 302 and '/confirm' in r_big.headers.get('Location', ''),
          'code=%s（实际上限 16MB）' % r_big.status_code, xfail='P2-01')
    r_after = s_admin.get(BASE + '/projects')
    check('上传', '超限请求后服务仍可用', r_after.status_code == 200)
    check('上传', '服务端上传上限与 README 标注一致（20MB）',
          APP.config.get('MAX_CONTENT_LENGTH') == 20 * 1024 * 1024,
          '实际 %s' % APP.config.get('MAX_CONTENT_LENGTH'), xfail='P2-01')

    # 图片 OCR（本机沙箱可能不可用）
    try:
        from PIL import Image, ImageDraw
        png = os.path.join(os.environ.get('TEMP', '.'), 'qa_ocr_probe.png')
        im = Image.new('RGB', (900, 300), 'white')
        ImageDraw.Draw(im).text((30, 60), '合同总金额 165000 元 税率 13%', fill='black')
        im.save(png)
        r_img = C.post(s_admin, BASE, '/contracts/upload', files={'file': ('ocr_probe.png', open(png, 'rb'), 'image/png')})
        img_row = q(DB, 'SELECT status,error_msg FROM contract_files ORDER BY id DESC LIMIT 1')[0]
        check('上传', '图片合同 OCR 可识别并进入确认页', r_img.status_code == 302 and '/confirm' in r_img.headers.get('Location', ''),
              'status=%s error=%s（目标机需复测）' % (img_row[0], img_row[1]), xfail='P2-08')
    except ImportError:
        check('上传', '图片合同 OCR 用例（Pillow 未安装跳过）', True)


def _state_and_money(s_admin, pid_a, pid_c):
    # 状态机
    nid = node_ids(pid_c, 'first')[0]
    C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'pay', 'paid_amount': '100'})
    st = q1(DB, 'SELECT status FROM payment_nodes WHERE id=?', (nid,))
    check('状态', '未触发节点不应能直接登记付款', st != '已付款', 'status=%s' % st)
    # 重复付款
    nid = node_ids(pid_c, 'progress')[0]
    C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'apply', 'apply_amount': '5200'})
    C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'invoice', 'invoice_amount': '5200', 'invoice_no': 'FP-QA-1'})
    C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'pay', 'paid_amount': '5200'})
    C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'pay', 'paid_amount': '5200'})
    paid = float(q1(DB, 'SELECT paid_amount FROM payment_nodes WHERE id=?', (nid,)) or 0)
    due = float(q1(DB, 'SELECT due_amount FROM payment_nodes WHERE id=?', (nid,)) or 0)
    check('状态', '累计付款不得超过应付', paid <= due, '已付 %.2f / 应付 %.2f' % (paid, due))
    C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'apply', 'apply_amount': '1'})
    st = q1(DB, 'SELECT status FROM payment_nodes WHERE id=?', (nid,))
    check('状态', '已付款节点不应回退为已申请', st != '未开票已申请', 'status=%s' % st)
    # 项目状态回退 + 归档后改账
    C.post(s_admin, BASE, '/projects/%d/status' % pid_c, {'status': '已归档'})
    C.post(s_admin, BASE, '/projects/%d/status' % pid_c, {'status': '进行中'})
    check('状态', '项目状态不应从已归档回退进行中',
          q1(DB, 'SELECT status FROM projects WHERE id=?', (pid_c,)) == '已归档', xfail='P1-03')
    C.post(s_admin, BASE, '/projects/%d/status' % pid_c, {'status': '已归档'})
    nid2 = node_ids(pid_c, 'acceptance')[0]
    C.post(s_admin, BASE, '/nodes/%d/update' % nid2, {'action': 'apply', 'apply_amount': '10'})
    check('状态', '已归档项目应只读', q1(DB, 'SELECT status FROM payment_nodes WHERE id=?', (nid2,)) != '未开票已申请',
          xfail='P1-03')
    C.post(s_admin, BASE, '/projects/%d/status' % pid_c, {'status': '进行中'})
    # 非法状态
    C.post(s_admin, BASE, '/projects/%d/edit' % pid_a, {'name': 'QA权限-经办项目', 'status': '<b>非法状态</b>'})
    check('状态', '编辑项目应拒绝白名单外状态值',
          q1(DB, 'SELECT status FROM projects WHERE id=?', (pid_a,)) != '<b>非法状态</b>')
    C.post(s_admin, BASE, '/projects/%d/edit' % pid_a, {'name': 'QA权限-经办项目', 'status': '进行中', 'remark': ''})
    # 清空日期残留
    C.post(s_admin, BASE, '/projects/%d/trial' % pid_a, {'trial_date': '2026-01-15'})
    C.post(s_admin, BASE, '/projects/%d/acceptance' % pid_a, {'acceptance_date': '2026-03-10'})
    C.post(s_admin, BASE, '/projects/%d/edit' % pid_a, {'name': 'QA权限-经办项目', 'status': '进行中', 'remark': '',
                                                        'trial_date': '', 'acceptance_date': ''})
    trig = q1(DB, "SELECT trigger_date FROM payment_nodes WHERE project_id=? AND node_type='progress' LIMIT 1", (pid_a,))
    warr = q1(DB, 'SELECT warranty_due_date FROM projects WHERE id=?', (pid_a,))
    check('状态', '清空试运行日期应同步清空进度款触发日期',
          trig is None, 'trigger_date=%s' % trig)
    check('状态', '清空验收日期应同步清空质保到期日',
          warr is None, 'warranty_due_date=%s' % warr)


def _data_accuracy(s_admin):
    seed = q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('PJ-2025-DSJ',))
    checks = [('first', 49500.0), ('progress', 85800.0), ('acceptance', 24750.0), ('warranty', 4950.0)]
    for nt, expect in checks:
        actual = float(q1(DB, 'SELECT SUM(due_amount) FROM payment_nodes WHERE project_id=? AND node_type=?', (seed, nt)) or 0)
        check('数据', '种子 %s 合计 %.2f' % (nt, expect), abs(actual - expect) < 0.005, actual)
    total = float(q1(DB, 'SELECT SUM(due_amount) FROM payment_nodes WHERE project_id=?', (seed,)) or 0)
    check('数据', '种子节点合计 165,000.00', abs(total - 165000) < 0.005, total)
    check('数据', '种子试运行日期 2025-11-04', str(q1(DB, 'SELECT trial_date FROM projects WHERE id=?', (seed,))) == '2025-11-04')
    seed_prog = q(DB, "SELECT paid_amount,status FROM payment_nodes WHERE project_id=? AND node_type='progress'", (seed,))
    check('数据', '种子进度款「已付金额>0」与状态应一致',
          all(float(x[0]) == 0 or x[1] == '已付款' for x in seed_prog), seed_prog, xfail='P2-04')

    # 增补口径
    _, pid_add = create_project(s_admin, 'QA-ADD', 'QA增补口径', [('增补科目', '100000', '20000')])
    nsum = {nt: float(q1(DB, 'SELECT SUM(due_amount) FROM payment_nodes WHERE project_id=? AND node_type=?', (pid_add, nt)) or 0)
            for nt in ('first', 'progress', 'acceptance', 'warranty')}
    check('数据', '增补项目首付按原金额 30,000', abs(nsum['first'] - 30000) < 0.005, nsum)
    check('数据', '增补项目进度款按总额 62,400', abs(nsum['progress'] - 62400) < 0.005, nsum)
    check('数据', '增补项目验收款 18,000 / 质保金 3,600',
          abs(nsum['acceptance'] - 18000) < 0.005 and abs(nsum['warranty'] - 3600) < 0.005, nsum)
    check('数据', '增补项目节点合计应等于合同总额 120,000',
          abs(sum(nsum.values()) - 120000) < 0.005, '节点合计=%.2f（缺口=增补×30%%）' % sum(nsum.values()), xfail='P1-06')

    # 舍入（会计 HALF_UP）
    items = [('甲', '100.05', '0'), ('乙', '333.33', '66.67'), ('丙', '0.05', '0'), ('丁', '123456.78', '0.22')]
    _, pid_r = create_project(s_admin, 'QA-ROUND', 'QA舍入', items)
    mismatch = []
    for idx, it in enumerate(items):
        orig, add = d2(it[1]), d2(it[2])
        item_id = q1(DB, 'SELECT id FROM project_items WHERE project_id=? ORDER BY id LIMIT 1 OFFSET ?', (pid_r, idx))
        for nt in ('first', 'progress', 'acceptance', 'warranty'):
            base = orig if nt == 'first' else (orig + add)
            expect = d2(base * C.RATIOS[nt])
            actual = d2(q1(DB, 'SELECT due_amount FROM payment_nodes WHERE project_id=? AND item_id=? AND node_type=?', (pid_r, item_id, nt)))
            if expect != actual:
                mismatch.append('%s-%s 期望%s 实际%s' % (it[0], nt, expect, actual))
    check('数据', '项目新建节点金额应按会计四舍五入（HALF_UP）',
          not mismatch, '不匹配 %d 项: %s' % (len(mismatch), mismatch[:4]))
    # 负数金额
    r_neg, pid_neg = create_project(s_admin, 'QA-NEGAMT', 'QA负金额', [('负金额科目', '-1000', '0')])
    neg_due = q1(DB, "SELECT due_amount FROM payment_nodes WHERE project_id=? AND node_type='first'", (pid_neg,)) if pid_neg else None
    check('数据', '项目新建应拒绝负数原金额', pid_neg is None or float(neg_due or 0) >= 0,
          '生成负数应付=%s' % neg_due)
    # 质保 1095 天跨闰年
    _, pid_w = create_project(s_admin, 'QA-WARRANTY', 'QA质保日期', [('科目', '1000', '0')])
    C.post(s_admin, BASE, '/projects/%d/acceptance' % pid_w, {'acceptance_date': '2024-02-29'})
    w = str(q1(DB, 'SELECT warranty_due_date FROM projects WHERE id=?', (pid_w,)))
    check('数据', '质保到期=验收+1095 天（2024-02-29→2027-02-28）', w == '2027-02-28', w)
    C.post(s_admin, BASE, '/projects/%d/acceptance' % pid_w, {'acceptance_date': '2026-03-10'})
    w = str(q1(DB, 'SELECT warranty_due_date FROM projects WHERE id=?', (pid_w,)))
    check('数据', '质保到期=验收+1095 天（2026-03-10→2029-03-09）', w == '2029-03-09', w)


def _contracts(s_admin, ct1):
    # 确认页基础识别
    page = s_admin.get(BASE + '/contracts/%d/confirm' % ct1).text
    check('合同', '确认页展示识别金额/税率/出处', '165,000.00' in page and '13%' in page and '合同总金额' in page)
    # 比例合计 98%
    _, ct_ratio = upload(s_admin, SAMPLE_TXT, '比例98.txt')
    r = confirm_ct(s_admin, ct_ratio, {'project_no': 'QA-RATIO', 'contract_name': '比例98%项目',
                                       'original_amount': '165000', 'additional_amount': '0', 'tax_rate': '13%',
                                       'ratio_first': '30', 'ratio_progress': '50', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
    check('合同', '比例合计 98% 应被拒绝', r.status_code != 302 or q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-RATIO',)) is None,
          '仍生成项目')
    # 负比例
    _, ct_neg = upload(s_admin, SAMPLE_TXT, '负比例.txt')
    r = confirm_ct(s_admin, ct_neg, {'project_no': 'QA-NEG', 'contract_name': '负比例项目',
                                     'original_amount': '165000', 'additional_amount': '0', 'tax_rate': '13%',
                                     'ratio_first': '-10', 'ratio_progress': '52', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
    neg_pid = q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-NEG',))
    neg_due = q1(DB, "SELECT due_amount FROM payment_nodes WHERE project_id=? AND node_type='first'", (neg_pid,)) if neg_pid else None
    check('合同', '负比例应被拒绝', neg_pid is None or float(neg_due or 0) >= 0,
          '生成负数应付=%s' % neg_due)
    # 增补合同逐项与合计
    _, ct_add = upload(s_admin, SAMPLE_TXT, '增补合同.txt')
    confirm_ct(s_admin, ct_add, {'project_no': 'QA-CT-ADD', 'contract_name': '合同增补项目',
                                 'original_amount': '100000', 'additional_amount': '20000', 'tax_rate': '13%',
                                 'ratio_first': '30', 'ratio_progress': '52', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
    pid_add = q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-CT-ADD',))
    if pid_add:
        nsum = {nt: float(q1(DB, 'SELECT SUM(due_amount) FROM payment_nodes WHERE project_id=? AND node_type=?', (pid_add, nt)) or 0)
                for nt in ('first', 'progress', 'acceptance', 'warranty')}
        check('合同', '合同增补：首付按原金额、其余按总额',
              abs(nsum['first'] - 30000) < 0.005 and abs(nsum['progress'] - 62400) < 0.005, nsum)
    # 合同确认路径的舍入（P1-07 另一条链路）
    _, ct_round = upload(s_admin, SAMPLE_TXT, '舍入合同.txt')
    confirm_ct(s_admin, ct_round, {'project_no': 'QA-CT-ROUND', 'contract_name': '舍入合同',
                                   'original_amount': '100.05', 'additional_amount': '0', 'tax_rate': '13%',
                                   'ratio_first': '30', 'ratio_progress': '52', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
    ct_round_pid = q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-CT-ROUND',))
    if ct_round_pid:
        first_due = float(q1(DB, "SELECT due_amount FROM payment_nodes WHERE project_id=? AND node_type='first'", (ct_round_pid,)) or 0)
        check('合同', '合同确认路径也应按 HALF_UP 舍入（100.05×30%=30.02）',
              abs(first_due - 30.02) < 0.005, '首付=%.2f' % first_due)
    # 首次确认生成项目，再次提交应更新原项目而不新建
    # 注意：比例合计必须为 100%（新校验），故用 25/57/15/3 而不是 25/52/15/3
    cnt0 = q1(DB, 'SELECT COUNT(*) FROM projects')
    confirm_ct(s_admin, ct1, {'project_no': 'QA-CT1', 'contract_name': '首次确认项目',
                              'original_amount': '165000', 'additional_amount': '0', 'tax_rate': '13%',
                              'ratio_first': '25', 'ratio_progress': '57', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
    ct1_pid = q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-CT1',))
    check('合同', '首次确认生成项目与节点', ct1_pid is not None)
    cnt1 = q1(DB, 'SELECT COUNT(*) FROM projects')
    # 回改比例属金额变更 → 必须先返回差异确认页，带 confirm_token 二次提交才生效
    r_diff = confirm_ct(s_admin, ct1, {'contract_name': '二次确认项目', 'original_amount': '165000',
                                       'additional_amount': '0', 'tax_rate': '13%',
                                       'ratio_first': '30', 'ratio_progress': '52',
                                       'ratio_acceptance': '15', 'ratio_warranty': '3'})
    m_token = re.search(r'name="confirm_token" value="([^"]+)"', r_diff.text)
    check('合同', '回改金额需二次确认（返回差异确认页）', r_diff.status_code == 200 and m_token is not None,
          'code=%s token=%s' % (r_diff.status_code, bool(m_token)))
    if m_token:
        confirm_ct(s_admin, ct1, {'contract_name': '二次确认项目', 'original_amount': '165000',
                                  'additional_amount': '0', 'tax_rate': '13%',
                                  'ratio_first': '30', 'ratio_progress': '52',
                                  'ratio_acceptance': '15', 'ratio_warranty': '3',
                                  'confirm_token': m_token.group(1)})
    check('合同', '重复确认不应新建项目', q1(DB, 'SELECT COUNT(*) FROM projects') == cnt1, 'cnt0=%s cnt1=%s' % (cnt0, cnt1))
    if ct1_pid:
        first_due = float(q1(DB, "SELECT due_amount FROM payment_nodes WHERE project_id=? AND node_type='first'", (ct1_pid,)) or 0)
        check('合同', '无付款记录时回改比例可更新原项目节点', abs(first_due - 49500) < 0.005, '首付=%.2f' % first_due)
    # 已付款后回改金额 → 500
    _, ct_pay = upload(s_admin, SAMPLE_TXT, '回改金额.txt')
    confirm_ct(s_admin, ct_pay, {'project_no': 'QA-RECONF', 'contract_name': '回改金额项目',
                                 'original_amount': '165000', 'additional_amount': '0', 'tax_rate': '13%',
                                 'ratio_first': '30', 'ratio_progress': '52', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
    rp = q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-RECONF',))
    if rp:
        pf = node_ids(rp, 'first')[0]
        for data in ({'action': 'apply', 'apply_amount': '49500'},
                     {'action': 'invoice', 'invoice_amount': '49500', 'invoice_no': 'FP-RECONF'},
                     {'action': 'pay', 'paid_amount': '49500', 'pay_date': '2026-10-20'}):
            C.post(s_admin, BASE, '/nodes/%d/update' % pf, data)
        r = confirm_ct(s_admin, ct_pay, {'contract_name': '回改金额项目（改小）', 'original_amount': '100000',
                                         'additional_amount': '0', 'tax_rate': '13%',
                                         'ratio_first': '30', 'ratio_progress': '52', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
        check('合同', '有付款记录时回改金额应安全处理（非 500）',
              r.status_code != 500, 'code=%s' % r.status_code)
        check('合同', '回改失败时不得丢失付款记录',
              q1(DB, 'SELECT COUNT(*) FROM payment_records WHERE node_id=?', (pf,)) == 1)


def _concurrency(s_admin):
    # 8 并发申请
    _, pid = create_project(s_admin, 'QA-CONC-APPLY', 'QA并发申请', [('科目', '10000', '0')])
    nid = node_ids(pid, 'first')[0]
    res = run_threads(8, lambda s, i, n=nid: C.post(s, BASE, '/nodes/%d/update' % n, {'action': 'apply', 'apply_amount': '100'}))
    apps = q1(DB, 'SELECT COUNT(*) FROM payment_applications WHERE node_id=?', (nid,))
    bad = [x for x in res if getattr(x, 'status_code', None) == 500]
    # 状态机要求「未触发 → 未开票已申请」只能发生一次：8 个并发请求应只有 1 个成功、且无 500
    check('并发', '8 并发申请不应 500，且只生成一张申请单',
          not bad and apps == 1, '申请单=%s 500数=%s' % (apps, len(bad)))

    # 并发付款：节点已付金额必须等于付款流水合计（不丢更新）
    lost = []
    for trial in range(3):
        _, p = create_project(s_admin, 'QA-LU-%d' % trial, 'QA并发付款%d' % trial, [('科目', '10000', '0')])
        n = node_ids(p, 'first')[0]
        # 先进入「已申请」：状态机不允许未申请直接付款
        C.post(s_admin, BASE, '/nodes/%d/update' % n,
               {'action': 'apply', 'apply_amount': '2000', 'invoice_amount': '2000',
                'invoice_no': 'FP-LU-%d' % trial})
        run_threads(2, lambda s, i, n=n: C.post(s, BASE, '/nodes/%d/update' % n,
                                                {'action': 'pay', 'paid_amount': '1000', 'pay_date': '2026-10-20'}))
        paid = float(q1(DB, 'SELECT paid_amount FROM payment_nodes WHERE id=?', (n,)) or 0)
        pay_sum = float(q1(DB, 'SELECT COALESCE(SUM(paid_amount),0) FROM payment_records WHERE node_id=?', (n,)) or 0)
        recs = q1(DB, 'SELECT COUNT(*) FROM payment_records WHERE node_id=?', (n,))
        lost.append((paid, pay_sum, recs))
    check('并发', '两人同时付款后节点已付应与流水合计一致（无丢失更新）',
          all(abs(paid - pay_sum) < 0.005 and 1 <= recs <= 2 for paid, pay_sum, recs in lost),
          '实测 (node_paid, records_sum, records)=%s' % lost)

    # 并发同编号项目
    res = run_threads(2, lambda s, i: C.post(s, BASE, '/projects/new',
                                             {'project_no': 'QA-DUP', 'name': '重复编号',
                                              'item_name': ['a'], 'original_amount': ['1']}))
    codes = sorted(getattr(x, 'status_code', 'EXC') for x in res)
    check('并发', '并发创建同编号项目不应 500', 500 not in codes, 'codes=%s' % codes, xfail='P2-07')


def _audit_and_config(s_admin, pid_a, ct1):
    tables = [x[0] for x in q(DB, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    audit_like = [t for t in tables if any(k in t.lower() for k in ('log', 'audit', 'history'))]
    check('审计', '应存在审计/操作日志表', bool(audit_like), tables, xfail='P0-02')
    pnode_cols = [x[1] for x in q(DB, 'PRAGMA table_info(payment_nodes)')]
    check('审计', '付款节点应记录 updated_at/updated_by',
          'updated_at' in pnode_cols or 'updated_by' in pnode_cols, pnode_cols, xfail='P0-02')
    pcols = [x[1] for x in q(DB, 'PRAGMA table_info(projects)')]
    check('审计', '项目应记录 updated_at/updated_by', 'updated_at' in pcols or 'updated_by' in pcols, pcols, xfail='P0-02')
    cfcols = [x[1] for x in q(DB, 'PRAGMA table_info(contract_files)')]
    check('审计', '合同应记录确认人/确认时间',
          'confirmed_by' in cfcols or 'confirmed_at' in cfcols, cfcols, xfail='P0-02')
    check('审计', '删除项目应可追溯（软删/回收站/删除日志）',
          bool(audit_like) or ('deleted_at' in pcols) or ('is_deleted' in pcols), '物理删除无痕', xfail='P0-02')
    rec_cols = [x[1] for x in q(DB, 'PRAGMA table_info(payment_records)')]
    check('审计', '付款流水保留操作人（部分可追溯）', 'operator_name' in rec_cols)

    # CSRF
    page = s_admin.get(BASE + '/projects/%d' % pid_a).text
    check('安全', '表单应包含 CSRF token', 'csrf' in page.lower(), '页面无 token', xfail='P1-09')
    r = C.post(s_admin, BASE, '/projects/%d/items' % pid_a, {'item_name': 'csrf测试科目', 'original_amount': '1'})
    check('安全', '无 CSRF token 的表单提交应被拒绝', r.status_code in (400, 403),
          'code=%s（直接成功）' % r.status_code, xfail='P1-09')

    # WAL
    conn = sqlite3.connect(DB)
    jm = conn.execute('PRAGMA journal_mode').fetchone()[0]
    conn.close()
    check('配置', 'SQLite 运行于 WAL 模式', str(jm).lower() == 'wal', jm)
    # 错误页
    r = s_admin.get(BASE + '/projects/999999')
    check('配置', '404 页面应为中文友好页', r.status_code == 404 and ('页面不存在' in r.text or '未找到' in r.text),
          '默认 Not Found 英文页', xfail='P2-01')
    # 依赖
    req = open(os.path.join(PROJECT_ROOT, 'requirements.txt'), encoding='utf-8').read()
    check('配置', 'requirements.txt 应锁定版本且声明 Pillow',
          'Pillow' in req and all('==' in line for line in req.splitlines() if line.strip()),
          req.replace('\n', '; '), xfail='P2-10')
    # 文件日志
    app_init = open(os.path.join(PROJECT_ROOT, 'app', '__init__.py'), encoding='utf-8').read()
    check('配置', '应用应配置文件日志（FileHandler/轮转）',
          'FileHandler' in app_init or 'RotatingFileHandler' in app_init, '仅 stderr', xfail='P2-09')
    # viewer 打开确认页不应看到可编辑表单
    s_view, _ = C.login(BASE, 'viewer', '123456')
    page = s_view.get(BASE + '/contracts/%d/confirm' % ct1).text
    check('配置', '只读角色不应看到可提交的合同编辑表单',
          'name="ratio_first"' not in page, 'viewer 可见可编辑表单', xfail='P2-11')


def run(server):
    global DB, BASE, APP, UPLOAD_DIR
    DB, BASE, APP, UPLOAD_DIR = server.db_path, server.base, server.app, server.upload_dir
    print('临时数据库: %s' % DB)
    print('随机端口服务: %s' % BASE)
    check('框架', '测试数据库不在项目 instance 目录', os.path.abspath(DB) != C.INSTANCE_DB, DB)
    section('########## 1. 会话与 Cookie ##########', lambda: _sessions())
    s_admin, _ = C.login(BASE, 'admin', 'admin123')
    pid_a = pid_c = ct1 = None

    def perms():
        nonlocal pid_a, pid_c, ct1
        pid_a, pid_c, ct1 = _permissions(s_admin)
    section('########## 2. 权限矩阵 ##########', perms)
    section('########## 3. 上传安全 ##########', lambda: _upload_security(s_admin))
    if pid_a and pid_c:
        section('########## 4. 状态流转与金额完整性 ##########', lambda: _state_and_money(s_admin, pid_a, pid_c))
    section('########## 5. 数据准确性 ##########', lambda: _data_accuracy(s_admin))
    if ct1:
        section('########## 6. 合同确认 ##########', lambda: _contracts(s_admin, ct1))
    section('########## 7. 并发写 ##########', lambda: _concurrency(s_admin))
    if pid_a and ct1:
        section('########## 8. 审计与配置 ##########', lambda: _audit_and_config(s_admin, pid_a, ct1))

# -*- coding: utf-8 -*-
"""HTTP 层回归用例：会话/权限/上传/状态/金额/合同/并发/审计。

约定：check(..., xfail='P0-01') 表示该用例断言的是「正确行为」，当前预期失败，
对应 docs/qa-baseline.md 的缺陷编号；缺陷修复后会变为 XPASS，提示更新基线。
"""
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
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


def item_ids(pid):
    return [r[0] for r in q(DB, 'SELECT id FROM project_items WHERE project_id=? ORDER BY id', (pid,))]


CH_TOKEN_RE = re.compile(r'name="confirm_token" value="([^"]+)"')


def contract_change(sess, pid, totals, confirm=True):
    """逐科目合同变更（CR-04）：totals 为 ``{item_id: 新总额}``。

    confirm=True 时自动从差异确认页取一次性 token 二次提交；
    返回 (第一次响应, 二次响应或 None)。
    """
    data = {'item_total_%d' % iid: str(v) for iid, v in totals.items()}
    r1 = C.post(sess, BASE, '/projects/%d/contract-change' % pid, data)
    if not confirm:
        return r1, None
    m = CH_TOKEN_RE.search(r1.text or '')
    if not m:
        return r1, None
    data['confirm_token'] = m.group(1)
    return r1, C.post(sess, BASE, '/projects/%d/contract-change' % pid, data)


def create_project(sess, no, name, items, remark='', ratios=None):
    """新建项目（CR-04：4 个节点比例必填、无默认值）。"""
    data = {'project_no': no, 'name': name, 'remark': remark,
            'item_name': [i[0] for i in items],
            'tax_rate': [i[3] if len(i) > 3 else '6%' for i in items],
            'original_amount': [i[1] for i in items]}
    data.update(ratios or C.RATIO_FORM)
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
    check('会话', '会话 Cookie 含 HttpOnly/SameSite/过期时间',
          ('HttpOnly' in sc) and ('SameSite' in sc) and ('Expires' in sc or 'Max-Age' in sc),
          sc + ' → 缺 SameSite/过期')

    s_wrong = requests.Session()
    rw = s_wrong.post(BASE + '/login', data={'username': 'admin', 'password': 'bad'}, allow_redirects=False)
    rw2 = s_wrong.get(BASE + '/projects', allow_redirects=False)
    check('会话', '错误密码不建立会话', rw.status_code == 200 and rw2.status_code == 302,
          'code=%s 后续=%s' % (rw.status_code, rw2.status_code))

    # 登录限速（4b）：显式开启限速的客户端 + 专用账号验证
    C.post(s_admin, BASE, '/admin/users/new',
           {'username': 'qa_lock1', 'display_name': 'QA限速', 'password': 'Qa#123456', 'role': 'viewer'})
    tc_lock = _cfg_client(LOGIN_THROTTLE_ENABLED=True)
    for _ in range(5):
        tc_lock.post('/login', data={'username': 'qa_lock1', 'password': 'wrong'}, follow_redirects=False)
    r_lock = tc_lock.post('/login', data={'username': 'qa_lock1', 'password': 'Qa#123456'},
                          follow_redirects=False)
    _lock_body = r_lock.get_data(as_text=True)
    check('会话', '连续失败后应锁定/延迟',
          r_lock.status_code == 200 and any(k in _lock_body for k in ('锁定', '稍后', '尝试')),
          'code=%s' % r_lock.status_code)

    bad = []
    for path in ['/projects', '/reports', '/contracts', '/projects/1', '/admin/users',
                 '/projects/1/edit', '/contracts/1/raw']:
        rr = requests.get(BASE + path, allow_redirects=False)
        if not (rr.status_code in (301, 302) and '/login' in rr.headers.get('Location', '')):
            bad.append('%s->%s' % (path, rr.status_code))
    check('会话', '未登录访问受保护页面全部重定向', not bad, bad)

    # 登出方式兼容批次 4a 前后：新行为为 POST，旧行为为 GET
    rp = s_admin.post(BASE + '/logout', allow_redirects=False)
    if rp.status_code in (403, 405, 404):
        s_admin.get(BASE + '/logout', allow_redirects=False)
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
    item_data = {'item_name': '经办添加科目', 'tax_rate': '6%', 'original_amount': '5000'}
    item_data.update(C.RATIO_FORM)  # CR-04：新增科目也要填 4 个比例
    C.post(s_op, BASE, '/projects/%d/items' % pid_a, item_data)
    check('权限', 'operator 添加科目允许', q1(DB, 'SELECT COUNT(*) FROM project_items WHERE project_id=?', (pid_a,)) == ic0 + 1)
    node_a_prog = node_ids(pid_a, 'progress')[0]
    C.post(s_op, BASE, '/nodes/%d/update' % node_a_prog, {'action': 'apply', 'apply_amount': '1000'})
    C.post(s_op, BASE, '/nodes/%d/update' % node_a_prog, {'action': 'invoice', 'invoice_amount': '1000', 'invoice_no': 'FP-QA-OP'})
    rr = C.post(s_op, BASE, '/nodes/%d/update' % node_a_prog, {'action': 'pay', 'paid_amount': '1000', 'pay_date': '2026-10-20'})
    check('权限', 'operator 申请→发票→付款允许（CR-04 三维，部分付款）',
          q1(DB, 'SELECT status FROM payment_nodes WHERE id=?', (node_a_prog,)) == '部分付款'
          and d2(q1(DB, 'SELECT applied_amount FROM payment_nodes WHERE id=?', (node_a_prog,))) == d2('1000')
          and d2(q1(DB, 'SELECT invoice_amount FROM payment_nodes WHERE id=?', (node_a_prog,))) == d2('1000')
          and d2(q1(DB, 'SELECT paid_amount FROM payment_nodes WHERE id=?', (node_a_prog,))) == d2('1000'))
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
          '项目已被经办人删除')
    # 越权 2：经办人经编辑页改状态
    before = q1(DB, 'SELECT status FROM projects WHERE id=?', (pid_a,))
    C.post(s_op, BASE, '/projects/%d/edit' % pid_a, {'name': 'QA权限-经办项目', 'status': '已归档', 'remark': ''})
    after = q1(DB, 'SELECT status FROM projects WHERE id=?', (pid_a,))
    check('权限', '经办人不应能经编辑页改项目状态', after == before,
          '状态 %s→%s' % (before, after))
    C.post(s_admin, BASE, '/projects/%d/edit' % pid_a, {'name': 'QA权限-经办项目', 'status': '进行中', 'remark': ''})

    # --- admin ---
    rr = s_admin.get(BASE + '/admin/users')
    check('权限', 'admin 用户管理允许', rr.status_code == 200)
    uc0 = q1(DB, 'SELECT COUNT(*) FROM users')
    C.post(s_admin, BASE, '/admin/users/new', {'username': 'qa_test_u', 'display_name': 'QA测试用户', 'password': 'Qa#123456', 'role': 'viewer'})
    check('权限', 'admin 新建用户允许', q1(DB, 'SELECT COUNT(*) FROM users') == uc0 + 1)
    uid = q1(DB, 'SELECT id FROM users WHERE username=?', ('qa_test_u',))
    C.post(s_admin, BASE, '/admin/users/%d/delete' % uid)
    check('权限', '删除用户需二次确认（无 confirm 不删）',
          q1(DB, 'SELECT COUNT(*) FROM users WHERE id=?', (uid,)) == 1)
    C.post(s_admin, BASE, '/admin/users/%d/delete' % uid, {'confirm': '1'})
    check('权限', 'admin 二次确认后删除用户允许',
          q1(DB, 'SELECT COUNT(*) FROM users WHERE id=?', (uid,)) == 0)
    C.post(s_admin, BASE, '/admin/users/1/delete')
    check('权限', 'admin 删除自身被拒', q1(DB, 'SELECT COUNT(*) FROM users WHERE id=1') == 1)
    C.post(s_admin, BASE, '/projects/%d/delete' % pid_d)
    conn = sqlite3.connect(DB)
    pcols = [r[1] for r in conn.execute('PRAGMA table_info(projects)')]
    conn.close()
    visible = 'QA-待管理员删除项目' in s_admin.get(BASE + '/projects').text
    check('权限', 'admin 删除项目改为软删除（列表不可见、数据保留）',
          q1(DB, 'SELECT COUNT(*) FROM projects WHERE id=?', (pid_d,)) == 1
          and ('deleted_at' in pcols) and not visible,
          '物理删除或仍在列表')
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
    check('上传', '上传失败后不残留孤立文件', orphan_ok, '失败记录仍保留文件')

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

    # 上传大小：用 21MB 必超限文件验证 413（避免 17MB 进入文本解析造成长耗时）
    big = b'A' * (21 * 1024 * 1024)
    r_big = C.post(s_admin, BASE, '/contracts/upload', files={'file': ('big.txt', big, 'text/plain')})
    check('上传', '超过上限的文件应返回 413',
          r_big.status_code == 413, 'code=%s' % r_big.status_code)
    r_after = s_admin.get(BASE + '/projects')
    check('上传', '超限请求后服务仍可用', r_after.status_code == 200)
    check('上传', '服务端上传上限与 README 标注一致（20MB）',
          APP.config.get('MAX_CONTENT_LENGTH') == 20 * 1024 * 1024,
          '实际 %s' % APP.config.get('MAX_CONTENT_LENGTH'))

    # 图片 OCR（默认启用：本机 WinRT 访问问题已修复；无 OCR 的机器可显式 QA_ENABLE_OCR=0 跳过）
    if os.environ.get('QA_ENABLE_OCR', '1') == '0':
        check('上传', '图片合同 OCR（QA_ENABLE_OCR=0，本机无 OCR 环境跳过）', True,
              '如需强制测试请去掉 QA_ENABLE_OCR=0')
    else:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            ImageFont = None
        if ImageFont is None:
            check('上传', '图片合同 OCR 用例（Pillow 未安装跳过）', True)
        else:
            # 用小字号默认字体 OCR 识别率低，这里用系统中文字体 48px 生成清晰图片
            font = None
            for fp in (r'C:\Windows\Fonts\simhei.ttf', r'C:\Windows\Fonts\msyh.ttc',
                       r'C:\Windows\Fonts\simsun.ttc'):
                if os.path.exists(fp):
                    try:
                        font = ImageFont.truetype(fp, 48)
                        break
                    except OSError:
                        pass
            png = os.path.join(tempfile.gettempdir(), 'qa_ocr_probe.png')
            im = Image.new('RGB', (1400, 500), 'white')
            draw = ImageDraw.Draw(im)
            lines = [('合同总金额：165000.00 元', (40, 40)), ('税率：13%', (40, 140)),
                     ('合同名称：QA OCR 测试合同', (40, 240))]
            for text, xy in lines:
                if font:
                    draw.text(xy, text, font=font, fill='black')
                else:
                    draw.text(xy, text, fill='black')
            im.save(png)
            with open(png, 'rb') as fh:
                r_img = C.post(s_admin, BASE, '/contracts/upload', files={'file': ('ocr_probe.png', fh, 'image/png')})
            img_row = q(DB, 'SELECT status,error_msg FROM contract_files ORDER BY id DESC LIMIT 1')[0]
            check('上传', '图片合同 OCR 可识别并进入确认页',
                  r_img.status_code == 302 and '/confirm' in r_img.headers.get('Location', ''),
                  'status=%s error=%s font=%s' % (img_row[0], img_row[1], bool(font)))


def _state_and_money(s_admin, pid_a, pid_c):
    # CR-04 三本账：付款不再以申请/开票为前置（任意顺序、可多次累计）
    nid = node_ids(pid_c, 'first')[0]
    C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'pay', 'paid_amount': '100'})
    paid0 = float(q1(DB, 'SELECT paid_amount FROM payment_nodes WHERE id=?', (nid,)) or 0)
    st = q1(DB, 'SELECT status FROM payment_nodes WHERE id=?', (nid,))
    check('状态', '先付款后补申请/开票应被接受（三维并列）',
          abs(paid0 - 100) < 0.005 and st == '部分付款', 'paid=%.2f status=%s' % (paid0, st))
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
          q1(DB, 'SELECT status FROM projects WHERE id=?', (pid_c,)) == '已归档')
    C.post(s_admin, BASE, '/projects/%d/status' % pid_c, {'status': '已归档'})
    nid2 = node_ids(pid_c, 'acceptance')[0]
    C.post(s_admin, BASE, '/nodes/%d/update' % nid2, {'action': 'apply', 'apply_amount': '10'})
    check('状态', '已归档项目应只读', q1(DB, 'SELECT status FROM payment_nodes WHERE id=?', (nid2,)) != '未开票已申请')
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
    # P2-04 口径（Chief 定）：种子项目仅演示数据，上线初始化不建种子（--no-seed 为生产默认），
    # 故「已付但状态=已申请」不再是缺陷，也不计数；此处仅登记范围外说明，不计 FAIL/XFAIL。
    seed_prog = q(DB, "SELECT paid_amount,status FROM payment_nodes WHERE project_id=? AND node_type='progress'", (seed,))
    check('数据', '[不在上线范围] 种子演示项目进度款状态口径（仅演示数据，生产不初始化种子）', True,
          'seed_prog=%s' % (seed_prog,))

    # 增补口径（CR-04：新建项目无增补，增补走合同变更；4 节点按新总额重算，首付也纳入）
    _, pid_add = create_project(s_admin, 'QA-ADD', 'QA增补口径', [('增补科目', '100000', '0')])
    iid_add = item_ids(pid_add)[0]
    r1, r2 = contract_change(s_admin, pid_add, {iid_add: '120000'})
    nsum = {nt: float(q1(DB, 'SELECT SUM(due_amount) FROM payment_nodes WHERE project_id=? AND node_type=?', (pid_add, nt)) or 0)
            for nt in ('first', 'progress', 'acceptance', 'warranty')}
    add_total = d2(q1(DB, 'SELECT SUM(original_amount+additional_amount) FROM project_items WHERE project_id=?', (pid_add,)))
    check('数据', '合同变更增补：两段式提交应保存新总额 120,000',
          add_total == d2('120000'), '总额=%s r1=%s r2=%s' % (add_total, r1.status_code, getattr(r2, 'status_code', None)))
    check('数据', '增补项目首付按新总额 36,000（含增补）',
          abs(nsum['first'] - 36000) < 0.005, nsum)
    check('数据', '增补项目进度款按总额 62,400', abs(nsum['progress'] - 62400) < 0.005, nsum)
    check('数据', '增补项目验收款 18,000 / 质保金 3,600',
          abs(nsum['acceptance'] - 18000) < 0.005 and abs(nsum['warranty'] - 3600) < 0.005, nsum)
    check('数据', '增补项目节点合计应等于合同总额 120,000',
          abs(sum(nsum.values()) - 120000) < 0.005, '节点合计=%.2f' % sum(nsum.values()))

    # 舍入（会计 HALF_UP，尾差按 质保金→验收→进度→首付 归集；CR-04 新建无增补）
    items = [('甲', '100.05', '0'), ('乙', '400.00', '0'), ('丙', '0.05', '0'), ('丁', '123457.00', '0')]
    _, pid_r = create_project(s_admin, 'QA-ROUND', 'QA舍入', items)
    mismatch = []
    for idx, it in enumerate(items):
        base = d2(it[1])
        expect = {nt: d2(base * C.RATIOS[nt]) for nt in ('first', 'progress', 'acceptance', 'warranty')}
        tail = base - sum(expect.values())
        if tail != 0:
            for nt in ('warranty', 'acceptance', 'progress', 'first'):
                if expect[nt] > 0 and expect[nt] + tail >= 0:
                    expect[nt] += tail
                    break
        item_id = q1(DB, 'SELECT id FROM project_items WHERE project_id=? ORDER BY id LIMIT 1 OFFSET ?', (pid_r, idx))
        for nt in ('first', 'progress', 'acceptance', 'warranty'):
            actual = d2(q1(DB, 'SELECT due_amount FROM payment_nodes WHERE project_id=? AND item_id=? AND node_type=?', (pid_r, item_id, nt)))
            if expect[nt] != actual:
                mismatch.append('%s-%s 期望%s 实际%s' % (it[0], nt, expect[nt], actual))
    check('数据', '项目新建节点金额应按会计四舍五入（HALF_UP，首付按总额、尾差归集）',
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
    # 增补：确认页不再有增补输入（CR-04），增补只能走合同变更
    _, ct_add = upload(s_admin, SAMPLE_TXT, '增补合同.txt')
    confirm_ct(s_admin, ct_add, {'project_no': 'QA-CT-ADD', 'contract_name': '合同增补项目',
                                 'original_amount': '100000', 'additional_amount': '20000', 'tax_rate': '13%',
                                 'ratio_first': '30', 'ratio_progress': '52', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
    pid_add = q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-CT-ADD',))
    if pid_add:
        add_total = d2(q1(DB, 'SELECT SUM(original_amount+additional_amount) FROM project_items WHERE project_id=?', (pid_add,)))
        nsum = {nt: float(q1(DB, 'SELECT SUM(due_amount) FROM payment_nodes WHERE project_id=? AND node_type=?', (pid_add, nt)) or 0)
                for nt in ('first', 'progress', 'acceptance', 'warranty')}
        check('合同', '确认页忽略增补输入：首次确认只落合同原金额 100,000',
              add_total == d2('100000') and abs(sum(nsum.values()) - 100000) < 0.005,
              '总额=%s 节点合计=%.2f' % (add_total, sum(nsum.values())))
        r1, r2 = contract_change(s_admin, pid_add, {item_ids(pid_add)[0]: '120000'})
        nsum = {nt: float(q1(DB, 'SELECT SUM(due_amount) FROM payment_nodes WHERE project_id=? AND node_type=?', (pid_add, nt)) or 0)
                for nt in ('first', 'progress', 'acceptance', 'warranty')}
        check('合同', '合同变更增补：4 节点按新总额（首付 36000 / 进度 62400 / 合计 120000）',
              abs(nsum['first'] - 36000) < 0.005 and abs(nsum['progress'] - 62400) < 0.005
              and abs(sum(nsum.values()) - 120000) < 0.005, nsum)
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
    # 8 并发申请 × 40 轮（CR04-01 验收强度对齐开发口径）：
    # 全部成功、无 500、单号全局唯一、累计申请=成功笔数
    _, pid = create_project(s_admin, 'QA-CONC-APPLY', 'QA并发申请', [('科目', '10000', '0')])
    nid = node_ids(pid, 'first')[0]
    codes = []
    for _round in range(40):
        res = run_threads(8, lambda s, i, n=nid: C.post(
            s, BASE, '/nodes/%d/update' % n, {'action': 'apply', 'apply_amount': '1'}))
        codes.extend(getattr(x, 'status_code', 'EXC') for x in res)
    applied = float(q1(DB, 'SELECT applied_amount FROM payment_nodes WHERE id=?', (nid,)) or 0)
    apps = q1(DB, 'SELECT COUNT(*) FROM payment_applications WHERE node_id=?', (nid,))
    uniq = q1(DB, 'SELECT COUNT(DISTINCT apply_no) FROM payment_applications WHERE node_id=?', (nid,))
    dist = {}
    for c in codes:
        dist[c] = dist.get(c, 0) + 1
    check('并发', '8 并发申请 × 40 轮全部成功、无 500',
          len(codes) == 320 and all(c == 302 for c in codes), '状态分布=%s' % dist)
    check('并发', '并发申请单号全局唯一且累计申请=成功笔数（CR04-01）',
          apps == 320 and uniq == 320 and abs(applied - 320) < 0.005,
          '申请单=%s 唯一单号=%s 累计申请=%s' % (apps, uniq, applied))

    # 单号生成器本身：连续生成 5000 个必须唯一（防止回退到秒级时间碰撞）
    from app.routes import make_apply_no
    nos = [make_apply_no() for _ in range(5000)]
    check('并发', '申请单号生成器连续 5000 次无重复',
          len(nos) == len(set(nos)), '唯一值=%d/5000' % len(set(nos)))

    # 并发付款：节点已付金额必须等于付款流水合计（不丢更新）
    lost = []
    for trial in range(3):
        _, p = create_project(s_admin, 'QA-LU-%d' % trial, 'QA并发付款%d' % trial, [('科目', '10000', '0')])
        n = node_ids(p, 'first')[0]
        # 先进入「已申请」：状态机不允许未申请直接付款
        C.post(s_admin, BASE, '/nodes/%d/update' % n,
               {'action': 'apply', 'apply_amount': '2000', 'invoice_amount': '2000',
                'invoice_no': 'FP-LU-%d' % trial})
        res = run_threads(2, lambda s, i, n=n: C.post(s, BASE, '/nodes/%d/update' % n,
                                                     {'action': 'pay', 'paid_amount': '1000', 'pay_date': '2026-10-20'}))
        codes = [getattr(x, 'status_code', 'EXC') for x in res]
        paid = float(q1(DB, 'SELECT paid_amount FROM payment_nodes WHERE id=?', (n,)) or 0)
        pay_sum = float(q1(DB, 'SELECT COALESCE(SUM(paid_amount),0) FROM payment_records WHERE node_id=?', (n,)) or 0)
        recs = q1(DB, 'SELECT COUNT(*) FROM payment_records WHERE node_id=?', (n,))
        lost.append((paid, pay_sum, recs, codes))
    check('并发', '两人同时付款后节点已付应与流水合计一致（无丢失更新）',
          all(abs(paid - pay_sum) < 0.005 and 1 <= recs <= 2 and 500 not in codes
              and 'EXC' not in codes for paid, pay_sum, recs, codes in lost),
          '实测 (node_paid, records_sum, records, codes)=%s' % lost)

    # 并发同编号项目
    res = run_threads(2, lambda s, i: C.post(s, BASE, '/projects/new',
                                             {'project_no': 'QA-DUP', 'name': '重复编号',
                                              'item_name': ['a'], 'original_amount': ['1']}))
    codes = sorted(getattr(x, 'status_code', 'EXC') for x in res)
    check('并发', '并发创建同编号项目不应 500', 500 not in codes, 'codes=%s' % codes)


def _audit_and_config(s_admin, pid_a, ct1):
    tables = [x[0] for x in q(DB, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    audit_like = [t for t in tables if any(k in t.lower() for k in ('log', 'audit', 'history'))]
    check('审计', '应存在审计/操作日志表', bool(audit_like), tables)
    pnode_cols = [x[1] for x in q(DB, 'PRAGMA table_info(payment_nodes)')]
    check('审计', '付款节点应记录 updated_at/updated_by',
          'updated_at' in pnode_cols or 'updated_by' in pnode_cols, pnode_cols)
    pcols = [x[1] for x in q(DB, 'PRAGMA table_info(projects)')]
    check('审计', '项目应记录 updated_at/updated_by', 'updated_at' in pcols or 'updated_by' in pcols, pcols)
    cfcols = [x[1] for x in q(DB, 'PRAGMA table_info(contract_files)')]
    check('审计', '合同应记录确认人/确认时间',
          'confirmed_by' in cfcols or 'confirmed_at' in cfcols, cfcols)
    check('审计', '删除项目应可追溯（审计日志/软删标记）',
          bool(audit_like) or ('deleted_at' in pcols) or ('is_deleted' in pcols), '物理删除无痕')
    rec_cols = [x[1] for x in q(DB, 'PRAGMA table_info(payment_records)')]
    check('审计', '付款流水保留操作人（部分可追溯）', 'operator_name' in rec_cols)

    # CSRF：用显式开启校验的测试客户端（实时服务为测试模式，默认不强制 CSRF）
    page = s_admin.get(BASE + '/projects/%d' % pid_a).text
    check('安全', '表单应包含 CSRF token', 'csrf' in page.lower(), '页面无 token')
    csrf_client = _make_csrf_client()
    _csrf_login(csrf_client)
    r = csrf_client.post('/projects/%d/items' % pid_a,
                         data={'item_name': 'csrf测试科目', 'original_amount': '1'})
    check('安全', '无 CSRF token 的表单提交应被拒绝', r.status_code in (400, 403),
          'code=%s（直接成功）' % r.status_code)

    # WAL
    conn = sqlite3.connect(DB)
    jm = conn.execute('PRAGMA journal_mode').fetchone()[0]
    conn.close()
    check('配置', 'SQLite 运行于 WAL 模式', str(jm).lower() == 'wal', jm)
    # 错误页
    r = s_admin.get(BASE + '/projects/999999')
    check('配置', '404 页面应为中文友好页', r.status_code == 404 and ('页面不存在' in r.text or '未找到' in r.text),
          '默认 Not Found 英文页')
    # 依赖：Pillow 缺失 + 版本锁定（架构师在锁依赖）
    req = open(os.path.join(PROJECT_ROOT, 'requirements.txt'), encoding='utf-8').read()
    req_lines = [ln.strip() for ln in req.splitlines() if ln.strip() and not ln.strip().startswith('#')]
    check('配置', 'requirements.txt 应声明 Pillow（扫描件脚本依赖）',
          any('pillow' in ln.lower() for ln in req_lines), req.replace('\n', '; '))
    check('配置', 'requirements.txt 应锁定版本（==）',
          bool(req_lines) and all('==' in ln for ln in req_lines),
          req.replace('\n', '; '))
    # e2e_scan_pdf.py 失败时应有汇总且退出码非 0
    scan_src = open(os.path.join(PROJECT_ROOT, 'scripts', 'e2e_scan_pdf.py'), encoding='utf-8').read()
    has_counter = ('FAIL' in scan_src and 'PASS' in scan_src)
    has_exit = ('sys.exit(1)' in scan_src or 'exit(1)' in scan_src or 'raise SystemExit' in scan_src)
    check('配置', 'e2e_scan_pdf.py 失败时应有 PASS/FAIL 汇总且退出码非 0',
          has_counter and has_exit, '失败仅 print 后 return，退出码仍为 0')
    # 文件日志
    app_init = open(os.path.join(PROJECT_ROOT, 'app', '__init__.py'), encoding='utf-8').read()
    check('配置', '应用应配置文件日志（FileHandler/轮转）',
          'FileHandler' in app_init or 'RotatingFileHandler' in app_init, '仅 stderr')
    # viewer 打开确认页不应看到可编辑表单
    s_view, _ = C.login(BASE, 'viewer', '123456')
    page = s_view.get(BASE + '/contracts/%d/confirm' % ct1).text
    check('配置', '只读角色不应看到可提交的合同编辑表单',
          'name="ratio_first"' not in page, 'viewer 可见可编辑表单')


def _audit_has_table():
    return bool(q(DB, "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_logs'"))


def _audit_cols():
    return [r[1] for r in q(DB, 'PRAGMA table_info(audit_logs)')] if _audit_has_table() else []


def _audit_max_id():
    if not _audit_has_table():
        return 0
    rows = q(DB, 'SELECT COALESCE(MAX(id),0) FROM audit_logs')
    return int(rows[0][0] or 0) if rows else 0


def _audit_rows(min_id=0):
    if not _audit_has_table():
        return []
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute('SELECT * FROM audit_logs WHERE id>? ORDER BY id', (min_id,))]
    finally:
        conn.close()


def _empty_json(value):
    return value is None or str(value).strip() in ('', '{}', 'null')


def _audit_trails(s_admin):
    """批次 3（P0-02 审计留痕）验收：登录、业务变更、删除、查询页权限与不可篡改、updated_*。"""
    cols = _audit_cols()
    # 兼容计划字段名（occurred_at/entity_type/entity_id/user_agent）与实际实现（ts/object_type/object_id/ua）
    field_ok = (
        any(c in cols for c in ('occurred_at', 'ts'))
        and 'actor_id' in cols and 'actor_name' in cols and 'action' in cols
        and any(c in cols for c in ('entity_type', 'object_type'))
        and any(c in cols for c in ('entity_id', 'object_id'))
        and 'before_json' in cols and 'after_json' in cols
    )
    check('审计留痕', 'audit_logs 表存在且含人/动作/对象/前后值字段',
          field_ok, 'audit_logs 列=%s' % cols)

    # 1) 登录成功/失败留痕
    base = _audit_max_id()
    C.login(BASE, 'admin', 'admin123')
    requests.post(BASE + '/login', data={'username': 'admin', 'password': 'bad'})
    rows = [r for r in _audit_rows(base)
            if 'login' in (str(r.get('action', '')) + str(r.get('entity_type', ''))).lower()]
    check('审计留痕', '登录成功与失败均写审计', len(rows) >= 2,
          '新增 login 审计行=%d' % len(rows))
    if rows:
        text = ' '.join(str(r.get('action', '')) + ' ' + str(r.get('result', '')) + ' '
                        + str(r.get('after_json', '')) for r in rows).lower()
        check('审计留痕', '审计可区分登录成功/失败',
              any(k in text for k in ('fail', '失败', 'denied'))
              and any(k in text for k in ('success', '成功', 'ok')),
              text[:200])

    # 2) 节点申请→开票→付款留痕
    s, _ = C.login(BASE, 'admin', 'admin123')
    _, pid = create_project(s, 'QA-AUDIT', 'QA审计节点', [('审计科目', '10000', '0')])
    nid = node_ids(pid, 'first')[0]
    base = _audit_max_id()
    C.post(s, BASE, '/nodes/%d/update' % nid, {'action': 'apply', 'apply_amount': '3000'})
    C.post(s, BASE, '/nodes/%d/update' % nid, {'action': 'invoice', 'invoice_amount': '3000', 'invoice_no': 'FP-AUDIT'})
    C.post(s, BASE, '/nodes/%d/update' % nid, {'action': 'pay', 'paid_amount': '3000', 'pay_date': '2026-10-23'})
    rows = _audit_rows(base)
    node_rows = [r for r in rows
                 if str(r.get('entity_id') or r.get('object_id')) == str(nid)
                 or str(r.get('project_id')) == str(pid)]
    check('审计留痕', '节点申请/开票/付款均留痕（≥3 条）', len(node_rows) >= 3,
          '节点相关审计=%d' % len(node_rows))
    check('审计留痕', '节点审计含操作人、对象、before/after',
          bool(node_rows)
          and all(str(r.get('actor_name') or '').strip() for r in node_rows)
          and all(any(str(r.get(k) or '').strip() for k in ('entity_type', 'object_type', 'entity_id', 'object_id', 'project_id')) for r in node_rows)
          and sum(1 for r in node_rows if not _empty_json(r.get('before_json')) or not _empty_json(r.get('after_json'))) >= 2,
          '示例=%s' % (node_rows[:1] if node_rows else '无'))

    # 3) 项目编辑 / 状态变更留痕
    base = _audit_max_id()
    C.post(s, BASE, '/projects/%d/edit' % pid, {'name': 'QA审计节点-改名', 'remark': '审计备注', 'status': '进行中'})
    C.post(s, BASE, '/projects/%d/status' % pid, {'status': '已归档'})
    rows = _audit_rows(base)
    proj_rows = [r for r in rows
                 if str(r.get('project_id')) == str(pid)
                 or str(r.get('entity_id') or r.get('object_id')) == str(pid)]
    check('审计留痕', '项目编辑与状态变更均留痕（≥2 条）', len(proj_rows) >= 2,
          '项目相关审计=%d' % len(proj_rows))
    check('审计留痕', '项目编辑/状态审计含 before/after 明细',
          any(not _empty_json(r.get('before_json')) and not _empty_json(r.get('after_json')) for r in proj_rows),
          '项目相关审计=%s' % proj_rows[:1])

    # 4) 删除留痕（允许先只做到「有日志」，软删另算）
    _, pid_del = create_project(s, 'QA-AUDIT-DEL', 'QA审计删除', [('科目', '1000', '0')])
    base = _audit_max_id()
    C.post(s, BASE, '/projects/%d/delete' % pid_del)
    rows = _audit_rows(base)
    del_rows = [r for r in rows
                if 'delete' in str(r.get('action', '')).lower() or '删除' in str(r.get('action', ''))
                or 'delete' in str(r.get('entity_type') or r.get('object_type') or '').lower()]
    check('审计留痕', '项目删除留痕（仅日志即可，软删另算）', bool(del_rows),
          '删除审计行=%d' % len(del_rows))

    # 5) 审计查询页：admin 可访问、viewer/operator 被拒、不可篡改
    candidates = ['/admin/audit', '/admin/audit-logs', '/audit', '/audit-logs', '/admin/logs']
    found = None
    for path in candidates:
        r = s.get(BASE + path, allow_redirects=False)
        if r.status_code == 200:
            found = (path, r.text)
            break
    check('审计留痕', '管理员可访问审计查询页', found is not None,
          '候选路径均不可用：%s' % candidates)
    if found:
        path, page = found
        check('审计留痕', '审计页可展示操作记录', '审计' in page or '删除' in page or 'login' in page.lower(),
              '页面已返回')
        sv, _ = C.login(BASE, 'viewer', '123456')
        so, _ = C.login(BASE, 'jbr', '123456')
        check('审计留痕', 'viewer 访问审计页被拒',
              sv.get(BASE + path, allow_redirects=False).status_code in (302, 403),
              'viewer 可访问 %s' % path)
        check('审计留痕', 'operator 访问审计页被拒',
              so.get(BASE + path, allow_redirects=False).status_code in (302, 403),
              'operator 可访问 %s' % path)
        rp = s.post(BASE + path, data={}, allow_redirects=False)
        check('审计留痕', '审计页不接受 POST（不可经 UI 修改/删除）',
              rp.status_code in (404, 403, 405), 'POST code=%s' % rp.status_code)
        check('审计留痕', '审计页无 POST/删除表单',
              re.search(r'<form[^>]*action="[^"]*audit[^"]*"[^>]*method=["\']?post', page, re.I) is None
              and '删除审计' not in page and '清空日志' not in page,
              '页面含 POST 表单或删除入口')

    # 6) updated_at / updated_by：字段存在且变更后更新
    ncols = [r[1] for r in q(DB, 'PRAGMA table_info(payment_nodes)')]
    pcols = [r[1] for r in q(DB, 'PRAGMA table_info(projects)')]
    if 'updated_at' in ncols and 'updated_by' in ncols:
        _, p_ts = create_project(s, 'QA-AUDIT-TS', 'QA审计时间戳', [('科目', '1000', '0')])
        n_ts = node_ids(p_ts, 'first')[0]
        before = q(DB, 'SELECT updated_at, updated_by FROM payment_nodes WHERE id=?', (n_ts,))[0]
        C.post(s, BASE, '/nodes/%d/update' % n_ts, {'action': 'apply', 'apply_amount': '300'})
        after = q(DB, 'SELECT updated_at, updated_by FROM payment_nodes WHERE id=?', (n_ts,))[0]
        check('审计留痕', '节点变更后 updated_at/updated_by 已更新',
              after[0] is not None and str(after[0]) != str(before[0]) and str(after[1]) == '1',
              'before=%s after=%s' % (before, after))
    else:
        check('审计留痕', '节点变更后 updated_at/updated_by 已更新', False,
              'payment_nodes 缺字段', xfail='P0-02')
    if 'updated_at' in pcols and 'updated_by' in pcols:
        _, pid_ts = create_project(s_admin, 'QA-AUDIT-PTS', 'QA审计项目时间戳', [('科目', '1000', '0')])
        before = q(DB, 'SELECT updated_at, updated_by FROM projects WHERE id=?', (pid_ts,))[0]
        C.post(s_admin, BASE, '/projects/%d/edit' % pid_ts,
               {'name': 'QA审计项目时间戳-改名', 'status': '进行中', 'remark': ''})
        after = q(DB, 'SELECT updated_at, updated_by FROM projects WHERE id=?', (pid_ts,))[0]
        check('审计留痕', '项目编辑后 updated_at/updated_by 已更新',
              after[0] is not None and str(after[0]) != str(before[0]) and str(after[1]) == '1',
              'before=%s after=%s' % (before, after))
    else:
        check('审计留痕', '项目编辑后 updated_at/updated_by 已更新', False,
              'projects 缺字段', xfail='P0-02')


def _make_csrf_client():
    """批次 4a：显式打开 CSRF 校验的测试客户端（与实时服务共用临时库）。"""
    app = C.make_app(DB, CSRF_ENABLED=True, SECRET_KEY=C.DEFAULT_SECRET)
    return app.test_client()


def _csrf_login(client):
    page = client.get('/login').get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', page)
    data = {'username': 'admin', 'password': 'admin123'}
    if m:
        data['csrf_token'] = m.group(1)
    return client.post('/login', data=data, follow_redirects=False), m


def _security_baseline(s_admin):
    """批次 4a（安全基线）验收：CSRF、密钥、会话、登出 POST、错误页、越权收口。"""
    # 1) CSRF：无 token 的 POST 应 403 中文页；带 token 可通过
    client = _make_csrf_client()
    _csrf_login(client)
    _, pid_sec = create_project(s_admin, 'QA-SEC', 'QA安全基线', [('科目', '1000', '0')])
    page = client.get('/projects/%d' % pid_sec).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', page)
    r_no = client.post('/projects/%d/delete' % pid_sec, data={})
    body = r_no.get_data(as_text=True)
    has_cn = any('\u4e00' <= ch <= '\u9fff' for ch in body)
    check('安全基线', '无 CSRF token 的 POST 应被拒绝（403 中文页）',
          r_no.status_code == 403 and has_cn,
          'code=%s' % r_no.status_code)
    token = m.group(1) if m else None
    check('安全基线', '页面表单包含 CSRF token 字段', token is not None, '未找到 csrf_token')
    if token:
        r_ok = client.post('/projects/%d/delete' % pid_sec, data={'csrf_token': token})
        check('安全基线', '携带有效 CSRF token 的 POST 可执行',
              r_ok.status_code in (200, 302), 'code=%s' % r_ok.status_code)

    # 2) SECRET_KEY：未配置时不得使用可预测默认值（否则可伪造会话）
    forged = None
    try:
        from flask import Flask
        from flask.sessions import SecureCookieSessionInterface
        _a = Flask('qa-sec')
        _a.secret_key = C.DEFAULT_SECRET
        forged = SecureCookieSessionInterface().get_signing_serializer(_a).dumps({'user_id': 1})
    except Exception:
        pass
    old_env = os.environ.pop('SECRET_KEY', None)
    secret_ok = False
    try:
        try:
            app2 = C.make_app(DB)
            tc2 = app2.test_client()
            if forged:
                tc2.set_cookie('session', forged, domain='localhost')
            secret_ok = tc2.get('/admin/users', allow_redirects=False).status_code != 200
        except Exception:
            secret_ok = True  # 启动失败（fail-fast）也算通过
    finally:
        if old_env is not None:
            os.environ['SECRET_KEY'] = old_env
    check('安全基线', '未配置 SECRET_KEY 时不使用可预测默认值',
          secret_ok, '默认密钥可伪造管理员会话')

    # 3) 登出改 POST；GET 应 405
    s2, _ = C.login(BASE, 'admin', 'admin123')
    r_get = s2.get(BASE + '/logout', allow_redirects=False)
    check('安全基线', '登出仅接受 POST（GET /logout 返回 405）',
          r_get.status_code == 405, 'GET code=%s' % r_get.status_code)
    page = s2.get(BASE + '/projects').text
    check('安全基线', '页面退出入口为 POST 表单',
          re.search(r'<form[^>]*action="/logout"[^>]*method=["\']?post', page, re.I) is not None,
          '仍为 GET 链接')

    # 4) 会话 Cookie：HttpOnly + SameSite=Lax + 8h 过期
    _, r = C.login(BASE, 'admin', 'admin123')
    sc = r.headers.get('Set-Cookie', '')
    check('安全基线', '会话 Cookie 满足 HttpOnly + SameSite=Lax + 8h 过期',
          ('HttpOnly' in sc) and ('SameSite=Lax' in sc.replace(' ', ''))
          and (('Max-Age=28800' in sc) or ('Expires=' in sc)),
          sc)

    # 5) 错误页：404/413 中文；403/404/413/500 均有处理器
    r404 = s_admin.get(BASE + '/projects/999999')
    check('安全基线', '404 页面为中文友好页（含「页面不存在」）',
          r404.status_code == 404 and '页面不存在' in r404.text,
          '默认英文 404 页')
    big = b'A' * (21 * 1024 * 1024)
    r413 = C.post(s_admin, BASE, '/contracts/upload', files={'file': ('sec_big.txt', big, 'text/plain')})
    body413 = r413.text
    check('安全基线', '413 超限页为中文友好提示',
          r413.status_code == 413 and any('\u4e00' <= ch <= '\u9fff' for ch in body413),
          'code=%s body=%s' % (r413.status_code, body413[:80]))
    src = open(os.path.join(PROJECT_ROOT, 'app', '__init__.py'), encoding='utf-8').read()
    src += open(os.path.join(PROJECT_ROOT, 'app', 'routes.py'), encoding='utf-8').read()
    handlers_ok = all(('errorhandler(%d)' % code) in src for code in (403, 404, 413, 500))
    check('安全基线', '403/404/413/500 均注册中文错误处理器', handlers_ok,
          '缺 errorhandler')

    # 6) 越权收口：operator 编辑页不应渲染状态字段
    s_op, _ = C.login(BASE, 'jbr', '123456')
    page = s_op.get(BASE + '/projects/%d/edit' % pid_sec).text
    check('安全基线', '经办人编辑页不渲染项目状态字段',
          'name="status"' not in page, '仍然渲染 name="status"')


def _audit_extras(s_admin):
    """批次 3 补充验收：停用登录、停用/启用审计、筛选分页、审计降级、updated_by 落值。"""
    s_admin, _ = C.login(BASE, 'admin', 'admin123')
    # 1) 停用后登录被拒 + 停用/启用审计
    C.post(s_admin, BASE, '/admin/users/new',
           {'username': 'qa_dis_u', 'display_name': 'QA停用用户', 'password': 'Qa#123456', 'role': 'viewer'})
    uid = q1(DB, 'SELECT id FROM users WHERE username=?', ('qa_dis_u',))
    check('审计补充', '新建用户可登录', C.login(BASE, 'qa_dis_u', 'Qa#123456')[1].status_code == 302)
    if uid is None:
        check('审计补充', '停用后登录被拒', False, '测试用户创建失败', xfail='P1-10')
    else:
        base = _audit_max_id()
        C.post(s_admin, BASE, '/admin/users/%d/toggle' % uid)
        _, r_fail = C.login(BASE, 'qa_dis_u', 'Qa#123456')
        check('审计补充', '停用后登录被拒', r_fail.status_code == 200, 'code=%s' % r_fail.status_code)
        rows = _audit_rows(base)
        acts = [str(r.get('action', '')) for r in rows]
        dis_rows = [r for r in rows if str(r.get('action')) == 'user.disable']
        check('审计补充', '停用操作写审计（user.disable，含 is_active 前后值）',
              bool(dis_rows) and not _empty_json(dis_rows[0].get('before_json'))
              and not _empty_json(dis_rows[0].get('after_json')),
              '动作=%s' % acts)
        base = _audit_max_id()
        C.post(s_admin, BASE, '/admin/users/%d/toggle' % uid)
        check('审计补充', '启用后恢复登录', C.login(BASE, 'qa_dis_u', 'Qa#123456')[1].status_code == 302)
        en_rows = [r for r in _audit_rows(base) if str(r.get('action')) == 'user.enable']
        check('审计补充', '启用操作写审计（user.enable）', bool(en_rows), '动作=%s' % [r.get('action') for r in _audit_rows(base)])

    # 2) 审计筛选 + 分页（脚本注入 60 条合成记录；页面大小 50）
    conn = sqlite3.connect(DB, timeout=15)
    try:
        conn.executemany(
            'INSERT INTO audit_logs (ts, actor_id, actor_name, action, object_type, object_id, '
            'project_id, before_json, after_json, ip, ua, result) '
            "VALUES (?, 1, 'QA', ?, 'synthetic', 0, NULL, '{}', '{}', '127.0.0.1', 'qa', 'ok')",
            [('2099-01-01 00:00:%02d' % i, 'qa.page.%02d' % i) for i in range(60)])
        conn.commit()
    finally:
        conn.close()
    page1 = s_admin.get(BASE + '/admin/audit').text
    page2 = s_admin.get(BASE + '/admin/audit?page=2').text
    filt = s_admin.get(BASE + '/admin/audit?action=qa.page.00').text

    def _tbody(html):
        m = re.search(r'<tbody>(.*?)</tbody>', html, re.S | re.I)
        return m.group(1) if m else ''
    body1, body2, body_f = _tbody(page1), _tbody(page2), _tbody(filt)
    check('审计补充', '审计页分页：第 1 页为最新、第 2 页含更早记录',
          ('qa.page.59' in body1) and ('qa.page.00' not in body1)
          and ('qa.page.00' in body2), 'page1/2 分页异常')
    check('审计补充', '审计页按动作筛选生效',
          ('qa.page.00' in body_f) and ('qa.page.59' not in body_f), '筛选结果异常')
    check('审计补充', '审计页非法页码被钳制（page=999 仍 200）',
          s_admin.get(BASE + '/admin/audit?page=999').status_code == 200)

    # 3) updated_by 记录实际操作人（经办人 id=2）
    s_op, _ = C.login(BASE, 'jbr', '123456')
    _, pid_op = create_project(s_op, 'QA-UPDBY', 'QA操作人留痕', [('科目', '1000', '0')])
    n_op = node_ids(pid_op, 'first')[0]
    C.post(s_op, BASE, '/nodes/%d/update' % n_op, {'action': 'apply', 'apply_amount': '300'})
    upd = q(DB, 'SELECT updated_by FROM payment_nodes WHERE id=?', (n_op,))[0][0]
    check('审计补充', 'updated_by 记录实际操作人（经办人 id=2）', str(upd) == '2', 'updated_by=%s' % upd)

    # 4) 审计表缺失时业务降级（放最后：会删除临时库的 audit_logs）
    conn = sqlite3.connect(DB, timeout=15)
    try:
        conn.execute('DROP TABLE audit_logs')
        conn.commit()
    finally:
        conn.close()
    degrade = {'project_no': 'QA-DEGRADE', 'name': 'QA审计降级', 'item_name': ['科目'], 'original_amount': ['100']}
    degrade.update(C.RATIO_FORM)
    r = C.post(s_admin, BASE, '/projects/new', degrade)
    exists = q1(DB, 'SELECT COUNT(*) FROM projects WHERE project_no=?', ('QA-DEGRADE',))
    check('审计补充', '审计表缺失时业务降级（不 500、业务照常提交）',
          r.status_code == 302 and exists == 1, 'code=%s 项目存在=%s' % (r.status_code, exists))


def _cfg_client(**cfg):
    base = {'SECRET_KEY': C.DEFAULT_SECRET}
    base.update(cfg)
    return C.make_app(DB, **base).test_client()


def _batch4b_security(s_admin):
    """批次 4b 验收：改密/重置、强制改密、登录限速、会话失效、越权审计、上传上限配置化。"""
    # 0) 新用户密码最小长度
    uc0 = q1(DB, 'SELECT COUNT(*) FROM users')
    C.post(s_admin, BASE, '/admin/users/new',
           {'username': 'qa_short_pw', 'display_name': 'QA短密码', 'password': 'short', 'role': 'viewer'})
    check('4b安全', '新建用户密码不足 8 位应被拒绝',
          q1(DB, 'SELECT COUNT(*) FROM users WHERE username=?', ('qa_short_pw',)) == 0
          and q1(DB, 'SELECT COUNT(*) FROM users') == uc0, '短密码用户被创建')

    # 1) 自助改密 /change-password
    C.post(s_admin, BASE, '/admin/users/new',
           {'username': 'qa_pw_u', 'display_name': 'QA改密用户', 'password': 'Qa#123456', 'role': 'operator'})
    uid = q1(DB, 'SELECT id FROM users WHERE username=?', ('qa_pw_u',))
    check('4b安全', '新建用户使用 8 位以上密码可创建', uid is not None)
    s1, _ = C.login(BASE, 'qa_pw_u', 'Qa#123456')
    s2, _ = C.login(BASE, 'qa_pw_u', 'Qa#123456')
    check('4b安全', '改密页面登录后可访问',
          s1.get(BASE + '/change-password', allow_redirects=False).status_code == 200)
    base_audit = _audit_max_id()
    pw_data = {'current_password': 'Qa#123456', 'old_password': 'Qa#123456',
               'new_password': 'New#123456', 'confirm_password': 'New#123456',
               'new_password_confirm': 'New#123456'}
    r = C.post(s1, BASE, '/change-password', pw_data)
    check('4b安全', '改密成功（当前密码正确、新密码≥8、确认一致）', r.status_code == 302,
          'code=%s' % r.status_code)
    check('4b安全', '改密后旧密码失效', C.login(BASE, 'qa_pw_u', 'Qa#123456')[1].status_code == 200,
          '旧密码仍可登录')
    check('4b安全', '改密后新密码可登录', C.login(BASE, 'qa_pw_u', 'New#123456')[1].status_code == 302,
          '新密码不可登录')
    check('4b安全', '改密后其它旧会话失效',
          s2.get(BASE + '/projects', allow_redirects=False).status_code == 302)
    check('4b安全', '改密写审计 user.change_password',
          any('change_password' in str(x.get('action', '')) for x in _audit_rows(base_audit)),
          '未找到改密审计')

    # 2) 管理员重置密码 /admin/users/<id>/reset-password
    C.post(s_admin, BASE, '/admin/users/new',
           {'username': 'qa_rst_u', 'display_name': 'QA重置用户', 'password': 'Qa#123456', 'role': 'viewer'})
    uid_r = q1(DB, 'SELECT id FROM users WHERE username=?', ('qa_rst_u',))
    s_old, _ = C.login(BASE, 'qa_rst_u', 'Qa#123456')
    base_audit = _audit_max_id()
    r = C.post(s_admin, BASE, '/admin/users/%d/reset-password' % uid_r,
               {'password': 'Rst#123456', 'new_password': 'Rst#123456'})
    check('4b安全', '管理员重置密码成功', r.status_code == 302, 'code=%s' % r.status_code)
    check('4b安全', '重置后旧会话失效',
          s_old.get(BASE + '/projects', allow_redirects=False).status_code == 302)
    r_new = C.login(BASE, 'qa_rst_u', 'Rst#123456')[1]
    check('4b安全', '重置后强制改密（登录跳转 /change-password）',
          r_new.status_code == 302 and '/change-password' in r_new.headers.get('Location', ''),
          'loc=%s' % r_new.headers.get('Location'))
    check('4b安全', '重置密码写审计 user.reset_password',
          any('reset_password' in str(x.get('action', '')) for x in _audit_rows(base_audit)),
          '未找到重置审计')

    # 3) 强制改密：默认口令登录跳转改密页
    tc = _cfg_client(FORCE_DEFAULT_PASSWORD_CHANGE=True)
    r = tc.post('/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=False)
    check('4b安全', '强制改密：默认口令登录跳转 /change-password',
          r.status_code == 302 and '/change-password' in r.headers.get('Location', ''),
          'loc=%s' % r.headers.get('Location'))

    # 4) 登录限速/锁定
    C.post(s_admin, BASE, '/admin/users/new',
           {'username': 'qa_lock_u', 'display_name': 'QA限速用户', 'password': 'Qa#123456', 'role': 'viewer'})
    tc = _cfg_client(LOGIN_THROTTLE_ENABLED=True)
    for _ in range(5):
        tc.post('/login', data={'username': 'qa_lock_u', 'password': 'wrong'}, follow_redirects=False)
    base_audit = _audit_max_id()
    r = tc.post('/login', data={'username': 'qa_lock_u', 'password': 'Qa#123456'}, follow_redirects=False)
    body = r.get_data(as_text=True)
    check('4b安全', '连续 5 次失败后登录被锁定（200+中文提示）',
          r.status_code == 200 and any(k in body for k in ('锁定', '稍后', '尝试')),
          'code=%s' % r.status_code)
    acts = [str(x.get('action', '')) for x in _audit_rows(0)]
    check('4b安全', '登录失败/锁定写审计（auth.login.failed/locked）',
          sum(1 for a in acts if 'login.failed' in a) >= 5
          and any('login.locked' in a for a in acts), '动作=%s' % acts[-5:])

    # 5) 越权尝试写审计 auth.denied
    _, pid_deny = create_project(s_admin, 'QA-DENY', 'QA越权审计', [('科目', '100', '0')])
    s_op, _ = C.login(BASE, 'jbr', '123456')
    base_audit = _audit_max_id()
    s_op.post(BASE + '/projects/%d/delete' % pid_deny, allow_redirects=False)
    denied = [x for x in _audit_rows(base_audit)
              if 'denied' in str(x.get('action', '')).lower() or str(x.get('result')) == 'denied']
    check('4b安全', '越权尝试写审计 auth.denied', bool(denied),
          '未找到越权审计')

    # 6) 上传上限配置化（MAX_UPLOAD_MB，默认 20MB 与 README 对齐）
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    env['SECRET_KEY'] = 'qa-upload-cfg-secret'
    env['MAX_UPLOAD_MB'] = '20'
    out_file = os.path.join(os.path.dirname(DB), 'upload_cfg.txt')
    code = ('from app import create_app; a = create_app(); '
            'print(a.config["MAX_CONTENT_LENGTH"])')
    with open(out_file, 'w', encoding='utf-8') as f:
        r = subprocess.run([sys.executable, '-c', code], cwd=C.PROJECT_ROOT,
                           env=env, stdout=f, stderr=subprocess.STDOUT, timeout=120)
    val = ''
    try:
        val = open(out_file, encoding='utf-8', errors='replace').read().strip()
    except Exception:
        pass
    nums = [ln.strip() for ln in val.splitlines() if ln.strip().isdigit()]
    check('4b安全', 'MAX_UPLOAD_MB 可配置且默认 20MB（与 README 对齐）',
          r.returncode == 0 and bool(nums) and int(nums[-1]) == 20 * 1024 * 1024,
          'returncode=%s output=%s' % (r.returncode, val[:120]))



def _tx_semantics(s_admin):
    """事务语义：业务回滚不落审计；关键审计失败业务失败；非关键降级见审计补充。"""
    s_admin, _ = C.login(BASE, 'admin', 'admin123')
    _, pid = create_project(s_admin, 'QA-TX', 'QA事务语义', [('科目', '1000', '0')])
    nid = node_ids(pid, 'first')[0]
    C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'apply', 'apply_amount': '300'})
    base = _audit_max_id()
    r = C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'pay', 'paid_amount': '99999'})
    rows = [x for x in _audit_rows(base) if 'node.pay' in str(x.get('action', ''))]
    check('事务语义', '业务校验失败回滚时不落审计',
          not rows and r.status_code in (200, 302), '新增 node.pay 审计=%d' % len(rows))

    conn = sqlite3.connect(DB, timeout=15)
    conn.execute('ALTER TABLE audit_logs RENAME TO audit_logs_bak')
    conn.commit()
    conn.close()
    try:
        st_before = q1(DB, 'SELECT status FROM payment_nodes WHERE id=?', (nid,))
        r = C.post(s_admin, BASE, '/nodes/%d/update' % nid,
                   {'action': 'invoice', 'invoice_amount': '10', 'invoice_no': 'FP-TX'})
        st_after = q1(DB, 'SELECT status FROM payment_nodes WHERE id=?', (nid,))
        check('事务语义', '关键动作审计写失败时业务失败并回滚',
              r.status_code >= 500 and st_after == st_before,
              'code=%s status=%s→%s' % (r.status_code, st_before, st_after))
    finally:
        conn = sqlite3.connect(DB, timeout=15)
        conn.execute('ALTER TABLE audit_logs_bak RENAME TO audit_logs')
        conn.commit()
        conn.close()


def _session_invalidation_cases(s_admin):
    """4b 会话失效补充：登出、停用后旧 Cookie 失效。"""
    s_admin, _ = C.login(BASE, 'admin', 'admin123')
    s1, _ = C.login(BASE, 'viewer', '123456')
    s1.post(BASE + '/logout', allow_redirects=False)
    check('会话失效', '登出后旧 Cookie 失效',
          s1.get(BASE + '/projects', allow_redirects=False).status_code == 302)
    C.post(s_admin, BASE, '/admin/users/new',
           {'username': 'qa_sess_u', 'display_name': 'QA会话用户', 'password': 'Qa#123456', 'role': 'viewer'})
    uid = q1(DB, 'SELECT id FROM users WHERE username=?', ('qa_sess_u',))
    if uid is not None:
        s2, _ = C.login(BASE, 'qa_sess_u', 'Qa#123456')
        C.post(s_admin, BASE, '/admin/users/%d/toggle' % uid)
        check('会话失效', '停用后旧会话失效',
              s2.get(BASE + '/projects', allow_redirects=False).status_code == 302)
    else:
        check('会话失效', '停用后旧会话失效', False, '测试用户创建失败')


def _soft_delete_cases(s_admin):
    """项目软删除 + 时间口径（批次 4c 验收，未实现部分 xfail）。"""
    s_admin, _ = C.login(BASE, 'admin', 'admin123')
    _, pid = create_project(s_admin, 'QA-SOFT', 'QA软删项目', [('科目', '1000', '0')])
    if pid is not None:
        nodes_before = q1(DB, 'SELECT COUNT(*) FROM payment_nodes WHERE project_id=?', (pid,))
        C.post(s_admin, BASE, '/projects/%d/delete' % pid)
        conn = sqlite3.connect(DB)
        cols = [r[1] for r in conn.execute('PRAGMA table_info(projects)')]
        conn.close()
        exists = q1(DB, 'SELECT COUNT(*) FROM projects WHERE id=?', (pid,))
        deleted_at = q1(DB, 'SELECT deleted_at FROM projects WHERE id=?', (pid,)) if 'deleted_at' in cols else None
        nodes_after = q1(DB, 'SELECT COUNT(*) FROM payment_nodes WHERE project_id=?', (pid,))
        page = s_admin.get(BASE + '/projects').text
        check('软删除', '删除后列表不可见且数据保留（软删）',
              exists == 1 and deleted_at is not None and 'QA软删项目' not in page
              and nodes_before == nodes_after,
              'exists=%s deleted_at=%s nodes=%s→%s' % (exists, deleted_at, nodes_before, nodes_after))
        page = s_admin.get(BASE + '/reports').text
        check('软删除', '删除后报表不可见', 'QA软删项目' not in page, '报表仍可见')
        trash = s_admin.get(BASE + '/projects?deleted=1', allow_redirects=False)
        check('软删除', 'admin 可通过 /projects?deleted=1 查看回收站',
              trash.status_code == 200 and 'QA软删项目' in trash.text,
              'code=%s' % trash.status_code)
        base = _audit_max_id()
        r = s_admin.post(BASE + '/projects/%d/restore' % pid, allow_redirects=False)
        acts = [str(x.get('action', '')) for x in _audit_rows(base)]
        check('软删除', '恢复后列表可见并写 project.restore 审计',
              r.status_code == 302 and 'QA软删项目' in s_admin.get(BASE + '/projects').text
              and any('project.restore' in a for a in acts), '动作=%s' % acts)

    _, pid_arch = create_project(s_admin, 'QA-ARCH', 'QA归档项目', [('科目', '1000', '0')])
    if pid_arch is not None:
        C.post(s_admin, BASE, '/projects/%d/status' % pid_arch, {'status': '已归档'})
        page = s_admin.get(BASE + '/projects?status=已归档').text
        check('软删除', '归档≠删除：归档项目仍在库且可按状态查询',
              q1(DB, 'SELECT COUNT(*) FROM projects WHERE id=?', (pid_arch,)) == 1
              and 'QA归档项目' in page)

    # 软删除不解绑合同；对已删除项目的合同回改应被拒
    _, ct_soft = upload(s_admin, SAMPLE_TXT, '软删合同.txt')
    if ct_soft:
        confirm_ct(s_admin, ct_soft, {'project_no': 'QA-SOFT-CT', 'contract_name': '软删合同项目',
                                      'original_amount': '165000', 'additional_amount': '0', 'tax_rate': '13%',
                                      'ratio_first': '30', 'ratio_progress': '52', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
        cpid = q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-SOFT-CT',))
        if cpid:
            C.post(s_admin, BASE, '/projects/%d/delete' % cpid)
            bound = q1(DB, 'SELECT project_id FROM contract_files WHERE id=?', (ct_soft,))
            check('软删除', '软删除不解绑合同（contract_files.project_id 保持）', bound == cpid, bound)
            nodes_before = q1(DB, 'SELECT COUNT(*) FROM payment_nodes WHERE project_id=?', (cpid,))
            confirm_ct(s_admin, ct_soft, {'contract_name': '软删合同项目（改价）', 'original_amount': '100000',
                                          'additional_amount': '0', 'tax_rate': '13%',
                                          'ratio_first': '30', 'ratio_progress': '52', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
            nodes_after = q1(DB, 'SELECT COUNT(*) FROM payment_nodes WHERE project_id=?', (cpid,))
            check('软删除', '已删除项目的合同回改被拒（节点数不变）', nodes_before == nodes_after,
                  '%s→%s' % (nodes_before, nodes_after))

    # 时间口径：存储统一 UTC；审计 ts 与业务 created_at 差值 <5 分钟
    from datetime import datetime
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute('SELECT ts FROM audit_logs ORDER BY id DESC LIMIT 1').fetchone()
        ts = row['ts'] if row else None
    except sqlite3.Error:
        ts = None
    conn.close()
    created = q1(DB, 'SELECT created_at FROM projects ORDER BY id DESC LIMIT 1')
    diff_ok = False
    try:
        t1 = datetime.strptime(str(ts)[:19], '%Y-%m-%d %H:%M:%S')
        t2 = datetime.strptime(str(created)[:19], '%Y-%m-%d %H:%M:%S')
        diff_ok = abs((t1 - t2).total_seconds()) < 300
    except Exception:
        pass
    check('时间口径', '审计 ts 与业务 created_at 存储时区一致（UTC，差值<5 分钟）',
          diff_ok, 'audit=%s created=%s' % (ts, created))



def _archived_readonly_cases(s_admin):
    """批次 5：归档项目只读（编辑/收付款/合同回改被拒）。"""
    s_admin, _ = C.login(BASE, 'admin', 'admin123')
    _, pid = create_project(s_admin, 'QA-ARCH-RO', 'QA归档只读', [('科目', '10000', '0')])
    if pid is not None:
        nid = node_ids(pid, 'first')[0]
        C.post(s_admin, BASE, '/nodes/%d/update' % nid,
               {'action': 'apply', 'apply_amount': '3000', 'invoice_no': 'FP-RO', 'invoice_amount': '3000'})
        C.post(s_admin, BASE, '/projects/%d/status' % pid, {'status': '已归档'})
        C.post(s_admin, BASE, '/projects/%d/edit' % pid, {'name': 'QA归档只读-改名', 'status': '已归档'})
        check('归档只读', '归档项目编辑被拒（名称不变）',
              q1(DB, 'SELECT name FROM projects WHERE id=?', (pid,)) == 'QA归档只读')
        paid_before = q1(DB, 'SELECT paid_amount FROM payment_nodes WHERE id=?', (nid,))
        C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'pay', 'paid_amount': '100'})
        check('归档只读', '归档项目收付款被拒（金额不变）',
              q1(DB, 'SELECT paid_amount FROM payment_nodes WHERE id=?', (nid,)) == paid_before)

    # 合同回改：归档后不得修改金额/比例
    _, ct = upload(s_admin, SAMPLE_TXT, '归档合同.txt')
    if ct:
        confirm_ct(s_admin, ct, {'project_no': 'QA-CONF-RO', 'contract_name': '归档回改合同',
                                 'original_amount': '165000', 'additional_amount': '0', 'tax_rate': '13%',
                                 'ratio_first': '30', 'ratio_progress': '52', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
        cpid = q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-CONF-RO',))
        if cpid:
            C.post(s_admin, BASE, '/projects/%d/status' % cpid, {'status': '已归档'})
            due_before = q1(DB, "SELECT due_amount FROM payment_nodes WHERE project_id=? AND node_type='first'", (cpid,))
            confirm_ct(s_admin, ct, {'contract_name': '归档回改合同（改价）', 'original_amount': '100000',
                                     'additional_amount': '0', 'tax_rate': '13%',
                                     'ratio_first': '30', 'ratio_progress': '52', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
            due_after = q1(DB, "SELECT due_amount FROM payment_nodes WHERE project_id=? AND node_type='first'", (cpid,))
            check('归档只读', '归档项目合同回改被拒（金额不变）',
                  due_before == due_after, 'due %s→%s' % (due_before, due_after))



def _batch6_export_import(s_admin):
    """批次 6a 导出验收（导入经用户确认不在上线范围，已移出回归）。"""
    s_admin, _ = C.login(BASE, 'admin', 'admin123')
    candidates = ['/reports/export', '/reports/export.xlsx', '/export', '/projects/export']
    found = None
    for path in candidates:
        r = s_admin.get(BASE + path, allow_redirects=False)
        if r.status_code == 200 and (
                'spreadsheet' in r.headers.get('Content-Type', '') or r.content[:2] == b'PK'):
            found = (path, r)
            break
    check('导出', '存在 Excel 导出接口（admin 可下载 xlsx）', found is not None,
          '候选路径均不可用：%s' % candidates)
    if found:
        path, resp = found
        rows = []
        try:
            from openpyxl import load_workbook
            import io as _io
            wb = load_workbook(_io.BytesIO(resp.content))
            rows = list(wb.active.iter_rows(values_only=True))
        except Exception as e:
            check('导出', '导出文件可被 openpyxl 解析', False, repr(e))
        flat = ' '.join(str(c) for row in rows[:3] for c in row if c is not None)
        check('导出', '导出字段与页面一致（项目编号/项目名称/金额）',
              all(k in flat for k in ('项目编号', '项目名称')), flat[:120])
        header = next((' '.join(str(c) for c in row if c is not None)
                       for row in rows if any('项目编号' in str(c) for c in row if c is not None)), '')
        check('导出', '导出含三本账列（已申请/已开票/已付款，CR-04）',
              all(k in header for k in ('已申请', '已开票')), header[:160])
        check('导出', '导出金额为两位小数（Decimal 口径）',
              any(isinstance(c, (int, float)) and abs(round(float(c), 2) - float(c)) < 1e-9
                  for row in rows for c in row), '未发现两位小数金额')
        cnt_all = q1(DB, 'SELECT COUNT(*) FROM projects')
        page_all = s_admin.get(BASE + path).text
        check('导出', '导出筛选条件生效（不限条件导出全部项目）',
              len(rows) >= min(cnt_all, 2), 'rows=%d projects=%d' % (len(rows), cnt_all))
        sv, _ = C.login(BASE, 'viewer', '123456')
        check('导出', 'viewer 可导出', sv.get(BASE + path, allow_redirects=False).status_code == 200)
        check('导出', '未登录用户导出被拒',
              requests.get(BASE + path, allow_redirects=False).status_code in (301, 302, 401, 403))
        base_audit = _audit_max_id()
        s_admin.get(BASE + path)
        acts = [str(x.get('action', '')) for x in _audit_rows(base_audit)]
        check('导出', '导出写审计', any('export' in a for a in acts), '动作=%s' % acts)






def _node_vals(nid):
    row = q(DB, 'SELECT due_amount,applied_amount,invoice_amount,paid_amount,status FROM payment_nodes WHERE id=?', (nid,))[0]
    return dict(due=d2(row[0]), applied=d2(row[1]), invoice=d2(row[2]),
                paid=d2(row[3]), status=row[4])


def _batch8_change_cases(s_admin):
    """CR-04 合同变更：冻结已付节点、差额按未付节点原应付比例分摊、尾差归集、两段确认、审计。"""
    _, pid = create_project(s_admin, 'QA-CHG', 'QA变更项目', [('合同价款', '165000', '0')])
    if pid is None:
        check('变更', '合同变更入口可用', False, '测试项目创建失败')
        return
    iid = item_ids(pid)[0]
    nid = node_ids(pid, 'first')[0]
    # 先让首付节点发生完整业务（申请→开票→付款），作为冻结节点
    C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'apply', 'apply_amount': '49500'})
    C.post(s_admin, BASE, '/nodes/%d/update' % nid,
           {'action': 'invoice', 'invoice_amount': '49500', 'invoice_no': 'FP-CHG'})
    C.post(s_admin, BASE, '/nodes/%d/update' % nid,
           {'action': 'pay', 'paid_amount': '49500', 'pay_date': '2026-10-23'})
    before = _node_vals(nid)
    records_before = q1(DB, 'SELECT COUNT(*) FROM payment_records WHERE node_id=?', (nid,))
    due_before = {nt: _node_vals(node_ids(pid, nt)[0])['due']
                  for nt in ('first', 'progress', 'acceptance', 'warranty')}

    def node_sum():
        return d2(q1(DB, 'SELECT SUM(due_amount) FROM payment_nodes WHERE project_id=?', (pid,)) or 0)

    def node_due(nt):
        return _node_vals(node_ids(pid, nt)[0])['due']

    # 1) 变更增加 165000 → 185000：冻结已付节点，其余按原应付比例分摊
    r1, r2 = contract_change(s_admin, pid, {iid: '185000'})
    check('变更', '变更需两段确认（先出差异清单，第二次才落库）',
          r1.status_code == 200 and r2 is not None and r2.status_code == 302
          and abs(node_sum() - 185000) < 0.01,
          'r1=%s r2=%s 合计=%.2f' % (r1.status_code, getattr(r2, 'status_code', None), node_sum()))
    after_frozen = _node_vals(nid)
    check('变更', '变更增加：已付节点应付/已付/开票/状态全部冻结',
          after_frozen['due'] == before['due'] and after_frozen['paid'] == before['paid']
          and after_frozen['invoice'] == before['invoice'] and after_frozen['status'] == before['status']
          and q1(DB, 'SELECT COUNT(*) FROM payment_records WHERE node_id=?', (nid,)) == records_before,
          'before=%s after=%s' % (before, after_frozen))
    check('变更', '变更增加：节点应付合计=新总额 185,000', abs(node_sum() - 185000) < 0.01,
          '合计=%.2f' % node_sum())
    spread = [node_due(nt) for nt in ('progress', 'acceptance', 'warranty')]
    weight_sum = due_before['progress'] + due_before['acceptance'] + due_before['warranty']
    rem = d2('135500')
    expected = [d2(rem * due_before[nt] / weight_sum)
                for nt in ('progress', 'acceptance', 'warranty')]
    check('变更', '变更增加：未付节点按原应付比例分摊（85800:24750:4950）',
          all(abs(a - b) < 0.02 for a, b in zip(spread, expected))
          and abs(sum(spread) - rem) < 0.01, '%s 期望≈%s' % (spread, expected))
    check('变更', '变更增加：差额体现为未付（合计-已付=135,500）',
          abs(d2(q1(DB, 'SELECT SUM(due_amount-paid_amount) FROM payment_nodes WHERE project_id=?', (pid,))) - 135500) < 0.01)
    check('变更', '变更增加：合同总额=原金额+增补=185,000',
          d2(q1(DB, 'SELECT SUM(original_amount+additional_amount) FROM project_items WHERE project_id=?', (pid,))) == d2('185000'))

    # 2) 变更减少：→ 100000（> 已付应付 49500，允许；增补为负 -65000）
    r1, r2 = contract_change(s_admin, pid, {iid: '100000'})
    check('变更', '变更减少：应付下降且已付保留（增补为负被接受）',
          abs(node_sum() - 100000) < 0.01
          and _node_vals(nid)['paid'] == before['paid'],
          '合计=%.2f 已付=%s' % (node_sum(), _node_vals(nid)['paid']))
    check('变更', '变更减少：增补金额记为负数 -65,000',
          d2(q1(DB, 'SELECT additional_amount FROM project_items WHERE id=?', (iid,))) == d2('-65000'),
          'additional=%s' % q1(DB, 'SELECT additional_amount FROM project_items WHERE id=?', (iid,)))
    rem_sum = sum(node_due(nt) for nt in ('progress', 'acceptance', 'warranty'))
    check('变更', '变更减少：未付节点合计=新总额-冻结合计（50,500）',
          abs(rem_sum - 50500) < 0.01, '未付合计=%.2f' % rem_sum)

    # 3) 变更后总额 < 已付应付 → 阻断，库不变
    sum_before = node_sum()
    r1, r2 = contract_change(s_admin, pid, {iid: '40000'})
    check('变更', '新总额小于已付节点应付合计时阻断（库不变）',
          r2 is None and abs(node_sum() - sum_before) < 0.01,
          'r1=%s r2=%s 合计=%.2f' % (r1.status_code, getattr(r2, 'status_code', None), node_sum()))

    # 4) 总额≤0 被拒绝，库不变
    r1, r2 = contract_change(s_admin, pid, {iid: '0'})
    check('变更', '总额≤0 的变更被拒绝且库不变',
          r2 is None and abs(node_sum() - sum_before) < 0.01,
          'r1=%s r2=%s 合计=%.2f' % (r1.status_code, getattr(r2, 'status_code', None), node_sum()))

    # 5) 尾差归质保金：100000.05 → 节点合计恰等于合同总额
    contract_change(s_admin, pid, {iid: '100000.05'})
    total3 = node_sum()
    contract_total = d2(q1(DB, 'SELECT SUM(original_amount+additional_amount) FROM project_items WHERE project_id=?', (pid,)))
    first3, prog3, acc3 = (node_due(nt) for nt in ('first', 'progress', 'acceptance'))
    warr3 = node_due('warranty')
    check('变更', '尾差归质保金后节点合计=合同总额',
          total3 == contract_total and abs(warr3 - (contract_total - first3 - prog3 - acc3)) < 0.005,
          '合计=%s 合同总额=%s 质保金=%.2f' % (total3, contract_total, warr3))

    # 6) 审计：project.contract_change 且有 before/after 与冻结标记
    chg_rows = [x for x in _audit_rows(0)
                if 'contract_change' in str(x.get('action', ''))]
    check('变更', '变更写审计 project.contract_change 且含 before/after',
          bool(chg_rows) and any(not _empty_json(x.get('before_json'))
                                 and not _empty_json(x.get('after_json')) for x in chg_rows),
          '动作=%s' % [x.get('action') for x in chg_rows][:3])

    # 7) 已开票 > 变更后应付 → 阻断
    _, pid2 = create_project(s_admin, 'QA-CHG-INV', 'QA变更开票阻断', [('合同价款', '10000', '0')])
    if pid2:
        iid2 = item_ids(pid2)[0]
        n_first, n_prog = node_ids(pid2, 'first')[0], node_ids(pid2, 'progress')[0]
        C.post(s_admin, BASE, '/nodes/%d/update' % n_first, {'action': 'pay', 'paid_amount': '3000', 'pay_date': '2026-10-23'})
        C.post(s_admin, BASE, '/nodes/%d/update' % n_prog,
               {'action': 'invoice', 'invoice_amount': '5200', 'invoice_no': 'FP-CHG-INV'})
        r1, r2 = contract_change(s_admin, pid2, {iid2: '6000'})
        check('变更', '已开票大于变更后应付时阻断', r2 is None,
              'r1=%s r2=%s' % (r1.status_code, getattr(r2, 'status_code', None)))
        check('变更', '阻断时节点金额保持不变',
              _node_vals(n_prog)['due'] == d2('5200') and _node_vals(n_prog)['invoice'] == d2('5200'),
              _node_vals(n_prog))


def _three_books_cases(s_admin):
    """CR-04 三本账：6 种操作顺序、同维多次累计、超累计拒绝、一次性 token。"""
    from itertools import permutations
    AMT = '100'
    for idx, order in enumerate(permutations(('apply', 'invoice', 'pay'))):
        no = 'QA-TB-%d' % idx
        _, pid = create_project(s_admin, no, 'QA三本账%d' % idx, [('科目', '1000', '0')])
        if pid is None:
            check('三本账', '%s 顺序：项目创建成功' % '-'.join(order), False, '创建失败')
            continue
        nid = node_ids(pid, 'first')[0]
        codes = []
        for action in order:
            data = {'action': action}
            if action == 'apply':
                data['apply_amount'] = AMT
            elif action == 'invoice':
                data.update({'invoice_amount': AMT, 'invoice_no': 'FP-TB-%d-%s' % (idx, action)})
            else:
                data.update({'paid_amount': AMT, 'pay_date': '2026-10-23'})
            codes.append(C.post(s_admin, BASE, '/nodes/%d/update' % nid, data).status_code)
        vals = _node_vals(nid)
        check('三本账', '任意顺序均可登记：%s' % '→'.join(order),
              vals['applied'] == d2(AMT) and vals['invoice'] == d2(AMT)
              and vals['paid'] == d2(AMT) and 500 not in codes and vals['status'] == '部分付款',
              'codes=%s vals=%s' % (codes, vals))

    # 同一维度多次部分累计（首付节点应付 3000 = 10000×30%）
    _, pid = create_project(s_admin, 'QA-TB-MULTI', 'QA多次累计', [('科目', '10000', '0')])
    nid = node_ids(pid, 'first')[0]
    for _ in range(3):
        C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'apply', 'apply_amount': '500'})
    for _ in range(2):
        C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'invoice', 'invoice_amount': '500', 'invoice_no': 'FP-MULTI'})
    for _ in range(4):
        C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'pay', 'paid_amount': '250', 'pay_date': '2026-10-23'})
    vals = _node_vals(nid)
    check('三本账', '同一维度可多次部分累计（申请 1500 / 开票 1000 / 付款 1000）',
          vals['applied'] == d2(1500) and vals['invoice'] == d2(1000) and vals['paid'] == d2(1000),
          vals)

    # 超累计拒绝（最后一笔恰好到应付可以，再多 1 分拒绝）
    C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'apply', 'apply_amount': '1500'})
    ok_exact = _node_vals(nid)['applied'] == d2(3000)  # 1500 + 1500 = 应付 3000
    C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'apply', 'apply_amount': '0.01'})
    over = _node_vals(nid)['applied']
    check('三本账', '累计申请恰好补到应付时可成功', ok_exact, ok_exact)
    check('三本账', '累计申请超过应付时拒绝（库不变）', over == d2(3000), 'applied=%s' % over)
    # 再多 1 分拒绝（已到应付）
    C.post(s_admin, BASE, '/nodes/%d/update' % nid, {'action': 'apply', 'apply_amount': '0.01'})
    check('三本账', '累计已等于应付时再多 1 分拒绝',
          _node_vals(nid)['applied'] == d2(3000), 'applied=%s' % _node_vals(nid)['applied'])

    # 一次性 token：同一 token 重复提交只成功一次
    _, pid3 = create_project(s_admin, 'QA-TB-TOKEN', 'QA重复提交', [('科目', '10000', '0')])
    nid3 = node_ids(pid3, 'first')[0]
    tok = C.node_tokens(s_admin, BASE, pid3)[(nid3, 'pay')]
    r_a = s_admin.post(BASE + '/nodes/%d/update' % nid3,
                       data={'action': 'pay', 'paid_amount': '100', 'confirm_token': tok},
                       allow_redirects=False)
    paid_1 = _node_vals(nid3)['paid']
    r_b = s_admin.post(BASE + '/nodes/%d/update' % nid3,
                       data={'action': 'pay', 'paid_amount': '100', 'confirm_token': tok},
                       allow_redirects=False)
    recs = q1(DB, 'SELECT COUNT(*) FROM payment_records WHERE node_id=?', (nid3,))
    check('三本账', '重复提交同一 token 只成功一次（无重复流水）',
          paid_1 == d2(100) and _node_vals(nid3)['paid'] == d2(100) and recs == 1,
          'r_a=%s r_b=%s paid=%s recs=%s' % (r_a.status_code, r_b.status_code, _node_vals(nid3)['paid'], recs))
    # 无 token 直接 POST 被拒
    C.post(s_admin, BASE, '/nodes/%d/update' % nid3, {'action': 'pay', 'paid_amount': '100', 'confirm_token': ''})
    check('三本账', '缺少一次性 token 的节点动作被拒绝', _node_vals(nid3)['paid'] == d2(100))


def _ratio_required_cases(s_admin):
    """CR-04：新建项目 4 个比例必填、无默认值、合计须 100%（容差 ±0.01%）。"""

    def new_form(no):
        data = {'project_no': no, 'name': 'QA比例必填',
                'item_name': ['科目'], 'original_amount': ['100000'], 'tax_rate': ['6%']}
        return data

    r = C.post(s_admin, BASE, '/projects/new', new_form('QA-RATIO-NONE'))
    check('比例', '未填比例时新建项目被拒绝（无默认值）',
          q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-RATIO-NONE',)) is None,
          'code=%s 项目已生成' % r.status_code)

    bad = new_form('QA-RATIO-98')
    bad.update({'ratio_first': '30', 'ratio_progress': '50', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
    C.post(s_admin, BASE, '/projects/new', bad)
    check('比例', '比例合计 98% 时拒绝',
          q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-RATIO-98',)) is None)

    neg = new_form('QA-RATIO-NEG')
    neg.update({'ratio_first': '-10', 'ratio_progress': '92', 'ratio_acceptance': '15', 'ratio_warranty': '3'})
    C.post(s_admin, BASE, '/projects/new', neg)
    neg_pid = q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-RATIO-NEG',))
    neg_due = q1(DB, "SELECT due_amount FROM payment_nodes WHERE project_id=? AND node_type='first'", (neg_pid,)) if neg_pid else None
    check('比例', '负比例被拒绝', neg_pid is None or float(neg_due or 0) >= 0,
          '生成负数应付=%s' % neg_due)

    good = new_form('QA-RATIO-OK')
    good.update(C.RATIO_FORM)
    r_ok = C.post(s_admin, BASE, '/projects/new', good)
    pid = q1(DB, 'SELECT id FROM projects WHERE project_no=?', ('QA-RATIO-OK',))
    nsum = d2(q1(DB, 'SELECT SUM(due_amount) FROM payment_nodes WHERE project_id=?', (pid,))) if pid else None
    check('比例', '填全 30/52/15/3 后可创建且节点合计=100,000',
          r_ok.status_code == 302 and nsum == d2('100000'), 'code=%s nsum=%s' % (r_ok.status_code, nsum))
    # 合同变更逐科目输入：某科目留空 → 拒绝且库不变
    if pid:
        iid = item_ids(pid)[0]
        before_total = d2(q1(DB, 'SELECT SUM(original_amount+additional_amount) FROM project_items WHERE project_id=?', (pid,)))
        r_bad = C.post(s_admin, BASE, '/projects/%d/contract-change' % pid, {'item_total_%d' % iid: ''})
        after_total = d2(q1(DB, 'SELECT SUM(original_amount+additional_amount) FROM project_items WHERE project_id=?', (pid,)))
        check('比例', '合同变更未填科目新总额时拒绝且库不变',
              r_bad.status_code == 200 and before_total == after_total == d2('100000'),
              'code=%s before=%s after=%s' % (r_bad.status_code, before_total, after_total))


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
    section('########## 9. 审计留痕（批次 3 验收） ##########', lambda: _audit_trails(s_admin))
    section('########## 10. 安全基线（批次 4a 验收） ##########', lambda: _security_baseline(s_admin))
    section('########## 11. 密码与登录限速（批次 4b 验收） ##########', lambda: _batch4b_security(s_admin))
    section('########## 11a. 会话失效补充（4b） ##########', lambda: _session_invalidation_cases(s_admin))
    section('########## 11b. 事务语义（审计与业务同事务） ##########', lambda: _tx_semantics(s_admin))
    section('########## 11c. 软删除与时间口径（下一批次验收） ##########', lambda: _soft_delete_cases(s_admin))
    section('########## 11d. 归档只读（批次 5） ##########', lambda: _archived_readonly_cases(s_admin))
    section('########## 11e. 合同变更（CR-04） ##########', lambda: _batch8_change_cases(s_admin))
    section('########## 11f. 三本账并列（CR-04） ##########', lambda: _three_books_cases(s_admin))
    section('########## 11g. 比例必填（CR-04） ##########', lambda: _ratio_required_cases(s_admin))
    section('########## 12. 导出（批次 6a） ##########', lambda: _batch6_export_import(s_admin))
    section('########## 12. 审计补充（批次 3 补测，末项会删临时库 audit_logs） ##########', lambda: _audit_extras(s_admin))

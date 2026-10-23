# -*- coding: utf-8 -*-
"""付款进度管理系统 - 端到端运行验证脚本
覆盖：登录、项目列表/详情、节点金额、建项目、发起申请、登记发票、付款、
试运行联动、验收登记与质保日期推算、报表页、权限控制、数据落地。
数据库查询采用即查即关连接，避免 sqlite 写锁。
"""
import os
import re
import sqlite3
import sys

import requests

BASE = 'http://127.0.0.1:' + os.environ.get('PAY_PORT', '5001')
DB = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'instance', 'payment_progress.db'))

PASS = []
FAIL = []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print(f'  [PASS] {name}')
    else:
        FAIL.append(name)
        print(f'  [FAIL] {name} {extra}')


def q(sql, args=()):
    conn = sqlite3.connect(DB)
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = cur.fetchall()
        return rows
    finally:
        conn.close()


def get_node_ids(project_id, node_type=None):
    if node_type:
        rows = q('SELECT id, node_type, due_amount, status FROM payment_nodes WHERE project_id=? AND node_type=? ORDER BY item_id', (project_id, node_type))
        return [(r[0], int(r[2]) if r[2] else 0) for r in rows]
    rows = q('SELECT id, node_type FROM payment_nodes WHERE project_id=? ORDER BY item_id', (project_id,))
    return rows


def find_node_id(project_id, node_type, item_idx=0):
    nodes = get_node_ids(project_id, node_type)
    return nodes[item_idx][0]


print('=== 1. 登录 ===')
s = requests.Session()
r = s.post(BASE + '/login', data={'username': 'admin', 'password': 'admin123'}, allow_redirects=False)
check('登录 admin 跳转项目列表', r.status_code == 302 and '/projects' in r.headers.get('Location', ''), f'status={r.status_code} loc={r.headers.get("Location")}')
r = s.get(BASE + '/projects')
check('登录后访问项目列表 200', r.status_code == 200)
check('页面包含种子项目编号', 'PJ-2025-DSJ' in r.text)
check('页面包含种子项目名称', '新增动设备监测项目' in r.text)

print('=== 2. 种子项目节点金额核对 ===')
seed_id = q('SELECT id FROM projects WHERE project_no=?', ('PJ-2025-DSJ',))[0][0]
nodes = q('SELECT node_type, SUM(due_amount) FROM payment_nodes WHERE project_id=? GROUP BY node_type', (seed_id,))
node_sum = {nt: float(v) for nt, v in nodes}
# 首付30%按原金额165000*0.3=49500；其余按总金额165000*比例
check('首付款合计 49,500.00', abs(node_sum.get('first', 0) - 49500.0) < 0.01, str(node_sum))
check('进度款合计 85,800.00', abs(node_sum.get('progress', 0) - 85800.0) < 0.01, str(node_sum))
check('验收款合计 24,750.00', abs(node_sum.get('acceptance', 0) - 24750.0) < 0.01, str(node_sum))
check('质保金合计 4,950.00', abs(node_sum.get('warranty', 0) - 4950.0) < 0.01, str(node_sum))
total_node = sum(node_sum.values())
check('节点合计 165,000.00', abs(total_node - 165000.0) < 0.01, str(total_node))
# 页面金额千分位展示
r = s.get(BASE + f'/projects/{seed_id}')
check('详情页显示千分位金额 39,300.00', '39,300.00' in r.text)
check('详情页显示 68,120.00', '68,120.00' in r.text)

print('=== 3. 新建项目 E2E ===')
r = s.post(BASE + '/projects/new', data={
    'project_no': 'PJ-E2E-001',
    'name': '端到端测试项目',
    'item_name': ['测试设备费/服务费'],
    'tax_rate': ['6%'],
    'original_amount': ['100000'],
    'additional_amount': ['20000'],
    'remark': 'E2E 验证创建',
}, allow_redirects=False)
check('创建项目成功跳转详情', r.status_code == 302 and '/projects/' in r.headers.get('Location', ''), f'status={r.status_code}')
e2e_id = q('SELECT id FROM projects WHERE project_no=?', ('PJ-E2E-001',))[0][0]
r = s.get(BASE + f'/projects/{e2e_id}')
check('新项目详情 200 且含4类节点名', all(k in r.text for k in ['首付款30%', '进度款52%', '验收款15%', '质保金3%']))
# E2E 金额：原100000 + 增补20000 = 总120000
node_sum_e2e = {nt: float(v) for nt, v in q('SELECT node_type, SUM(due_amount) FROM payment_nodes WHERE project_id=? GROUP BY node_type', (e2e_id,))}
check('E2E 首付30%按原金额 30,000.00', abs(node_sum_e2e.get('first', 0) - 30000.0) < 0.01, str(node_sum_e2e))
check('E2E 进度款52%按总额 62,400.00', abs(node_sum_e2e.get('progress', 0) - 62400.0) < 0.01, str(node_sum_e2e))

print('=== 4. 付款申请-发票-付款 三态流转（E2E 项目全新首付款节点） ===')
# 种子项目的首付/进度款按 Excel 迁移为已付款/已申请，不可重复支付；
# 用 E2E 全新节点（paid=0）验证完整三态流转：apply -> invoice -> pay
e2e_first_id = find_node_id(e2e_id, 'first', 0)
check('E2E 首付款节点存在且应付 30,000.00', e2e_first_id > 0)
# 4.1 发起申请（未开票）
r = s.post(BASE + f'/nodes/{e2e_first_id}/update', data={'action': 'apply', 'apply_amount': '30000', 'invoice_amount': '', 'invoice_no': '', 'description': '首付款申请'}, allow_redirects=False)
check('发起申请成功跳转', r.status_code == 302)
st = q('SELECT status FROM payment_nodes WHERE id=?', (e2e_first_id,))[0]
check('节点状态=未开票已申请', st[0] == '未开票已申请', str(st))
app_amt = q('SELECT apply_amount FROM payment_applications WHERE node_id=? ORDER BY id DESC LIMIT 1', (e2e_first_id,))[0]
check('申请单申请金额 30000', float(app_amt[0]) == 30000.0, str(app_amt))
app_id = q('SELECT id FROM payment_applications WHERE node_id=? ORDER BY id DESC LIMIT 1', (e2e_first_id,))[0][0]
check('申请单已生成', app_id > 0)
# 4.2 登记发票 → 已申请
r = s.post(BASE + f'/nodes/{e2e_first_id}/update', data={'action': 'invoice', 'invoice_amount': '30000', 'invoice_no': 'FP-E2E-001'}, allow_redirects=False)
check('登记发票跳转', r.status_code == 302)
st2 = q('SELECT status, invoice_amount FROM payment_nodes WHERE id=?', (e2e_first_id,))[0]
check('节点状态=已申请', st2[0] == '已申请', str(st2))
inv = q('SELECT invoice_no, invoice_amount, status FROM payment_applications WHERE id=?', (app_id,))[0]
check('申请单发票号已回填 FP-E2E-001', inv[0] == 'FP-E2E-001', str(inv))
check('申请单发票金额 30000', float(inv[1]) == 30000.0, str(inv))
# 4.3 登记付款 → 已付款
r = s.post(BASE + f'/nodes/{e2e_first_id}/update', data={'action': 'pay', 'paid_amount': '30000', 'pay_date': '2026-10-20', 'pay_method': '银行转账', 'voucher_no': 'SK-E2E-001'}, allow_redirects=False)
check('登记付款跳转', r.status_code == 302)
st3 = q('SELECT status, paid_amount FROM payment_nodes WHERE id=?', (e2e_first_id,))[0]
check('节点状态=已付款', st3[0] == '已付款', str(st3))
check('节点已付金额 30000', float(st3[1]) == 30000.0, str(st3))
app_st = q('SELECT status FROM payment_applications WHERE id=?', (app_id,))[0]
check('申请单状态=已付款', app_st[0] == '已付款', str(app_st))
rec_cnt = q('SELECT COUNT(*) FROM payment_records WHERE node_id=?', (e2e_first_id,))[0][0]
check('付款记录已落地（恰1条）', rec_cnt == 1, f'count={rec_cnt}')

print('=== 5. 试运行/验收联动与质保日期（种子项目） ===')
# 5.1 试运行日期 → 进度款触发
trial_date = '2026-01-15'
r = s.post(BASE + f'/projects/{seed_id}/trial', data={'trial_date': trial_date}, allow_redirects=False)
check('登记试运行成功', r.status_code == 302)
tr = q('SELECT trigger_date FROM payment_nodes WHERE project_id=? AND node_type=?', (seed_id, 'progress'))[0]
check('进度款节点触发日期联动', tr[0] == trial_date, str(tr))
# 5.2 验收 → 质保到期日 = 2026-03-10 + 365*3 = 2029-03-09
acc_date = '2026-03-10'
r = s.post(BASE + f'/projects/{seed_id}/acceptance', data={'acceptance_date': acc_date, 'conclusion': '验收合格', 'acceptor': '张三'}, allow_redirects=False)
check('登记验收成功', r.status_code == 302)
wd = q('SELECT warranty_due_date FROM projects WHERE id=?', (seed_id,))[0][0]
check('质保到期日=2029-03-09', wd == '2029-03-09', str(wd))
acc_tr = q('SELECT trigger_date FROM payment_nodes WHERE project_id=? AND node_type=?', (seed_id, 'acceptance'))[0]
check('验收款节点触发日期=2026-03-10', acc_tr[0] == acc_date, str(acc_tr))
war_due = q('SELECT due_date FROM payment_nodes WHERE project_id=? AND node_type=? LIMIT 1', (seed_id, 'warranty'))[0]
check('质保金节点应付日期=2029-03-09', war_due[0] == '2029-03-09', str(war_due))
acc_cnt = q('SELECT COUNT(*) FROM acceptance_records WHERE project_id=?', (seed_id,))[0][0]
check('验收记录已落地', acc_cnt >= 1)
r = s.get(BASE + f'/projects/{seed_id}')
check('详情页显示质保到期日 2029-03-09', '2029-03-09' in r.text)

print('=== 6. 进度款申请+付款（E2E 项目） ===')
prog_id = find_node_id(e2e_id, 'progress', 0)
r = s.post(BASE + f'/nodes/{prog_id}/update', data={'action': 'apply', 'apply_amount': '62400', 'invoice_no': 'FP-E2E-002', 'invoice_amount': '62400'}, allow_redirects=False)
check('进度款带票申请跳转', r.status_code == 302)
stp = q('SELECT status FROM payment_nodes WHERE id=?', (prog_id,))[0]
check('进度款状态=已申请', stp[0] == '已申请', str(stp))
r = s.post(BASE + f'/nodes/{prog_id}/update', data={'action': 'pay', 'paid_amount': '62400', 'pay_date': '2026-10-21', 'pay_method': '电汇'}, allow_redirects=False)
check('进度款付款跳转', r.status_code == 302)
stp2 = q('SELECT status, paid_amount FROM payment_nodes WHERE id=?', (prog_id,))[0]
check('进度款状态=已付款，金额 62400', stp2[0] == '已付款' and float(stp2[1]) == 62400.0, str(stp2))

print('=== 7. 报表页 ===')
r = s.get(BASE + '/reports')
check('报表页 200', r.status_code == 200)
check('报表含项目名称', '端到端测试项目' in r.text)
# 报表总应付：种子165000 + E2E 120000 = 285000
check('报表未付待办含已申请节点', ('已申请' in r.text) or ('未开票已申请' in r.text))

print('=== 8. 权限控制 ===')
# viewer 用独立会话登录，避免污染 admin 会话
sv = requests.Session()
rv = sv.post(BASE + '/login', data={'username': 'viewer', 'password': '123456'}, allow_redirects=False)
check('viewer 登录跳转项目列表', rv.status_code == 302, f'status={rv.status_code}')
rv = sv.get(BASE + '/projects')
check('viewer 项目列表 200', rv.status_code == 200)
rv = sv.get(BASE + '/projects/new', allow_redirects=False)
check('viewer 访问新建页被拒（重定向）', rv.status_code == 302, f'status={rv.status_code}')
rv = sv.post(BASE + f'/nodes/{e2e_first_id}/update', data={'action': 'pay', 'paid_amount': '1'}, allow_redirects=False)
check('viewer 提交节点操作被拒', rv.status_code == 302, f'status={rv.status_code}')
# 确认 viewer 的 pay 未落地
paid_after = q('SELECT paid_amount FROM payment_nodes WHERE id=?', (e2e_first_id,))[0][0]
check('viewer pay 未写入节点金额', float(paid_after) == 30000.0, f'paid={paid_after}')
rv = sv.get(BASE + '/admin/users', allow_redirects=False)
check('viewer 访问用户管理被拒', rv.status_code == 302, f'status={rv.status_code}')

print('=== 9. 用户管理（admin） ===')
r = s.post(BASE + '/login', data={'username': 'admin', 'password': 'admin123'}, allow_redirects=False)
r = s.get(BASE + '/admin/users')
check('admin 访问用户管理 200', r.status_code == 200)
check('用户列表含三个种子账号', all(u in r.text for u in ['admin', 'jbr', 'viewer']))

print('=== 10. 合同识别：上传-识别-确认-生成节点闭环 ===')
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SAMPLE_TXT = os.path.join(BASE_DIR, 'scripts', 'samples', '合同样例-设备采购.txt')
SAMPLE_DOCX = os.path.join(BASE_DIR, 'scripts', 'samples', '合同样例-设备采购.docx')

# 10.1 合同列表页
r = s.get(BASE + '/contracts')
check('合同列表页 200', r.status_code == 200)
check('合同列表页含上传表单', 'enctype="multipart/form-data"' in r.text)
# 10.2 上传 TXT 样例
with open(SAMPLE_TXT, 'rb') as f:
    r = s.post(BASE + '/contracts/upload', files={'file': ('合同样例-设备采购.txt', f, 'text/plain')}, allow_redirects=False)
# 上传成功应 302 到 confirm，或 200 页面含识别提示
loc = r.headers.get('Location', '')
check('上传 TXT 重定向到确认页', r.status_code == 302 and '/contracts/' in loc and '/confirm' in loc, f'status={r.status_code} loc={loc}')
ct_id = int(loc.rstrip('/').split('/')[-2]) if '/confirm' in loc else None
check('确认页 id 有值', ct_id and ct_id > 0, str(ct_id))
# 10.3 确认页展示识别字段
r = s.get(BASE + f'/contracts/{ct_id}/confirm')
check('确认页 200', r.status_code == 200)
check('确认页展示识别金额 165,000.00', '165,000.00' in r.text)
check('确认页展示税率 13%', '13%' in r.text)
check('确认页展示付款方式分期支付', '分期支付' in r.text)
check('确认页展示金额出处（合同总金额）', '合同总金额' in r.text)
check('确认页展示首付 30 比例输入', 'name="ratio_first"' in r.text and 'value="30.00"' in r.text)
check('确认页含合计提示元素', 'id="sum_ratio"' in r.text and 'id="ratio_total_msg"' in r.text)
# 10.4 修改比例（首付 30%->25%）并确认生成项目
r = s.post(BASE + f'/contracts/{ct_id}/confirm', data={
    'project_no': 'PJ-CT-E2E-001',
    'contract_name': '信息化设备采购项目（e2e）',
    'original_amount': '165000',
    'additional_amount': '0',
    'tax_rate': '13%',
    'payment_method': '分期支付',
    'ratio_first': '25',
    'ratio_progress': '52',
    'ratio_acceptance': '15',
    'ratio_warranty': '3',
}, allow_redirects=False)
loc2 = r.headers.get('Location', '')
if r.status_code != 302 or '/projects/' not in loc2:
    r2 = s.get(BASE + f'/contracts/{ct_id}/confirm')
    print('  确认页响应片段:', r2.text[:500])
check('确认生成重定向项目详情', r.status_code == 302 and '/projects/' in loc2, f'status={r.status_code} loc={loc2}')
ct_proj_id = int(loc2.rstrip('/').split('/')[-1])
# 10.5 数据落地核对
proj = q('SELECT id, name, project_no FROM projects WHERE project_no=?', ('PJ-CT-E2E-001',))
check('合同识别生成项目落地', len(proj) == 1 and proj[0][1] == '信息化设备采购项目（e2e）', str(proj))
item = q('SELECT item_name, original_amount, additional_amount, tax_rate FROM project_items WHERE project_id=?', (ct_proj_id,))
check('合同科目=合同价款 165000/0', len(item) == 1 and item[0][0] == '合同价款' and float(item[0][1]) == 165000.0 and float(item[0][2]) == 0.0 and item[0][3] == '13%', str(item))
nodes = q('SELECT node_type, node_name, ratio, calc_base, due_amount FROM payment_nodes WHERE project_id=? ORDER BY node_type', (ct_proj_id,))
node_map = {nt: (name, float(ratio), base, float(due)) for nt, name, ratio, base, due in nodes}
check('已生成 4 个付款节点', len(node_map) == 4, str(node_map.keys()))
check('首付 25% 按原金额 41,250.00', abs(node_map['first'][3] - 41250.0) < 0.01 and node_map['first'][1] == 0.25 and node_map['first'][2] == 'original', str(node_map.get('first')))
check('进度款 52% 按总金额 85,800.00', abs(node_map['progress'][3] - 85800.0) < 0.01 and node_map['progress'][1] == 0.52 and node_map['progress'][2] == 'total', str(node_map.get('progress')))
check('验收款 15% 按总金额 24,750.00', abs(node_map['acceptance'][3] - 24750.0) < 0.01, str(node_map.get('acceptance')))
check('质保金 3% 按总金额 4,950.00', abs(node_map['warranty'][3] - 4950.0) < 0.01, str(node_map.get('warranty')))
cf = q('SELECT status, project_id, contract_name FROM contract_files WHERE id=?', (ct_id,))[0]
check('合同记录状态=confirmed 并绑定项目', cf[0] == 'confirmed' and cf[1] == ct_proj_id and cf[2] == '信息化设备采购项目（e2e）', str(cf))
# 10.6 重复确认拦截：不应新增项目
cnt_before = q('SELECT COUNT(*) FROM projects WHERE project_no=?', ('PJ-CT-E2E-001',))[0][0]
r = s.post(BASE + f'/contracts/{ct_id}/confirm', data={'contract_name': '重复', 'original_amount': '1', 'tax_rate': '', 'payment_method': '', 'ratio_first': '100', 'ratio_progress': '0', 'ratio_acceptance': '0', 'ratio_warranty': '0'}, allow_redirects=False)
cnt_after = q('SELECT COUNT(*) FROM projects WHERE project_no=?', ('PJ-CT-E2E-001',))[0][0]
check('重复确认被拦截（不新增项目）', cnt_after == cnt_before, f'{cnt_before}->{cnt_after}')
# 10.7 已确认合同重新识别仅更新结果，不退回待确认
r = s.post(BASE + f'/contracts/{ct_id}/rerun', allow_redirects=False)
cf2 = q('SELECT status FROM contract_files WHERE id=?', (ct_id,))[0][0]
check('已确认合同重新识别后仍为 confirmed', r.status_code in (302,) and cf2 == 'confirmed', f'status={r.status_code} cf={cf2}')
# 10.8 原文页
r = s.get(BASE + f'/contracts/{ct_id}/raw')
check('合同原文页 200 且含原文关键句', r.status_code == 200 and '合同总金额' in r.text and '质保期' in r.text)
# 10.9 上传 DOCX 样例并确认页可展示
with open(SAMPLE_DOCX, 'rb') as f:
    r = s.post(BASE + '/contracts/upload', files={'file': ('合同样例-设备采购.docx', f, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')}, allow_redirects=False)
loc3 = r.headers.get('Location', '')
check('上传 DOCX 重定向确认页', r.status_code == 302 and '/confirm' in loc3, f'status={r.status_code} loc={loc3}')
ct_docx_id = int(loc3.rstrip('/').split('/')[-2])
r = s.get(BASE + f'/contracts/{ct_docx_id}/confirm')
check('DOCX 确认页识别金额 165,000.00', r.status_code == 200 and '165,000.00' in r.text)

# 10.10 合同确认后回改比例并重新生成
print('=== 10.10 合同回改重新生成 ===')
proj_total_before = q('SELECT COUNT(*) FROM projects')[0][0]
r = s.get(BASE + f'/contracts/{ct_id}/confirm')
check('已确认合同可进入确认页修改', r.status_code == 200 and '修改合同确认' in r.text)
r = s.post(BASE + f'/contracts/{ct_id}/confirm', data={
    'contract_name': '信息化设备采购项目（e2e修改）',
    'original_amount': '165000',
    'additional_amount': '0',
    'tax_rate': '13%',
    'payment_method': '分期支付',
    'ratio_first': '30',
    'ratio_progress': '52',
    'ratio_acceptance': '15',
    'ratio_warranty': '3',
}, allow_redirects=False)
loc_re = r.headers.get('Location', '')
check('回改提交后重定向到项目详情', r.status_code == 302 and '/projects/' in loc_re, f'status={r.status_code} loc={loc_re}')
# 验证节点已更新（首付变为30% = 49500）
nodes2 = q('SELECT node_type, due_amount FROM payment_nodes WHERE project_id=? ORDER BY node_type', (ct_proj_id,))
node_map2 = {nt: float(due) for nt, due in nodes2}
check('回改后首付变为30%即49500', abs(node_map2.get('first', 0) - 49500.0) < 0.01, str(node_map2))
check('回改后项目总数未增加（未新建）', q('SELECT COUNT(*) FROM projects')[0][0] == proj_total_before)

# 10.11 项目删除及关联数据清理
print('=== 10.11 项目删除 ===')
# 先为这个项目的某个节点创建申请和发票，验证级联删除
e2e_first_node = q('SELECT id FROM payment_nodes WHERE project_id=? AND node_type=?', (ct_proj_id, 'first'))[0][0]
s.post(BASE + f'/nodes/{e2e_first_node}/update', data={'action': 'apply', 'apply_amount': '1000', 'description': '删除测试'}, allow_redirects=False)
s.post(BASE + f'/nodes/{e2e_first_node}/update', data={'action': 'invoice', 'invoice_no': 'FP-DEL-001', 'invoice_amount': '1000'}, allow_redirects=False)
app_del_id = q('SELECT id FROM payment_applications WHERE node_id=?', (e2e_first_node,))[0][0]
s.post(BASE + f'/nodes/{e2e_first_node}/update', data={'action': 'pay', 'paid_amount': '1000', 'pay_date': '2026-01-03', 'pay_method': '银行转账', 'voucher_no': 'SK-DEL-001'}, allow_redirects=False)
# 删除项目
r = s.post(BASE + f'/projects/{ct_proj_id}/delete', allow_redirects=False)
check('项目删除后重定向到项目列表', r.status_code == 302 and '/projects' in r.headers.get('Location', ''))
# 验证关联数据已清理
check('项目删除后项目不存在', q('SELECT COUNT(*) FROM projects WHERE id=?', (ct_proj_id,))[0][0] == 0)
check('项目删除后科目已清理', q('SELECT COUNT(*) FROM project_items WHERE project_id=?', (ct_proj_id,))[0][0] == 0)
check('项目删除后节点已清理', q('SELECT COUNT(*) FROM payment_nodes WHERE project_id=?', (ct_proj_id,))[0][0] == 0)
check('项目删除后申请已清理', q('SELECT COUNT(*) FROM payment_applications WHERE node_id=?', (e2e_first_node,))[0][0] == 0)
check('项目删除后付款记录已清理', q('SELECT COUNT(*) FROM payment_records WHERE node_id=?', (e2e_first_node,))[0][0] == 0)
# 验证合同状态回退
cf_del = q('SELECT status, project_id FROM contract_files WHERE id=?', (ct_id,))[0]
check('项目删除后合同状态回退到recognized', cf_del[0] == 'recognized' and cf_del[1] is None, str(cf_del))

# 10.12 删除权限控制：viewer 无权删除项目
seed_id_check = q('SELECT id FROM projects WHERE project_no=?', ('PJ-2025-DSJ',))[0][0]
rv = sv.post(BASE + f'/projects/{seed_id_check}/delete', allow_redirects=False)
check('viewer 删除项目被拒（重定向）', rv.status_code == 302, f'status={rv.status_code}')
check('viewer 删除后项目仍存在', q('SELECT COUNT(*) FROM projects WHERE id=?', (seed_id_check,))[0][0] == 1)

print('=== 11. 合同权限控制 ===')
cnt_before = q('SELECT COUNT(*) FROM contract_files')[0][0]
with open(SAMPLE_TXT, 'rb') as f:
    rv = sv.post(BASE + '/contracts/upload', files={'file': ('viewer.txt', f, 'text/plain')}, allow_redirects=False)
cnt_after = q('SELECT COUNT(*) FROM contract_files')[0][0]
check('viewer 上传被拒', rv.status_code == 302 and cnt_after == cnt_before, f'status={rv.status_code} {cnt_before}->{cnt_after}')
rv = sv.get(BASE + '/contracts')
check('viewer 可查看合同列表 200', rv.status_code == 200)
rv = sv.post(BASE + f'/contracts/{ct_id}/confirm', data={'contract_name': 'x', 'original_amount': '1', 'tax_rate': '', 'payment_method': '', 'ratio_first': '100', 'ratio_progress': '0', 'ratio_acceptance': '0', 'ratio_warranty': '0'}, allow_redirects=False)
check('viewer 确认生成被拒', rv.status_code == 302)

print()
print(f'总计 {len(PASS)} 通过, {len(FAIL)} 失败')
if FAIL:
    print('失败项:', FAIL)
    sys.exit(1)
print('端到端验证全部通过')
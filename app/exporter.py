# -*- coding: utf-8 -*-
"""导出（批次 6a）：项目明细 / 报表汇总 → Excel；审计日志 → CSV。

口径约定：
- 金额统一 Decimal → 两位小数（``ROUND_HALF_UP``），Excel 单元格写数值并设
  ``#,##0.00`` 格式，可直接在 Excel 里求和/透视；
- 列与页面口径一致（项目详情 / 报表中心 / 操作日志），时间按 ``APP_TIMEZONE``
  由 UTC 转本地时展示，业务日期（验收/付款日等）保持日历日不换算；
- 文件名带筛选条件与导出时间（本地时），中文文件名由 Flask ``send_file`` 按
  RFC 5987 编码。

本模块只做“读数据 → 生成文件字节”，不 commit、不写业务表；导出操作的审计由
调用方（路由 / CLI）负责。
"""

import csv
import io
import re
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .money import CENT
from .timeutil import to_local, utcnow
from .validation import NODE_LABELS

__all__ = [
    'ITEM_HEADERS', 'NODE_HEADERS', 'APPLICATION_HEADERS', 'RECORD_HEADERS',
    'ACCEPTANCE_HEADERS', 'REPORT_HEADERS', 'AUDIT_HEADERS',
    'build_project_workbook', 'build_reports_workbook', 'audit_csv_bytes',
    'workbook_bytes', 'export_filename', 'money_value', 'pct_text',
]

#: 列定义（与页面表头保持一致；供页面/文档/测试引用）
ITEM_HEADERS = ('科目名称', '税率', '原金额', '增补金额', '科目总额')
NODE_HEADERS = ('科目', '节点名称', '节点类型', '比例', '计算基数',
                '应付金额', '已开票金额', '已支付金额', '未付金额',
                '状态', '应付日期', '触发日期')
APPLICATION_HEADERS = ('申请单号', '科目', '节点', '申请金额', '已开票金额',
                       '发票号', '说明', '状态', '申请人', '申请时间')
RECORD_HEADERS = ('付款日期', '科目', '节点', '付款金额', '付款方式',
                  '凭证号', '经办人', '关联申请单号')
ACCEPTANCE_HEADERS = ('验收日期', '验收人', '结论', '备注', '登记时间')
REPORT_HEADERS = ('项目', '编号', '状态', '应付金额', '已开票', '已付金额',
                  '未付金额', '付款进度(%)')
AUDIT_HEADERS = ('时间', '操作人', '角色', '动作', '对象类型', '对象ID',
                 '项目ID', '结果', 'IP', '浏览器', '变更前', '变更后')

MONEY_FORMAT = '#,##0.00'
PCT_FORMAT = '0.0'
_HEADER_FILL = PatternFill('solid', fgColor='1F4E79')
_HEADER_FONT = Font(bold=True, color='FFFFFF')
_HEADER_ALIGN = Alignment(horizontal='center', vertical='center', wrap_text=True)
_TOTAL_FONT = Font(bold=True)
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------- 基础转换 ----------

def money_value(value):
    """金额 → float（两位小数），保留 Decimal 舍入口径，供 Excel 数值列使用。"""
    try:
        return float(Decimal(value or 0).quantize(CENT))
    except Exception:
        return 0.0


def pct_text(ratio):
    """比例（0.30）→ 展示文本（30%）；保留非整数比例（如 30.5%）。"""
    try:
        value = (Decimal(ratio or 0) * 100).normalize()
    except Exception:
        value = Decimal('0')
    text = format(value, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text + '%'


def _safe_local(value):
    """UTC → 本地时；无应用上下文（纯库调用）时退回 UTC 显示，不抛错。"""
    try:
        return to_local(value)
    except Exception:
        return value


def _local_text(value, fmt='%Y-%m-%d %H:%M:%S'):
    local = _safe_local(value)
    return local.strftime(fmt) if local else ''


def _date_text(value):
    return value.isoformat() if value else ''


def _tz_label():
    try:
        from flask import current_app, has_app_context
        if has_app_context():
            return current_app.config.get('APP_TIMEZONE') or 'Asia/Shanghai'
    except Exception:
        pass
    return 'Asia/Shanghai'


def _display_len(text):
    """列宽估算：中文/全角按 2 个字符计。"""
    total = 0
    for ch in str(text):
        total += 2 if ord(ch) > 127 else 1
    return total


def _pct_value(progress):
    try:
        return float(Decimal(progress or 0))
    except Exception:
        return 0.0


# ---------- Excel 写入 ----------

def _add_sheet(workbook, title, headers, rows, money_cols=(), pct_cols=(),
               total_row=None):
    """新建工作表：表头样式、金额格式、列宽、冻结首行、自动筛选。"""
    ws = workbook.create_sheet(title=title)
    ws.append(list(headers))
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _HEADER_ALIGN

    body = list(rows)
    if total_row is not None:
        body.append(list(total_row))
    for row_index, row in enumerate(body, start=2):
        for col_index, value in enumerate(row, start=1):
            cell = ws.cell(row=row_index, column=col_index, value=value)
            cell.alignment = Alignment(vertical='center')
            if col_index in money_cols:
                cell.number_format = MONEY_FORMAT
            elif col_index in pct_cols:
                cell.number_format = PCT_FORMAT
        if total_row is not None and row_index == len(body) + 1:
            for cell in ws[row_index]:
                cell.font = _TOTAL_FONT

    widths = [_display_len(h) for h in headers]
    for row in body:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], _display_len(value))
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = min(max(8, width + 2), 46)

    ws.freeze_panes = 'A2'
    if ws.max_row > 1:
        ws.auto_filter.ref = 'A1:%s%d' % (get_column_letter(len(headers)), ws.max_row)
    return ws


def _new_workbook(title):
    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.properties.title = title
    workbook.properties.creator = '付款进度管理系统'
    return workbook


def workbook_bytes(workbook):
    """Workbook → 字节（供 send_file / 落盘）。"""
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ---------- 项目明细 ----------

def _project_info_rows(project):
    """项目信息：返回 [(字段, 值, 是否金额)]。"""
    deleted_text = _local_text(project.deleted_at) if project.deleted_at else '否'
    return [
        ('项目编号', project.project_no, False),
        ('项目名称', project.name, False),
        ('项目状态', project.status, False),
        ('是否已删除（回收站）', '是（%s）' % deleted_text if project.deleted_at else '否', False),
        ('合同总额（原金额+增补）', money_value(project.total_amount()), True),
        ('原金额合计', money_value(project.total_original()), True),
        ('增补金额合计', money_value(project.total_additional()), True),
        ('试运行日期', _date_text(project.trial_date) or '—', False),
        ('验收日期', _date_text(project.acceptance_date) or '—', False),
        ('质保到期', _date_text(project.warranty_due_date) or '—', False),
        ('备注', project.remark or '', False),
        ('创建时间', _local_text(project.created_at), False),
        ('更新时间', _local_text(project.updated_at), False),
        ('导出时间（%s）' % _tz_label(), _local_text(utcnow()), False),
    ]


def _item_rows(project):
    return [
        (item.item_name, item.tax_rate or '—',
         money_value(item.original_amount), money_value(item.additional_amount),
         money_value(item.total_amount))
        for item in project.items
    ]


def _ordered_nodes(project):
    order = {'first': 0, 'progress': 1, 'acceptance': 2, 'warranty': 3}
    items_order = {item.id: index for index, item in enumerate(project.items)}
    return sorted(project.nodes,
                  key=lambda n: (items_order.get(n.item_id, 999),
                                 order.get(n.node_type, 9), n.id))


def _node_rows(project):
    rows = []
    for node in _ordered_nodes(project):
        rows.append((
            node.item.item_name if node.item else '',
            node.node_name,
            NODE_LABELS.get(node.node_type, node.node_type),
            pct_text(node.ratio),
            '原金额' if node.calc_base == 'original' else '科目总额',
            money_value(node.due_amount),
            money_value(node.invoice_amount),
            money_value(node.paid_amount),
            money_value(node.remaining_amount),
            node.status,
            _date_text(node.due_date),
            _date_text(node.trigger_date),
        ))
    return rows


def _application_rows(project):
    rows = []
    for node in _ordered_nodes(project):
        for app in sorted(node.applications, key=lambda a: (a.apply_time or a.id, a.id)):
            rows.append((
                app.apply_no,
                node.item.item_name if node.item else '',
                node.node_name,
                money_value(app.apply_amount),
                money_value(app.invoice_amount),
                app.invoice_no or '',
                app.description or '',
                app.status,
                app.applicant_name or '',
                _local_text(app.apply_time),
            ))
    return rows


def _record_rows(project):
    rows = []
    for node in _ordered_nodes(project):
        for app in sorted(node.applications, key=lambda a: (a.apply_time or a.id, a.id)):
            for rec in sorted(app.records, key=lambda r: (r.pay_date or r.created_at, r.id)):
                rows.append((
                    _date_text(rec.pay_date),
                    node.item.item_name if node.item else '',
                    node.node_name,
                    money_value(rec.paid_amount),
                    rec.pay_method or '',
                    rec.voucher_no or '',
                    rec.operator_name or '',
                    app.apply_no if rec.application_id else '',
                ))
    return rows


def _acceptance_rows(project):
    return [
        (a.acceptance_date.isoformat() if a.acceptance_date else '',
         a.acceptor or '', a.conclusion or '', a.remark or '',
         _local_text(a.created_at))
        for a in sorted(project.acceptances,
                        key=lambda x: (x.acceptance_date or x.created_at, x.id))
    ]


def build_project_workbook(project):
    """项目明细工作簿：项目信息 / 费用科目 / 付款节点 / 付款申请 / 付款记录 / 验收记录。"""
    workbook = _new_workbook('项目明细 %s' % project.project_no)

    info = _project_info_rows(project)
    info_ws = _add_sheet(workbook, '项目信息', ('字段', '值'),
                         [(label, value) for label, value, _is_money in info])
    for row_index, (_label, _value, is_money) in enumerate(info, start=2):
        if is_money:
            info_ws.cell(row=row_index, column=2).number_format = MONEY_FORMAT

    items = _item_rows(project)
    item_total = ('合计', '', money_value(project.total_original()),
                  money_value(project.total_additional()),
                  money_value(project.total_amount()))
    _add_sheet(workbook, '费用科目', ITEM_HEADERS, items,
               money_cols=(3, 4, 5), total_row=item_total)

    nodes = _node_rows(project)
    node_total = ('合计', '', '', '', '',
                  money_value(sum(Decimal(n.due_amount or 0) for n in project.nodes)),
                  money_value(sum(Decimal(n.invoice_amount or 0) for n in project.nodes)),
                  money_value(sum(Decimal(n.paid_amount or 0) for n in project.nodes)),
                  money_value(sum(Decimal(n.remaining_amount) for n in project.nodes)),
                  '', '', '')
    _add_sheet(workbook, '付款节点', NODE_HEADERS, nodes,
               money_cols=(6, 7, 8, 9), total_row=node_total)

    _add_sheet(workbook, '付款申请', APPLICATION_HEADERS, _application_rows(project),
               money_cols=(4, 5))
    _add_sheet(workbook, '付款记录', RECORD_HEADERS, _record_rows(project),
               money_cols=(4,))
    _add_sheet(workbook, '验收记录', ACCEPTANCE_HEADERS, _acceptance_rows(project))
    return workbook


# ---------- 报表汇总 ----------

def build_reports_workbook(rows, totals, reminders=None):
    """报表汇总工作簿：付款台账总览 + 待办提醒。

    ``rows`` / ``totals`` 与 ``/reports`` 页同源（routes 传入）；``reminders`` 为
    [(kind, obj, days)]，kind='warranty' 或 'pending_pay'。
    """
    workbook = _new_workbook('付款台账报表汇总')
    body = [
        (row['project'].name, row['project'].project_no, row['project'].status,
         money_value(row['due']), money_value(row['invoice']),
         money_value(row['paid']), money_value(row['unpaid']),
         _pct_value(row['progress']))
        for row in rows
    ]
    # 合计列：应付 / 已开票 / 已付 / 未付；进度按已付/应付重算
    due_total = Decimal(str(totals.get('due', 0) or 0))
    paid_total = Decimal(str(totals.get('paid', 0) or 0))
    invoice_total = Decimal(str(totals.get('invoice', 0) or 0))
    progress_total = ((paid_total / due_total * Decimal('100')).quantize(Decimal('0.1'))
                      if due_total else Decimal('0'))
    total_row = ('合计', '', '',
                 money_value(due_total), money_value(invoice_total),
                 money_value(paid_total), money_value(totals.get('unpaid', 0)),
                 _pct_value(progress_total))
    _add_sheet(workbook, '付款台账总览', REPORT_HEADERS, body,
               money_cols=(4, 5, 6, 7), pct_cols=(8,), total_row=total_row)

    if reminders:
        reminder_rows = []
        for kind, obj, days in reminders:
            if kind == 'warranty':
                reminder_rows.append((
                    '质保到期', obj.name, _date_text(obj.warranty_due_date),
                    ('已到期 %d 天' % (-days)) if days is not None and days <= 0
                    else ('剩余 %d 天' % days if days is not None else ''),
                ))
            else:
                project = getattr(obj, 'project', None)
                reminder_rows.append((
                    '待付款', '%s %s' % (project.project_no if project else '',
                                        obj.node_name),
                    _date_text(obj.due_date),
                    '应付 ￥%s · %s' % (format(money_value(obj.due_amount), ',.2f'),
                                      obj.status),
                ))
        _add_sheet(workbook, '待办提醒', ('类型', '项目/节点', '日期', '说明'),
                   reminder_rows)
    return workbook


# ---------- 审计日志 CSV ----------

def audit_csv_bytes(logs):
    """审计日志 → CSV 字节（UTF-8 BOM，Excel 直接打开不乱码）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\r\n')
    writer.writerow(AUDIT_HEADERS)
    for log in logs:
        writer.writerow([
            _local_text(log.ts), log.actor_name or '',
            log.actor_role or '', log.action,
            log.object_type or '',
            '' if log.object_id is None else log.object_id,
            '' if log.project_id is None else log.project_id,
            log.result, log.ip or '', log.ua or '',
            log.before_json or '', log.after_json or '',
        ])
    return ('\ufeff' + buffer.getvalue()).encode('utf-8')


# ---------- 文件名 ----------

def _safe_part(text, limit=40):
    text = _UNSAFE_CHARS.sub('_', str(text or '')).strip().replace(' ', '_')
    return text[:limit] or '_'


def export_filename(prefix, parts, ext, ts=None):
    """生成「前缀_筛选条件_时间戳.ext」文件名（本地时、去掉非法字符）。"""
    local = _safe_local(ts or utcnow())
    stamp = local.strftime('%Y%m%d-%H%M%S') if local else '00000000-000000'
    chunks = [prefix] + [_safe_part(part) for part in (parts or []) if part]
    return '_'.join(chunks) + '_' + stamp + '.' + ext.lstrip('.')

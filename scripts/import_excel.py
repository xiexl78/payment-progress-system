# -*- coding: utf-8 -*-
"""Excel 导入（批次 6b）：解析 → 校验报告（dry-run） → 单事务写入 + 批次记录。

模板（按 README 种子表反推列结构，先 ``--write-template`` 生成空白模板）：

- 项目信息：项目编号 | 项目名称 | 状态 | 试运行日期 | 验收日期 | 备注
- 费用科目：项目编号 | 科目名称 | 税率 | 原金额 | 增补金额
- 付款节点（可空；空则按 30%/52%/15%/3% 自动生成）：
  项目编号 | 科目名称 | 节点类型 | 比例 | 应付金额 | 已开票金额 | 已付款金额 | 状态 | 应付日期 | 触发日期
- 付款申请（可空）：项目编号 | 科目名称 | 节点类型 | 申请单号 | 申请金额 | 已开票金额 | 发票号 | 说明 | 状态 | 申请人 | 申请时间
- 付款记录（可空）：项目编号 | 科目名称 | 节点类型 | 付款日期 | 付款金额 | 付款方式 | 凭证号 | 经办人 | 关联申请单号
- 验收记录（可空）：项目编号 | 验收日期 | 验收人 | 结论 | 备注

用法（工程根目录）：
    python scripts/import_excel.py --write-template instance/导入模板.xlsx
    python scripts/import_excel.py --file 旧台账.xlsx              # 默认 dry-run，只出报告
    python scripts/import_excel.py --file 旧台账.xlsx --commit     # 单事务写库 + 批次记录
    python scripts/import_excel.py --dry-run --input 旧台账.xlsx   # 兼容写法（等价默认）

导入格式：
- **多表模板**（推荐，``--write-template`` 生成）：6 个工作表，见上；
- **单表台账兜底**：若工作簿里没有「项目信息/费用科目」等模板表，则按第一张表解析
  「项目编号 + 项目名称 + 科目 + 税率 + 原金额 + 增补金额 + 状态」，一行一科目、
  项目编号重复出现；项目编号重复且名称冲突会报错。真实旧表拿到后逐步补别名。

安全约定：
- 默认只读 dry-run（有错误也正常退出并给出报告，rc=0），必须显式 ``--commit`` 才写库；
- 写入在**单个事务**内完成，任一条失败全部回滚；
- 项目编号 / 申请单号与现有库重复 → 阻断（本工具不做覆盖导入）；
- 批次记录写 ``import_batches``（SHA-256、计数、状态、报告路径），导入失败也尽力留痕；
- 真实 Excel 列结构未知：列名映射集中在 ``*_ALIASES`` / ``FLAT_ALIASES``，拿到用户真表后
  **只加别名**即可适配，不要改主流程。

退出码：0 成功（dry-run 完成 / 正式导入成功）；1 正式导入被阻断或失败；2 参数错误。
"""
import argparse
import hashlib
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from openpyxl import Workbook, load_workbook  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

from app import NODE_TEMPLATES, create_app  # noqa: E402
from app.audit import record as audit_record  # noqa: E402
from app.models import (AcceptanceRecord, ImportBatch, PaymentApplication,  # noqa: E402
                        PaymentNode, PaymentRecord, Project, ProjectItem, db)
from app.money import CENT, ZERO, mul_ratio, parse_money, sum_money  # noqa: E402
from app.timeutil import local_datetime_to_utc, utcnow  # noqa: E402
from app.validation import (PROJECT_STATUSES, TAX_RATES, ValidationError,  # noqa: E402
                            parse_date)

DEFAULT_DB = os.path.join(PROJECT_ROOT, 'instance', 'payment_progress.db')
FLOW_STATUS = ('未触发', '未开票已申请', '已申请', '已付款')
APP_STATUS = ('未开票已申请', '已申请', '已付款')

#: 列名映射：真实 Excel 来之后只在这里补别名
PROJECT_ALIASES = {
    'project_no': ('项目编号', '项目号', '项目编码', '编号'),
    'name': ('项目名称', '项目名', '名称'),
    'status': ('状态', '项目状态'),
    'trial_date': ('试运行日期', '试运行时间', '试运行'),
    'acceptance_date': ('验收日期', '验收时间', '验收'),
    'remark': ('备注', '说明'),
}
ITEM_ALIASES = {
    'project_no': ('项目编号', '项目号', '项目编码', '编号'),
    'item_name': ('科目名称', '科目', '费用项', '费用科目', '费用名称'),
    'tax_rate': ('税率',),
    'original_amount': ('原金额', '原合同金额', '合同金额', '原价'),
    'additional_amount': ('增补金额', '增补', '追加金额', '调增金额'),
}
NODE_ALIASES = {
    'project_no': ('项目编号', '项目号', '项目编码', '编号'),
    'item_name': ('科目名称', '科目', '费用项'),
    'node_type': ('节点类型', '节点', '付款节点', '款项类型'),
    'ratio': ('比例', '付款比例', '节点比例'),
    'due_amount': ('应付金额', '应付', '应付额', '合同应付款'),
    'invoice_amount': ('已开票金额', '开票金额', '已开票'),
    'paid_amount': ('已付款金额', '已付金额', '已付款', '已支付金额'),
    'status': ('状态', '节点状态'),
    'due_date': ('应付日期', '到期日期', '应付时间'),
    'trigger_date': ('触发日期', '实际触发日期', '触发时间'),
}
APPLICATION_ALIASES = {
    'project_no': ('项目编号', '项目号', '项目编码', '编号'),
    'item_name': ('科目名称', '科目', '费用项'),
    'node_type': ('节点类型', '节点', '付款节点', '款项类型'),
    'apply_no': ('申请单号', '申请编号', '付款申请号'),
    'apply_amount': ('申请金额', '申请额', '申请付款金额'),
    'invoice_amount': ('已开票金额', '开票金额'),
    'invoice_no': ('发票号', '发票号码'),
    'description': ('说明', '用途', '摘要'),
    'status': ('状态', '申请状态'),
    'applicant_name': ('申请人', '经办人'),
    'apply_time': ('申请时间', '申请日期'),
}
RECORD_ALIASES = {
    'project_no': ('项目编号', '项目号', '项目编码', '编号'),
    'item_name': ('科目名称', '科目', '费用项'),
    'node_type': ('节点类型', '节点', '付款节点', '款项类型'),
    'pay_date': ('付款日期', '付款时间', '实际付款日期'),
    'paid_amount': ('付款金额', '实付金额', '已付款金额'),
    'pay_method': ('付款方式', '支付方式'),
    'voucher_no': ('凭证号', '付款凭证号', '银行流水号'),
    'operator_name': ('经办人', '操作人'),
    'apply_no': ('关联申请单号', '申请单号', '关联申请号'),
}
ACCEPTANCE_ALIASES = {
    'project_no': ('项目编号', '项目号', '项目编码', '编号'),
    'acceptance_date': ('验收日期', '验收时间'),
    'acceptor': ('验收人', '验收人员'),
    'conclusion': ('结论', '验收结论'),
    'remark': ('备注', '说明'),
}

#: 工作表定义：(标准名, 内部 key, 别名, 必需列, 是否必需工作表)
SHEETS = [
    ('项目信息', 'project', PROJECT_ALIASES,
     (('project_no', '项目编号'), ('name', '项目名称')), True,
     ('项目', '项目清单')),
    ('费用科目', 'item', ITEM_ALIASES,
     (('project_no', '项目编号'), ('item_name', '科目名称')), True,
     ('科目', '费用明细', '科目明细')),
    ('付款节点', 'node', NODE_ALIASES,
     (('project_no', '项目编号'), ('item_name', '科目名称'),
      ('node_type', '节点类型'), ('ratio', '比例')), False,
     ('节点', '节点明细')),
    ('付款申请', 'application', APPLICATION_ALIASES,
     (('project_no', '项目编号'), ('item_name', '科目名称'),
      ('node_type', '节点类型'), ('apply_no', '申请单号'),
      ('apply_amount', '申请金额')), False,
     ('申请', '付款申请单')),
    ('付款记录', 'record', RECORD_ALIASES,
     (('project_no', '项目编号'), ('item_name', '科目名称'),
      ('node_type', '节点类型'), ('pay_date', '付款日期'),
      ('paid_amount', '付款金额')), False,
     ('付款', '付款明细')),
    ('验收记录', 'acceptance', ACCEPTANCE_ALIASES,
     (('project_no', '项目编号'), ('acceptance_date', '验收日期')), False,
     ('验收', '验收登记')),
]

#: 模板工作表及表头（写入模板用）
TEMPLATE_HEADERS = {
    '项目信息': ('项目编号', '项目名称', '状态', '试运行日期', '验收日期', '备注'),
    '费用科目': ('项目编号', '科目名称', '税率', '原金额', '增补金额'),
    '付款节点': ('项目编号', '科目名称', '节点类型', '比例', '应付金额',
                 '已开票金额', '已付款金额', '状态', '应付日期', '触发日期'),
    '付款申请': ('项目编号', '科目名称', '节点类型', '申请单号', '申请金额',
                 '已开票金额', '发票号', '说明', '状态', '申请人', '申请时间'),
    '付款记录': ('项目编号', '科目名称', '节点类型', '付款日期', '付款金额',
                 '付款方式', '凭证号', '经办人', '关联申请单号'),
    '验收记录': ('项目编号', '验收日期', '验收人', '结论', '备注'),
}

NODE_TYPE_ALIASES = {
    'first': ('首付款', '首付', '预付款', 'first'),
    'progress': ('进度款', '进度', 'progress'),
    'acceptance': ('验收款', '验收', 'acceptance'),
    'warranty': ('质保金', '质保', '尾款', 'warranty'),
}
NODE_TYPE_LABELS = {'first': '首付款', 'progress': '进度款',
                    'acceptance': '验收款', 'warranty': '质保金'}
NODE_TYPE_ORDER = {'first': 0, 'progress': 1, 'acceptance': 2, 'warranty': 3}


# ---------- 通用小工具 ----------

def _print(message=''):
    print(message)


def _err(message):
    print('[ERROR] %s' % message, file=sys.stderr)


def _prepare_streams():
    """输出被重定向（文件/管道）时强制 UTF-8，便于脚本和测试读取；控制台不变。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding='utf-8')
        except Exception:
            pass


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value):
    return '' if value is None else str(value).strip()


def _ctx(record):
    return '%s 第%d行' % (record.get('_sheet'), record.get('_row'))


def _norm_header(value):
    return _text(value).replace('\u3000', '').replace(' ', '').lower()


def _pct_text(ratio):
    value = (Decimal(ratio) * 100).normalize()
    text = format(value, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text + '%'


def _node_name(node_type, ratio):
    return NODE_TYPE_LABELS[node_type] + _pct_text(ratio)


def _cell_money(value, ctx, errors):
    if _blank(value):
        return None
    if isinstance(value, bool):
        errors.append('%s：金额不能是布尔值：%r' % (ctx, value))
        return None
    raw = value
    if isinstance(raw, float):
        # Excel 数字单元格是 double：先转字符串再进 Decimal，避免浮点误差从小数位混入
        raw = Decimal(repr(raw))
    try:
        parsed = parse_money(raw)
    except TypeError:
        errors.append('%s：金额格式不支持：%r' % (ctx, value))
        return None
    if parsed is None:
        errors.append('%s：金额无法解析：%r（仅数字，最多两位小数，不接受负数/科学计数）'
                      % (ctx, value))
    return parsed


def _cell_date(value, ctx, errors):
    if _blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return parse_date(_text(value), field=ctx)
    except ValidationError as exc:
        errors.append('%s：%s' % (ctx, exc.errors[0]))
        return None


def _cell_datetime(value, ctx, errors):
    if _blank(value):
        return None
    if isinstance(value, datetime):
        return local_datetime_to_utc(value)
    if isinstance(value, date):
        return local_datetime_to_utc(value)
    text = _text(value)
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d',
                '%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M', '%Y/%m/%d'):
        try:
            return local_datetime_to_utc(datetime.strptime(text, fmt))
        except ValueError:
            continue
    errors.append('%s：时间无法解析：%r（示例：2025-11-04 或 2025-11-04 10:00）'
                  % (ctx, value))
    return None


def _cell_ratio(value, ctx, errors, required=False):
    if _blank(value):
        if required:
            errors.append('%s：比例必填' % ctx)
        return None
    raw = value
    percent_hint = isinstance(raw, str) and raw.strip().endswith('%')
    if isinstance(raw, float):
        raw = Decimal(repr(raw))
    elif isinstance(raw, str):
        raw = raw.strip().rstrip('%').strip()
    try:
        value = parse_money(raw)
    except TypeError:
        errors.append('%s：比例格式不支持：%r' % (ctx, value))
        return None
    if value is None:
        errors.append('%s：比例无法解析：%r' % (ctx, value))
        return None
    ratio = value / Decimal('100') if (percent_hint or value > 1) else value
    if ratio <= 0 or ratio > 1:
        errors.append('%s：比例必须在 0~100%% 之间：%r' % (ctx, value))
        return None
    return ratio.quantize(Decimal('0.0001'))


def _cell_node_type(value, ctx, errors):
    text = _text(value)
    if not text:
        errors.append('%s：节点类型必填' % ctx)
        return None
    for node_type, aliases in NODE_TYPE_ALIASES.items():
        for alias in aliases:
            if text == alias or text.startswith(alias):
                return node_type
    errors.append('%s：节点类型无法识别：%r（可用：首付款/进度款/验收款/质保金）'
                  % (ctx, value))
    return None


def _cell_status(value, allowed, ctx, errors, default=None):
    text = _text(value)
    if not text:
        return default
    if text not in allowed:
        errors.append('%s：状态不合法：%r（可用：%s）'
                      % (ctx, value, '/'.join(allowed)))
        return default
    return text


def _find_sheet(workbook, name, aliases):
    for alias in (name,) + tuple(aliases or ()):
        if alias in workbook.sheetnames:
            return workbook[alias]
    return None


#: 单表台账格式（旧 Excel 常见：一行一个科目，项目编号重复出现）列映射
FLAT_ALIASES = {
    'project_no': PROJECT_ALIASES['project_no'],
    'name': PROJECT_ALIASES['name'],
    'status': PROJECT_ALIASES['status'],
    'item_name': ITEM_ALIASES['item_name'],
    'tax_rate': ITEM_ALIASES['tax_rate'],
    'original_amount': ITEM_ALIASES['original_amount'],
    'additional_amount': ITEM_ALIASES['additional_amount'],
}


def _read_flat(workbook, warnings):
    """单表台账兜底：一张表里同时有「项目编号+项目名称+科目+金额」列。"""
    parsed = {spec[1]: [] for spec in SHEETS}
    errors = []
    sheet = workbook.worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    header_index = None
    for index, row in enumerate(rows):
        if any(not _blank(cell) for cell in row):
            header_index = index
            break
    if header_index is None:
        return parsed, ['Excel 没有可读数据（空表）'], warnings

    header_map = {}
    unknown = []
    for column_index, header in enumerate(rows[header_index]):
        if _blank(header):
            continue
        norm = _norm_header(header)
        matched = None
        for key, key_aliases in FLAT_ALIASES.items():
            for alias in key_aliases:
                if norm == _norm_header(alias):
                    matched = key
                    break
            if matched:
                break
        if matched:
            header_map[column_index] = matched
        else:
            unknown.append(_text(header))
    if unknown:
        warnings.append('单表台账存在未识别列（已忽略）：%s' % '、'.join(unknown))
    present = set(header_map.values())
    if 'project_no' not in present or 'item_name' not in present:
        errors.append('无法识别列结构：需要「项目编号」和「科目名称/科目」两列；'
                      '复杂结构请使用 --write-template 生成的多表模板')
        return parsed, errors, warnings
    warnings.append('检测到单表台账格式：按「项目编号+科目」逐行导入，付款节点自动生成')

    projects_seen = {}
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        if all(_blank(cell) for cell in row):
            continue
        record = {'_row': offset, '_sheet': _text(sheet.title)}
        for column_index, key in header_map.items():
            record[key] = row[column_index] if column_index < len(row) else None
        project_no = _text(record.get('project_no'))
        name = _text(record.get('name'))
        if project_no:
            if project_no in projects_seen:
                if name and projects_seen[project_no] and name != projects_seen[project_no]:
                    errors.append('%s：项目编号重复且项目名称不一致（%s：%r ≠ %r）'
                                  % (_ctx(record), project_no, name,
                                     projects_seen[project_no]))
            else:
                projects_seen[project_no] = name
                parsed['project'].append({
                    '_row': offset, '_sheet': _text(sheet.title),
                    'project_no': project_no, 'name': name,
                    'status': record.get('status')})
        parsed['item'].append(record)
    return parsed, errors, warnings


# ---------- 模板 ----------

def write_template(path):
    """生成空白导入模板（含填写说明；数据表只留表头）。"""
    workbook = Workbook()
    workbook.remove(workbook.active)
    guide = workbook.create_sheet('填写说明')
    guide_lines = [
        '付款进度管理系统 · Excel 导入模板（批次 6b）',
        '',
        '1. 「项目信息」「费用科目」为必填工作表；项目编号是关联键，逐行必填。',
        '2. 日期格式：YYYY-MM-DD（如 2025-11-04）；时间格式：YYYY-MM-DD HH:MM。',
        '3. 金额只填数字（最多两位小数，不要带￥/千分位也可识别）；比例可填 30 或 30%（=0.30）。',
        '4. 「付款节点」可整表留空：系统按首付款30%（按原金额）、进度款52%、验收款15%、质保金3%（按总额）自动生成。',
        '5. 「付款申请」「付款记录」「验收记录」可留空；填了则通过 项目编号+科目名称+节点类型 关联到节点。',
        '6. 导入前先空跑一次（不加 --commit），按校验报告修正错误后再正式导入。',
        '7. 本工具不覆盖已存在的项目编号；历史数据请先用新编号或先清理旧数据。',
    ]
    for index, line in enumerate(guide_lines, start=1):
        guide.cell(row=index, column=1, value=line)
    guide.column_dimensions['A'].width = 110

    for sheet_name, headers in TEMPLATE_HEADERS.items():
        sheet = workbook.create_sheet(sheet_name)
        sheet.append(list(headers))
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='1F4E79')
            cell.alignment = Alignment(horizontal='center')
        for column_index, header in enumerate(headers, start=1):
            sheet.column_dimensions[get_column_letter(column_index)].width = \
                max(12, len(header) * 2 + 4)
        sheet.freeze_panes = 'A2'
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    workbook.save(path)
    return path


# ---------- 阶段 1：解析 ----------

def read_workbook(path):
    """读取 Excel → (parsed, errors, warnings)。

    ``parsed`` 为 ``{kind: [record, ...]}``；``record`` 含规范列名、``_row``、
    ``_sheet``。只做“读 + 单元格转换”，不做业务校验。
    """
    parsed = {spec[1]: [] for spec in SHEETS}
    errors, warnings = [], []
    try:
        workbook = load_workbook(path, data_only=True)
    except Exception as exc:
        raise SystemExit('无法读取 Excel 文件：%s（%s）' % (path, exc))

    # 没有模板的多表结构时，尝试单表台账格式（旧 Excel 迁移常见）
    if (_find_sheet(workbook, '项目信息', SHEETS[0][5]) is None
            and _find_sheet(workbook, '费用科目', SHEETS[1][5]) is None):
        return _read_flat(workbook, warnings)

    for name, kind, aliases, required, required_sheet, extra_names in SHEETS:
        sheet = _find_sheet(workbook, name, extra_names)
        if sheet is None:
            if required_sheet:
                errors.append('缺少必需工作表「%s」' % name)
            else:
                warnings.append('未提供工作表「%s」，按空表处理%s'
                                % (name, '（付款节点将自动生成）' if kind == 'node' else ''))
            continue

        rows = list(sheet.iter_rows(values_only=True))
        header_index = None
        for index, row in enumerate(rows):
            if any(not _blank(cell) for cell in row):
                header_index = index
                break
        if header_index is None:
            if required_sheet:
                errors.append('工作表「%s」是空表（缺少表头）' % name)
            continue

        headers = rows[header_index]
        header_map = {}
        unknown = []
        for column_index, header in enumerate(headers):
            if _blank(header):
                continue
            norm = _norm_header(header)
            matched = None
            for key, key_aliases in aliases.items():
                for alias in key_aliases:
                    if norm == _norm_header(alias):
                        matched = key
                        break
                if matched:
                    break
            if matched:
                header_map[column_index] = matched
            else:
                unknown.append(_text(header))
        if unknown:
            warnings.append('工作表「%s」存在未识别列（已忽略）：%s'
                            % (name, '、'.join(unknown)))

        present = set(header_map.values())
        missing = [label for key, label in required if key not in present]
        if missing:
            errors.append('工作表「%s」缺少必需列：%s' % (name, '、'.join(missing)))
            continue

        for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
            if all(_blank(cell) for cell in row):
                continue
            record = {'_row': offset, '_sheet': name}
            for column_index, key in header_map.items():
                record[key] = row[column_index] if column_index < len(row) else None
            parsed[kind].append(record)
    return parsed, errors, warnings


# ---------- 阶段 2：校验 / 生成导入计划 ----------

def _group_nodes(parsed):
    grouped = {}
    for record in parsed['node']:
        key = (_text(record.get('project_no')), _text(record.get('item_name')))
        grouped.setdefault(key, []).append(record)
    return grouped


def build_plan(parsed, errors, warnings):
    """校验并生成可写库的计划（不碰数据库）。"""
    plan = {'projects': [], 'items': [], 'nodes': [], 'applications': [],
            'records': [], 'acceptances': [], 'auto_nodes': 0}
    project_map = {}
    for record in parsed['project']:
        ctx = _ctx(record)
        project_no = _text(record.get('project_no'))
        name = _text(record.get('name'))
        if not project_no:
            errors.append('%s：项目编号必填' % ctx)
            continue
        if not name:
            errors.append('%s：项目名称必填' % ctx)
            continue
        if project_no in project_map:
            errors.append('%s：项目编号在表内重复：%s' % (ctx, project_no))
            continue
        status = _cell_status(record.get('status'), PROJECT_STATUSES, ctx, errors,
                              default='进行中')
        trial_date = _cell_date(record.get('trial_date'), ctx, errors)
        acceptance_date = _cell_date(record.get('acceptance_date'), ctx, errors)
        if trial_date and acceptance_date and acceptance_date < trial_date:
            errors.append('%s：验收日期不能早于试运行日期' % ctx)
        project = {'project_no': project_no, 'name': name, 'status': status,
                   'trial_date': trial_date, 'acceptance_date': acceptance_date,
                   'remark': _text(record.get('remark')) or None}
        project_map[project_no] = project
        plan['projects'].append(project)
    if not plan['projects']:
        errors.append('「项目信息」没有有效数据行')

    item_map = {}
    for record in parsed['item']:
        ctx = _ctx(record)
        project_no = _text(record.get('project_no'))
        item_name = _text(record.get('item_name'))
        if not project_no or not item_name:
            errors.append('%s：项目编号、科目名称必填' % ctx)
            continue
        if project_no not in project_map:
            errors.append('%s：项目编号在「项目信息」中不存在：%s'
                          % (ctx, project_no))
            continue
        if (project_no, item_name) in item_map:
            errors.append('%s：科目在表内重复：%s / %s'
                          % (ctx, project_no, item_name))
            continue
        tax_rate = _text(record.get('tax_rate'))
        if tax_rate and tax_rate not in TAX_RATES:
            warnings.append('%s：税率 %r 不在常用取值（13%%/9%%/6%%/3%%），按文本保留'
                            % (ctx, tax_rate))
        original = _cell_money(record.get('original_amount'), ctx, errors) or ZERO
        additional = _cell_money(record.get('additional_amount'), ctx, errors) or ZERO
        item = {'project_no': project_no, 'item_name': item_name,
                'tax_rate': tax_rate or None,
                'original_amount': original, 'additional_amount': additional,
                'nodes': []}
        item_map[(project_no, item_name)] = item
        plan['items'].append(item)
    if not plan['items']:
        errors.append('「费用科目」没有有效数据行')

    node_groups = _group_nodes(parsed)
    for project_no, item_name in node_groups:
        if (project_no, item_name) not in item_map:
            errors.append('「付款节点」中的科目在「费用科目」中不存在：%s / %s'
                          % (project_no, item_name))
    node_index = {}

    def add_auto_nodes(item):
        base_original = Decimal(item['original_amount'])
        base_total = Decimal(item['original_amount']) + Decimal(item['additional_amount'])
        for template in NODE_TEMPLATES:
            node_type = template['node_type']
            ratio = Decimal(str(template['ratio']))
            base = base_original if template['calc_base'] == 'original' else base_total
            node = {'project_no': item['project_no'], 'item_name': item['item_name'],
                    'node_type': node_type, 'ratio': ratio,
                    'due_amount': mul_ratio(base, ratio),
                    'invoice_amount': ZERO, 'paid_amount': ZERO,
                    'status': '未触发', 'due_date': None, 'trigger_date': None,
                    'source': 'auto'}
            item['nodes'].append(node)
            plan['nodes'].append(node)
            node_index[(item['project_no'], item['item_name'], node_type)] = node
            plan['auto_nodes'] += 1

    for item in plan['items']:
        key = (item['project_no'], item['item_name'])
        records = node_groups.get(key) or []
        if not records:
            add_auto_nodes(item)
            continue
        base_original = Decimal(item['original_amount'])
        base_total = Decimal(item['original_amount']) + Decimal(item['additional_amount'])
        seen = set()
        for record in records:
            ctx = _ctx(record)
            node_type = _cell_node_type(record.get('node_type'), ctx, errors)
            if node_type is None:
                continue
            if node_type in seen:
                errors.append('%s：同一科目节点重复：%s' % (ctx, NODE_TYPE_LABELS[node_type]))
                continue
            seen.add(node_type)
            ratio = _cell_ratio(record.get('ratio'), ctx, errors, required=True)
            if ratio is None:
                continue
            base = base_original if node_type == 'first' else base_total
            computed = mul_ratio(base, ratio)
            due = _cell_money(record.get('due_amount'), ctx, errors)
            if due is None:
                due = computed
            elif abs(due - computed) > CENT:
                warnings.append('%s：应付金额 %s 与「%s×%s」不一致（按表内金额导入）'
                                % (ctx, due, _pct_text(ratio), base))
            invoice = _cell_money(record.get('invoice_amount'), ctx, errors) or ZERO
            paid = _cell_money(record.get('paid_amount'), ctx, errors) or ZERO
            status = _cell_status(record.get('status'), FLOW_STATUS, ctx, errors,
                                  default='未触发')
            if paid > due:
                errors.append('%s：已付款金额 %s 超过应付金额 %s'
                              % (ctx, paid, due))
            if invoice > due:
                warnings.append('%s：已开票金额 %s 超过应付金额 %s'
                                % (ctx, invoice, due))
            if paid > 0 and status != '已付款':
                errors.append('%s：已付款金额大于 0 时状态必须为「已付款」' % ctx)
            if status == '已付款' and paid == 0:
                errors.append('%s：状态为「已付款」但已付款金额为 0' % ctx)
            node = {'project_no': item['project_no'], 'item_name': item['item_name'],
                    'node_type': node_type, 'ratio': ratio, 'due_amount': due,
                    'invoice_amount': invoice, 'paid_amount': paid,
                    'status': status,
                    'due_date': _cell_date(record.get('due_date'), ctx, errors),
                    'trigger_date': _cell_date(record.get('trigger_date'), ctx, errors),
                    'source': 'excel'}
            item['nodes'].append(node)
            plan['nodes'].append(node)
            node_index[key + (node_type,)] = node
        missing = [nt for nt in NODE_TYPE_ORDER if nt not in seen]
        if missing:
            errors.append('科目「%s / %s」缺少节点：%s（要么 4 个都给，要么整表留空自动生成）'
                          % (item['project_no'], item['item_name'],
                             '、'.join(NODE_TYPE_LABELS[nt] for nt in missing)))
        else:
            ratio_sum = sum((node['ratio'] for node in item['nodes']), Decimal('0'))
            if abs(ratio_sum - Decimal('1')) > Decimal('0.01'):
                errors.append('科目「%s / %s」节点比例合计 %s，应为 100%%'
                              % (item['project_no'], item['item_name'],
                                 _pct_text(ratio_sum)))
            total = base_total
            due_sum = sum_money(node['due_amount'] for node in item['nodes'])
            if abs(due_sum - total) > CENT:
                warnings.append('科目「%s / %s」节点应付合计 %s 与科目总额 %s 不一致'
                                '（首付款按原金额口径属正常，请人工确认）'
                                % (item['project_no'], item['item_name'], due_sum, total))

    application_map = {}
    for record in parsed['application']:
        ctx = _ctx(record)
        project_no = _text(record.get('project_no'))
        item_name = _text(record.get('item_name'))
        key = (project_no, item_name)
        if key not in item_map:
            errors.append('%s：科目在「费用科目」中不存在：%s / %s'
                          % (ctx, project_no, item_name))
            continue
        node_type = _cell_node_type(record.get('node_type'), ctx, errors)
        if node_type is None:
            continue
        node = node_index.get(key + (node_type,))
        if node is None:
            errors.append('%s：节点不存在（%s / %s），无法关联申请'
                          % (ctx, item_name, NODE_TYPE_LABELS[node_type]))
            continue
        apply_no = _text(record.get('apply_no'))
        if not apply_no:
            errors.append('%s：申请单号必填' % ctx)
            continue
        if apply_no in application_map:
            errors.append('%s：申请单号在表内重复：%s' % (ctx, apply_no))
            continue
        apply_amount = _cell_money(record.get('apply_amount'), ctx, errors)
        if apply_amount is None or apply_amount <= 0:
            errors.append('%s：申请金额必须大于 0' % ctx)
            continue
        if apply_amount > node['due_amount']:
            warnings.append('%s：申请金额 %s 超过节点应付 %s'
                            % (ctx, apply_amount, node['due_amount']))
        application = {'project_no': project_no, 'item_name': item_name,
                       'node_type': node_type, 'apply_no': apply_no,
                       'apply_amount': apply_amount,
                       'invoice_amount': _cell_money(record.get('invoice_amount'),
                                                     ctx, errors) or ZERO,
                       'invoice_no': _text(record.get('invoice_no')) or None,
                       'description': _text(record.get('description')) or None,
                       'status': _cell_status(record.get('status'), APP_STATUS,
                                              ctx, errors, default='未开票已申请'),
                       'applicant_name': _text(record.get('applicant_name')) or None,
                       'apply_time': _cell_datetime(record.get('apply_time'),
                                                    ctx, errors)}
        application_map[apply_no] = application
        plan['applications'].append(application)

    paid_by_node = {}
    for record in parsed['record']:
        ctx = _ctx(record)
        project_no = _text(record.get('project_no'))
        item_name = _text(record.get('item_name'))
        key = (project_no, item_name)
        if key not in item_map:
            errors.append('%s：科目在「费用科目」中不存在：%s / %s'
                          % (ctx, project_no, item_name))
            continue
        node_type = _cell_node_type(record.get('node_type'), ctx, errors)
        if node_type is None:
            continue
        node = node_index.get(key + (node_type,))
        if node is None:
            errors.append('%s：节点不存在（%s / %s），无法关联付款记录'
                          % (ctx, item_name, NODE_TYPE_LABELS[node_type]))
            continue
        pay_date = _cell_date(record.get('pay_date'), ctx, errors)
        if pay_date is None:
            errors.append('%s：付款日期必填' % ctx)
            continue
        paid_amount = _cell_money(record.get('paid_amount'), ctx, errors)
        if paid_amount is None or paid_amount <= 0:
            errors.append('%s：付款金额必须大于 0' % ctx)
            continue
        apply_no = _text(record.get('apply_no')) or None
        if apply_no and apply_no not in application_map:
            errors.append('%s：关联申请单号在「付款申请」中不存在：%s'
                          % (ctx, apply_no))
            continue
        plan['records'].append({
            'project_no': project_no, 'item_name': item_name,
            'node_type': node_type, 'pay_date': pay_date,
            'paid_amount': paid_amount,
            'pay_method': _text(record.get('pay_method')) or None,
            'voucher_no': _text(record.get('voucher_no')) or None,
            'operator_name': _text(record.get('operator_name')) or None,
            'apply_no': apply_no})
        paid_by_node[key + (node_type,)] = (paid_by_node.get(key + (node_type,), ZERO)
                                            + paid_amount)

    acceptance_dates = {}
    for record in parsed['acceptance']:
        ctx = _ctx(record)
        project_no = _text(record.get('project_no'))
        if project_no not in project_map:
            errors.append('%s：项目编号在「项目信息」中不存在：%s'
                          % (ctx, project_no))
            continue
        acceptance_date = _cell_date(record.get('acceptance_date'), ctx, errors)
        if acceptance_date is None:
            errors.append('%s：验收日期必填' % ctx)
            continue
        plan['acceptances'].append({
            'project_no': project_no, 'acceptance_date': acceptance_date,
            'acceptor': _text(record.get('acceptor')) or None,
            'conclusion': _text(record.get('conclusion')) or None,
            'remark': _text(record.get('remark')) or None})
        acceptance_dates[project_no] = max(
            acceptance_dates.get(project_no, acceptance_date), acceptance_date)

    # 项目验收日期：表内值与验收记录取较晚者（质保金到期按最晚验收推算）
    for project in plan['projects']:
        recorded = acceptance_dates.get(project['project_no'])
        if recorded and (project['acceptance_date'] is None
                         or recorded > project['acceptance_date']):
            project['acceptance_date'] = recorded

    # 节点与申请/付款记录对账；自动生成节点由记录反推状态与金额
    invoice_by_node = {}
    app_count_by_node = {}
    for application in plan['applications']:
        key = (application['project_no'], application['item_name'],
               application['node_type'])
        invoice_by_node[key] = invoice_by_node.get(key, ZERO) + application['invoice_amount']
        app_count_by_node[key] = app_count_by_node.get(key, 0) + 1
    for node in plan['nodes']:
        key = (node['project_no'], node['item_name'], node['node_type'])
        record_paid = paid_by_node.get(key, ZERO)
        record_invoice = invoice_by_node.get(key, ZERO)
        if node['source'] == 'excel':
            if record_paid != node['paid_amount']:
                errors.append('节点对账失败（%s / %s / %s）：节点已付款 %s，付款记录合计 %s'
                              % (node['project_no'], node['item_name'],
                                 NODE_TYPE_LABELS[node['node_type']],
                                 node['paid_amount'], record_paid))
            if node['invoice_amount'] and record_invoice and record_invoice != node['invoice_amount']:
                warnings.append('节点对账提示（%s / %s / %s）：节点已开票 %s，申请单发票合计 %s'
                                % (node['project_no'], node['item_name'],
                                   NODE_TYPE_LABELS[node['node_type']],
                                   node['invoice_amount'], record_invoice))
        else:
            if record_paid > node['due_amount']:
                errors.append('付款记录合计 %s 超过节点应付 %s（%s / %s / %s）'
                              % (record_paid, node['due_amount'],
                                 node['project_no'], node['item_name'],
                                 NODE_TYPE_LABELS[node['node_type']]))
            node['paid_amount'] = record_paid
            node['invoice_amount'] = record_invoice
            if record_paid > 0:
                node['status'] = '已付款'
            elif record_invoice > 0 or app_count_by_node.get(key):
                node['status'] = '已申请' if record_invoice > 0 else '未开票已申请'
            else:
                node['status'] = '未触发'
    return plan


def check_against_db(plan, errors):
    """只读检查与现有库的冲突：项目编号/申请单号重复（不支持覆盖导入）。"""
    project_nos = [project['project_no'] for project in plan['projects']]
    if project_nos:
        existing = {row[0] for row in
                    db.session.query(Project.project_no)
                    .filter(Project.project_no.in_(project_nos)).all()}
        for project_no in sorted(existing):
            errors.append('项目编号已存在于系统：%s（本工具不支持覆盖导入，请先清理）'
                          % project_no)
    apply_nos = [application['apply_no'] for application in plan['applications']]
    if apply_nos:
        existing = {row[0] for row in
                    db.session.query(PaymentApplication.apply_no)
                    .filter(PaymentApplication.apply_no.in_(apply_nos)).all()}
        for apply_no in sorted(existing):
            errors.append('申请单号已存在于系统：%s' % apply_no)


# ---------- 阶段 2：报告 ----------

def render_report(plan, parsed, errors, warnings, file_path, file_hash,
                  mode='dry-run', report_time=None):
    moment = report_time or datetime.now()
    lines = []
    lines.append('Excel 导入校验报告')
    lines.append('=' * 60)
    lines.append('生成时间：%s' % moment.strftime('%Y-%m-%d %H:%M:%S'))
    lines.append('源文件：%s' % os.path.abspath(file_path))
    lines.append('文件指纹：sha256:%s' % file_hash)
    lines.append('模式：%s' % ('正式导入' if mode == 'import' else '试运行（不写库）'))
    lines.append('')
    lines.append('一、解析结果')
    for name, kind, _aliases, _required, required_sheet, _extra in SHEETS:
        count = len(parsed.get(kind, []))
        suffix = '' if required_sheet else '（可空）'
        lines.append('  %s：%d 行%s' % (name, count, suffix))
    lines.append('  节点自动生成：%d 个' % plan.get('auto_nodes', 0))
    lines.append('  计划写入：项目 %d / 科目 %d / 节点 %d / 申请 %d / 付款 %d / 验收 %d'
                 % (len(plan['projects']), len(plan['items']), len(plan['nodes']),
                    len(plan['applications']), len(plan['records']),
                    len(plan['acceptances'])))
    lines.append('')
    lines.append('二、校验错误（%d）—— 有错误不能导入' % len(errors))
    if errors:
        for index, message in enumerate(errors, start=1):
            lines.append('  %d. %s' % (index, message))
    else:
        lines.append('  无')
    lines.append('')
    lines.append('三、警告（%d）—— 可继续，但建议核对' % len(warnings))
    if warnings:
        for index, message in enumerate(warnings, start=1):
            lines.append('  %d. %s' % (index, message))
    else:
        lines.append('  无')
    lines.append('')
    lines.append('四、结论')
    if errors:
        lines.append('  不能导入：请先修正上述 %d 条错误后重新运行。' % len(errors))
    else:
        lines.append('  校验通过：%s'
                     % ('开始写入（--commit），失败会整体回滚。' if mode == 'import'
                        else '未写库；确认无误后加 --commit 正式导入。'))
    return '\n'.join(lines)


# ---------- 阶段 3：写入 ----------

def build_app(db_path, data_dir, upgrade_schema=True):
    config = {
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + os.path.abspath(db_path),
        'SECRET_KEY': 'cli-import-local-only-%d' % os.getpid(),
        'DATA_DIR': data_dir,
        'UPLOAD_DIR': os.path.join(data_dir, 'uploads'),
        'CSRF_ENABLED': False,
        'LOGIN_THROTTLE_ENABLED': False,
        # dry-run 只读：不执行任何建表/补列 DDL
        'SKIP_SCHEMA_UPGRADE': not upgrade_schema,
    }
    return create_app(config)


#: 质保期天数：与系统口径一致（验收日期 + 365×3 天）
WARRANTY_DAYS = 365 * 3


def _fill_dates(project, nodes):
    """导入后补齐节点派生日期：只填空值，不覆盖 Excel 里显式给出的日期。

    （不走 ``sync_project_dates``：后者会冻结“有业务”节点，而导入期业务字段
    稍后才回填，会导致触发日期丢失。）
    """
    trial = project.trial_date
    acceptance = project.acceptance_date
    warranty_due = (acceptance + timedelta(days=WARRANTY_DAYS)) if acceptance else None
    project.warranty_due_date = warranty_due
    for node in nodes:
        if node.node_type == 'progress' and node.trigger_date is None:
            node.trigger_date = trial
        elif node.node_type == 'acceptance' and node.trigger_date is None:
            node.trigger_date = acceptance
        elif node.node_type == 'warranty' and node.due_date is None:
            node.due_date = warranty_due
    return nodes


def write_plan(plan, batch_meta):
    """单事务写入；任一条失败全部回滚，并尽力写一条失败批次记录。"""
    counters = {'projects_created': 0, 'items_created': 0, 'nodes_created': 0,
                'applications_created': 0, 'records_created': 0,
                'acceptances_created': 0}
    try:
        project_objs = {}
        for data in plan['projects']:
            project = Project(project_no=data['project_no'], name=data['name'],
                              status=data['status'], trial_date=data['trial_date'],
                              acceptance_date=data['acceptance_date'],
                              remark=data['remark'], created_by=None,
                              updated_by=None)
            db.session.add(project)
            db.session.flush()
            project_objs[data['project_no']] = project
            counters['projects_created'] += 1

        item_objs = {}
        for data in plan['items']:
            item = ProjectItem(project_id=project_objs[data['project_no']].id,
                               item_name=data['item_name'],
                               tax_rate=data['tax_rate'],
                               original_amount=data['original_amount'],
                               additional_amount=data['additional_amount'])
            db.session.add(item)
            db.session.flush()
            item_objs[(data['project_no'], data['item_name'])] = item
            counters['items_created'] += 1

        node_objs = {}
        for data in plan['nodes']:
            node = PaymentNode(
                project_id=project_objs[data['project_no']].id,
                item_id=item_objs[(data['project_no'], data['item_name'])].id,
                node_type=data['node_type'],
                node_name=_node_name(data['node_type'], data['ratio']),
                ratio=data['ratio'],
                calc_base='original' if data['node_type'] == 'first' else 'total',
                due_amount=data['due_amount'],
                due_date=data['due_date'], trigger_date=data['trigger_date'],
                invoice_amount=ZERO,
                paid_amount=ZERO, status='未触发')
            db.session.add(node)
            db.session.flush()
            node_objs[(data['project_no'], data['item_name'],
                       data['node_type'])] = node
            counters['nodes_created'] += 1

        # 先补齐派生日期（此时业务字段还是占位），再回填由申请/付款反推的业务数据
        business = {(data['project_no'], data['item_name'], data['node_type']):
                    (data['invoice_amount'], data['paid_amount'], data['status'])
                    for data in plan['nodes']}
        db.session.flush()
        for project_no, project in project_objs.items():
            project_nodes = [node for key, node in node_objs.items()
                             if key[0] == project_no]
            _fill_dates(project, project_nodes)
        for key, (invoice_amount, paid_amount, status) in business.items():
            node = node_objs[key]
            node.invoice_amount = invoice_amount
            node.paid_amount = paid_amount
            node.status = status

        application_objs = {}
        for data in plan['applications']:
            node = node_objs[(data['project_no'], data['item_name'],
                              data['node_type'])]
            application = PaymentApplication(
                node_id=node.id, apply_no=data['apply_no'],
                apply_amount=data['apply_amount'],
                invoice_amount=data['invoice_amount'],
                invoice_no=data['invoice_no'],
                description=data['description'], status=data['status'],
                applicant_id=None, applicant_name=data['applicant_name'],
                apply_time=data['apply_time'] or utcnow())
            db.session.add(application)
            db.session.flush()
            application_objs[data['apply_no']] = application
            counters['applications_created'] += 1

        for data in plan['records']:
            node = node_objs[(data['project_no'], data['item_name'],
                              data['node_type'])]
            application = application_objs.get(data['apply_no'])
            record = PaymentRecord(
                application_id=application.id if application else None,
                node_id=node.id, paid_amount=data['paid_amount'],
                pay_date=data['pay_date'], pay_method=data['pay_method'],
                voucher_no=data['voucher_no'], operator_id=None,
                operator_name=data['operator_name'],
                created_at=local_datetime_to_utc(data['pay_date']))
            db.session.add(record)
            counters['records_created'] += 1

        for data in plan['acceptances']:
            acceptance = AcceptanceRecord(
                project_id=project_objs[data['project_no']].id,
                acceptance_date=data['acceptance_date'],
                acceptor=data['acceptor'], conclusion=data['conclusion'],
                remark=data['remark'])
            db.session.add(acceptance)
            counters['acceptances_created'] += 1

        db.session.flush()
        batch = ImportBatch(
            ts=utcnow(), file_name=batch_meta['file_name'],
            file_hash=batch_meta['file_hash'], mode='import', status='success',
            operator='命令行导入', report_path=batch_meta.get('report_path'),
            **counters)
        db.session.add(batch)
        db.session.flush()
        audit_record('import.excel', 'import_batch', batch.id, None,
                     after={'file_name': batch_meta['file_name'],
                            'file_hash': batch_meta['file_hash'],
                            'counts': dict(counters),
                            'report_path': batch_meta.get('report_path')},
                     actor=(None, '命令行导入', 'system'), critical=False)
        db.session.commit()
        return batch.id, counters
    except Exception as exc:
        db.session.rollback()
        try:
            failed = ImportBatch(
                ts=utcnow(), file_name=batch_meta.get('file_name'),
                file_hash=batch_meta.get('file_hash'), mode='import',
                status='failed', operator='命令行导入',
                report_path=batch_meta.get('report_path'),
                error_msg=str(exc)[:2000])
            db.session.add(failed)
            db.session.commit()
        except Exception:
            db.session.rollback()
        raise


# ---------- CLI ----------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Excel 导入：解析 → 校验报告（dry-run） → 单事务写入 + 批次记录')
    parser.add_argument('--file', '--input', dest='file',
                        help='待导入的 .xlsx 文件')
    parser.add_argument('--db', default=DEFAULT_DB, help='SQLite 数据库路径')
    parser.add_argument('--commit', action='store_true',
                        help='正式写库（默认只做 dry-run 校验）')
    parser.add_argument('--dry-run', action='store_true',
                        help='只校验不写库（默认行为，保留兼容写法）')
    parser.add_argument('--report', help='校验报告输出路径（默认与源文件同目录）')
    parser.add_argument('--write-template', metavar='PATH',
                        help='生成空白导入模板后退出')
    return parser.parse_args(argv)


def main(argv=None):
    _prepare_streams()
    args = parse_args(argv)
    commit = bool(args.commit and not args.dry_run)
    if args.write_template:
        path = write_template(args.write_template)
        _print('[OK] 模板已生成：%s' % os.path.abspath(path))
        return 0
    if not args.file:
        _err('缺少 --file（或使用 --write-template 生成模板）')
        return 2
    file_path = os.path.abspath(args.file)
    if not os.path.isfile(file_path):
        _err('文件不存在：%s' % file_path)
        return 1
    if not file_path.lower().endswith(('.xlsx', '.xlsm')):
        _err('只支持 .xlsx / .xlsm 文件（旧 .xls 请先另存为 .xlsx）')
        return 1
    db_path = os.path.abspath(args.db)
    if not os.path.isfile(db_path):
        _err('数据库不存在：%s（请先 python scripts/init_db.py）' % db_path)
        return 1

    with open(file_path, 'rb') as handle:
        file_hash = hashlib.sha256(handle.read()).hexdigest()

    report_path = args.report
    if not report_path:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        report_path = os.path.join(os.path.dirname(file_path),
                                   'import_report_%s.txt' % stamp)
    report_path = os.path.abspath(report_path)

    app = build_app(db_path, os.path.dirname(db_path) or PROJECT_ROOT,
                    upgrade_schema=commit)
    with app.app_context():
        parsed, errors, warnings = read_workbook(file_path)
        plan = build_plan(parsed, errors, warnings)
        if not errors:
            check_against_db(plan, errors)
        report = render_report(plan, parsed, errors, warnings, file_path,
                               file_hash, mode='import' if commit else 'dry-run')
        try:
            os.makedirs(os.path.dirname(report_path) or '.', exist_ok=True)
            with open(report_path, 'w', encoding='utf-8') as handle:
                handle.write(report + '\n')
            _print(report)
            _print('\n[OK] 校验报告：%s' % report_path)
        except Exception as exc:
            _err('校验报告写入失败：%s' % exc)

        if errors:
            if not commit:
                # dry-run 的职责是“把问题讲清楚”：有错误也正常退出，报告可读
                _print('\n[DRY-RUN] 报告含 %d 条错误，未写库；修正后重新运行。' % len(errors))
                return 0
            _print('\n[FAIL] 存在 %d 条阻断错误，未写库。' % len(errors))
            return 1
        if not commit:
            _print('\n[DRY-RUN] 校验通过，未写库；确认无误后加 --commit 正式导入。')
            return 0
        batch_meta = {'file_name': os.path.basename(file_path),
                      'file_hash': file_hash, 'report_path': report_path}
        try:
            batch_id, counters = write_plan(plan, batch_meta)
        except Exception as exc:
            _err('导入失败，已整体回滚：%s' % exc)
            return 1
        _print('\n[OK] 导入成功（批次 #%d）：项目 %d / 科目 %d / 节点 %d / 申请 %d / 付款 %d / 验收 %d'
               % (batch_id, counters['projects_created'], counters['items_created'],
                  counters['nodes_created'], counters['applications_created'],
                  counters['records_created'], counters['acceptances_created']))
        return 0


if __name__ == '__main__':
    sys.exit(main())

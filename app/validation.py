# -*- coding: utf-8 -*-
"""服务端强校验（P0-3 落地）

把「金额 / 比例 / 日期 / 节点操作」的校验收敛到本模块一处，
路由层只负责取表单、调用校验、把 ``ValidationError.errors`` 回显给用户。

口径常量（含待业务确认项）集中在这里，改口径只改一处。
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from .money import CENT, MAX_MONEY, ZERO, allocate_tail, mul_ratio, parse_money, sum_money

__all__ = [
    'ValidationError',
    'PROJECT_STATUSES',
    'NODE_TYPES',
    'TAX_RATES',
    'RATIO_SUM_TOLERANCE',
    'CONTRACT_TOTAL_STRICT',
    'ALLOW_ZERO_RATIO',
    'ContractSubmission',
    'parse_amount',
    'parse_ratio_pct',
    'parse_date',
    'validate_project_dates',
    'validate_project_item_rows',
    'validate_project_ratios',
    'validate_contract_submission',
    'validate_node_action',
    'validate_contract_change_items',
    'node_base_amount',
]

#: 项目状态白名单（P2-12：禁止任意字符串写入）
PROJECT_STATUSES = ('进行中', '已完成', '已归档')
#: 付款节点类型（与 NODE_TEMPLATES 对齐）
NODE_TYPES = ('first', 'progress', 'acceptance', 'warranty')
NODE_LABELS = {'first': '首付款', 'progress': '进度款',
               'acceptance': '验收款', 'warranty': '质保金'}
#: 税率白名单（与页面下拉一致）
TAX_RATES = ('13%', '9%', '6%', '3%', '0%', '')
#: 比例合计容差：±0.01 个百分点
RATIO_SUM_TOLERANCE = Decimal('0.01')
#: 口径（批次 8 统一）：4 节点全部按「原金额+增补金额」计算，节点应付合计恒等于合同总额，
#: 逐项四舍五入产生的尾差归质保金（``TAIL_ORDER``），因此不需要“只警告不阻断”的宽松开关。
CONTRACT_TOTAL_STRICT = True
#: TODO(业务确认)：是否允许某类节点比例为 0（如无质保金合同）。
#: 当前按「0 < 比例 ≤ 100」执行；业务确认允许 0 后改为 True。
ALLOW_ZERO_RATIO = True   # CR-04：比例允许 0%（例如某节点本期不付款）
#: 尾差归集顺序：质保金优先
TAIL_ORDER = ('warranty', 'acceptance', 'progress', 'first')
#: 日期合法范围
MIN_DATE = date(1900, 1, 1)
MAX_DATE = date(2100, 12, 31)
#: 文本长度上限（与模型列定义对齐）
MAX_TEXT = {'contract_name': 256, 'project_no': 64, 'tax_rate': 32,
            'payment_method': 256, 'item_name': 128}


class ValidationError(ValueError):
    """校验失败：``errors`` 为可直接展示给用户的中文消息列表。"""

    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__('；'.join(self.errors))


def _is_blank(raw):
    return raw is None or (isinstance(raw, str) and not raw.strip())


def _err(field, message):
    return ValidationError('%s：%s' % (field, message))


def parse_amount(raw, *, field, required=True, allow_zero=True):
    """解析页面提交的金额字符串，返回两位小数 ``Decimal``。

    拒绝：空（required 时）、nan/inf/科学计数、非法字符、多于 2 位小数、
    负数、超过 NUMERIC(14,2) 上限。
    """
    if _is_blank(raw):
        if required:
            raise _err(field, '必填')
        return ZERO
    if isinstance(raw, str) and raw.strip().endswith('%'):
        raise _err(field, '不能带百分号')
    try:
        value = parse_money(raw, allow_negative=False)
    except TypeError:
        raise _err(field, '格式错误（禁止使用浮点数）')
    if value is None:
        raise _err(field, '请输入有效金额（仅数字，最多两位小数；不接受 nan/inf/科学计数/负数）')
    if not allow_zero and value == 0:
        raise _err(field, '必须大于 0')
    if value > MAX_MONEY:
        raise _err(field, '超出金额上限 %s' % MAX_MONEY)
    return value


def parse_ratio_pct(raw, *, field):
    """解析比例（百分数，如 ``'30.00'`` 表示 30%），返回 ``Decimal`` 百分数。

    规则：0 < 比例 ≤ 100（比例为 0 时按 ``ALLOW_ZERO_RATIO`` 决定）。
    """
    if _is_blank(raw):
        raise _err(field, '必填')
    text = str(raw).strip()
    if text.endswith('%'):
        text = text[:-1].strip()
    try:
        value = parse_money(text, allow_negative=False)
    except TypeError:
        raise _err(field, '格式错误（禁止使用浮点数）')
    if value is None:
        raise _err(field, '请输入有效比例（0~100，最多两位小数）')
    if value > Decimal('100'):
        raise _err(field, '比例必须在 0~100 之间')
    if value == 0 and not ALLOW_ZERO_RATIO:
        raise _err(field, '比例必须大于 0（如需 0% 请先与业务确认）')
    return value


def parse_date(raw, *, field, required=False):
    """解析 ``YYYY-MM-DD`` 日期；非法抛 ``ValidationError``。"""
    if _is_blank(raw):
        if required:
            raise _err(field, '必填')
        return None
    text = str(raw).strip()
    try:
        value = datetime.strptime(text, '%Y-%m-%d').date()
    except ValueError:
        raise _err(field, '格式应为 YYYY-MM-DD')
    if not (MIN_DATE <= value <= MAX_DATE):
        raise _err(field, '日期超出合理范围（1900~2100 年）')
    return value


def validate_project_dates(trial_date, acceptance_date):
    """试运行/验收日期先后校验。"""
    if trial_date and acceptance_date and acceptance_date < trial_date:
        raise ValidationError('验收日期不能早于试运行日期')


def validate_text(raw, *, field, required=False, max_len=None, allowed=None):
    text = '' if raw is None else str(raw).strip()
    if required and not text:
        raise _err(field, '必填')
    limit = max_len or MAX_TEXT.get(field)
    if limit and len(text) > limit:
        raise _err(field, '长度不能超过 %d 个字符' % limit)
    if allowed is not None and text not in allowed:
        raise _err(field, '取值不合法')
    return text


def validate_project_ratios(form):
    """校验新建项目的 4 个节点比例（百分数，必填、无默认值）。

    Returns: ``{node_type: Decimal(百分数)}``；Raises ``ValidationError``。
    """
    errors = []
    ratios = {}
    form_keys = {'first': 'ratio_first', 'progress': 'ratio_progress',
                 'acceptance': 'ratio_acceptance', 'warranty': 'ratio_warranty'}
    for nt in NODE_TYPES:
        raw = form.get(form_keys[nt])
        try:
            ratios[nt] = parse_ratio_pct(raw, field=NODE_LABELS[nt] + '比例')
        except ValidationError as exc:
            errors.extend(exc.errors)
    if errors:
        raise ValidationError(errors)
    total = sum(ratios.values(), Decimal('0'))
    if abs(total - Decimal('100')) > RATIO_SUM_TOLERANCE:
        errors.append('4 个节点比例合计应为 100%%（容差 ±%.2f%%），当前为 %s'
                      % (RATIO_SUM_TOLERANCE, _plain_pct(total)))
    if errors:
        raise ValidationError(errors)
    return ratios


def validate_project_item_rows(form):
    """校验「新建项目」的科目行（多科目动态表单）。

    CR-04：新建项目不再有增补金额字段，``additional_amount`` 恒为 0；
    比例不再由系统默认值给出，由 :func:`validate_project_ratios` 单独校验。

    Returns: ``[{'item_name','tax_rate','original_amount','additional_amount'}]``
    Raises: ``ValidationError``（收集所有错误一次返回）。
    """
    names = form.getlist('item_name')
    taxes = form.getlist('tax_rate')
    originals = form.getlist('original_amount')
    errors = []
    rows = []
    for idx in range(len(names)):
        name = (names[idx] or '').strip()
        if not name:
            # 与旧逻辑一致：空行跳过（但至少要有 1 行有效科目）
            continue
        row_no = '第 %d 个科目' % (len(rows) + 1)
        tax = (taxes[idx] if idx < len(taxes) else '') or ''
        try:
            tax = validate_text(tax, field='tax_rate', max_len=32, allowed=TAX_RATES)
        except ValidationError as e:
            errors.extend(e.errors)
        try:
            original = parse_amount(originals[idx] if idx < len(originals) else None,
                                    field=row_no + '原金额', allow_zero=True)
        except ValidationError as e:
            errors.extend(e.errors)
            original = ZERO
        if len(name) > MAX_TEXT['item_name']:
            errors.append('%s名称：长度不能超过 %d 个字符' % (row_no, MAX_TEXT['item_name']))
        rows.append({'item_name': name, 'tax_rate': tax,
                     'original_amount': original, 'additional_amount': ZERO})
    if not rows and not errors:
        errors.append('至少需要添加一个费用科目')
    if errors:
        raise ValidationError(errors)
    return rows


@dataclass
class ContractSubmission:
    """合同确认/回改的规范化提交值（全部 Decimal 或纯文本）。"""
    contract_name: str
    project_no: str
    original_amount: Decimal
    additional_amount: Decimal
    tax_rate: str
    payment_method: str
    ratios_pct: dict
    node_amounts: dict
    expected_total: Decimal
    contract_total: Decimal
    tail: Decimal
    tail_node: str
    warnings: list = field(default_factory=list)


def _base_for_node(node_type, original, additional):
    """统一口径（批次 8）：所有节点都按「原金额+增补金额」计算。"""
    return original + additional


def node_base_amount(node, original_amount, additional_amount):
    """按节点 calc_base 返回计算基数（Decimal）。"""
    if node.calc_base == 'original':
        return original_amount
    return original_amount + additional_amount


def build_contract_numbers(original_amount, additional_amount, ratios_pct):
    """按口径计算 4 个节点应付并做尾差归集。

    Returns: ``ContractSubmission`` 中金额部分（不含文本字段）。
    Raises: ``ValidationError``。
    """
    # 比例合计 100% ± 0.01 个百分点
    total_pct = sum((ratios_pct[nt] for nt in NODE_TYPES), Decimal('0'))
    if abs(total_pct - Decimal('100')) > RATIO_SUM_TOLERANCE:
        raise ValidationError(
            '比例合计应为 100%%（容差 ±%.2f%%），当前为 %s%%'
            % (RATIO_SUM_TOLERANCE, _plain_pct(total_pct)))

    contract_total = (original_amount + additional_amount).quantize(CENT)
    if contract_total <= 0:
        raise ValidationError('合同总额（原金额+增补金额）必须大于 0')
    base_map = {nt: _base_for_node(nt, original_amount, additional_amount)
                for nt in NODE_TYPES}
    raw_amounts = {nt: base_map[nt] * ratios_pct[nt] / Decimal('100')
                   for nt in NODE_TYPES}
    node_amounts = {nt: _quantize(raw_amounts[nt]) for nt in NODE_TYPES}
    expected_total = contract_total
    tail = (expected_total - sum(node_amounts.values(), ZERO)).quantize(CENT)
    tail_node = ''
    if tail != 0:
        before = dict(node_amounts)
        try:
            node_amounts = allocate_tail(node_amounts, expected_total, order=TAIL_ORDER)
        except ValueError as e:
            raise ValidationError('尾差归集失败：%s' % e)
        # 记录尾差实际落到了哪个节点（便于页面说明）
        for nt in TAIL_ORDER:
            if nt in node_amounts and node_amounts[nt] != before.get(nt):
                tail_node = nt
                break

    warnings = []
    payable = sum(node_amounts.values(), ZERO)
    if payable != contract_total:
        # 防御性兜底：4 节点合计必须恒等于合同总额（尾差归质保金）
        raise ValidationError('节点应付合计 %s 与合同总额 %s 不一致'
                              % (_money(payable), _money(contract_total)))
    return {
        'original_amount': original_amount,
        'additional_amount': additional_amount,
        'ratios_pct': dict(ratios_pct),
        'node_amounts': node_amounts,
        'expected_total': expected_total,
        'contract_total': contract_total,
        'tail': tail,
        'tail_node': tail_node,
        'warnings': warnings,
    }


def validate_contract_submission(form, *, additional_amount=None):
    """校验合同确认/回改表单，返回规范化 ``ContractSubmission``。

    CR-04：页面不再有增补输入。新建项目 ``additional_amount=0``；回改已存在
    项目时由调用方传入当前增补合计并原样保留（不因表单缺失被清零）。
    比例必填，未识别到就必须人工填写，系统不回退任何默认值。
    """
    errors = []

    def _try(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValidationError as e:
            errors.extend(e.errors)
            return None

    contract_name = _try(validate_text, form.get('contract_name'),
                         field='contract_name', required=True, max_len=256) or ''
    project_no = _try(validate_text, form.get('project_no'),
                      field='project_no', required=False, max_len=64) or ''
    original = _try(parse_amount, form.get('original_amount'),
                    field='原金额', allow_zero=False)
    additional = (ZERO if additional_amount is None
                  else Decimal(additional_amount).quantize(CENT))
    tax_rate = _try(validate_text, form.get('tax_rate'),
                    field='tax_rate', required=False, max_len=32, allowed=TAX_RATES) or ''
    payment_method = _try(validate_text, form.get('payment_method'),
                          field='payment_method', required=False,
                          max_len=256) or ''
    ratios = {}
    for nt in NODE_TYPES:
        ratios[nt] = _try(parse_ratio_pct, form.get('ratio_' + nt),
                          field=NODE_LABELS[nt] + '比例')

    if errors:
        raise ValidationError(errors)

    numbers = build_contract_numbers(original, additional, ratios)
    return ContractSubmission(
        contract_name=contract_name, project_no=project_no,
        original_amount=numbers['original_amount'],
        additional_amount=numbers['additional_amount'],
        tax_rate=tax_rate, payment_method=payment_method,
        ratios_pct=numbers['ratios_pct'], node_amounts=numbers['node_amounts'],
        expected_total=numbers['expected_total'],
        contract_total=numbers['contract_total'],
        tail=numbers['tail'], tail_node=numbers['tail_node'],
        warnings=numbers['warnings'],
    )


def validate_node_action(node, action, form):
    """校验节点申请 / 开票 / 付款（CR-04：三本独立账，互不为前置）。

    每个动作只校验自己这一维：金额 > 0、节点应付 > 0、该维累计 + 本次 ≤ 应付。
    **不再读取 ``status``**，也不检查其它维度是否发生。
    """
    due = Decimal(node.due_amount or 0)
    if due <= 0:
        raise ValidationError('该节点应付为 0，无法登记任何金额')
    if action == 'apply':
        amount = parse_amount(form.get('apply_amount'), field='申请金额', allow_zero=False)
        applied = Decimal(getattr(node, 'applied_amount', 0) or 0)
        if applied + amount > due:
            raise ValidationError('累计申请金额 %s 将超过节点应付 %s'
                                  % (_money(applied + amount), _money(due)))
        return {'amount': amount}
    if action == 'invoice':
        amount = parse_amount(form.get('invoice_amount'), field='开票金额', allow_zero=False)
        invoice = Decimal(node.invoice_amount or 0)
        if invoice + amount > due:
            raise ValidationError('累计开票金额 %s 将超过节点应付 %s'
                                  % (_money(invoice + amount), _money(due)))
        return {'amount': amount}
    if action == 'pay':
        amount = parse_amount(form.get('paid_amount'), field='付款金额', allow_zero=False)
        paid = Decimal(node.paid_amount or 0)
        if paid + amount > due:
            raise ValidationError('累计付款金额 %s 将超过节点应付 %s'
                                  % (_money(paid + amount), _money(due)))
        return {'amount': amount}
    raise ValidationError('未知操作：%s' % action)


def _quantize(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def validate_contract_change_items(project, form):
    """校验合同变更「逐科目变更后总额」输入（CR-04 §6.2）。

    Returns: ``{item_id: Decimal(new_total)}``；Raises ``ValidationError``。
    """
    errors = []
    totals = {}
    items = list(project.items)
    if not items:
        raise ValidationError('项目没有费用科目，无法做合同变更')
    all_same = True
    for item in items:
        field = 'item_total_%d' % item.id
        label = '科目「%s」变更后总额' % item.item_name
        raw = form.get(field)
        if raw is None or not str(raw).strip():
            errors.append('%s：必填' % label)
            continue
        try:
            value = parse_amount(raw, field=label, allow_zero=True)
        except ValidationError as exc:
            errors.extend(exc.errors)
            continue
        current = (Decimal(item.original_amount or 0)
                   + Decimal(item.additional_amount or 0)).quantize(CENT)
        if value != current:
            all_same = False
        frozen = [n for n in item.nodes
                  if Decimal(n.paid_amount or 0) > 0 or n.status == '已付款']
        frozen_due = sum((Decimal(n.due_amount or 0) for n in frozen), ZERO).quantize(CENT)
        if value < frozen_due:
            errors.append('%s %s 小于已有付款节点应付合计 %s，无法在冻结已有付款节点的前提下变更'
                          % (label, _money(value), _money(frozen_due)))
        unfrozen = [n for n in item.nodes if n not in frozen]
        if not unfrozen and value != frozen_due:
            errors.append('%s 的科目已无未付节点，变更后总额必须等于已付款节点应付合计 %s'
                          % (label, _money(frozen_due)))
        for node in item.nodes:
            if node.status == '超付待处理':
                errors.append('科目「%s」存在历史超付节点「%s」，请先处理后再做合同变更'
                              % (item.item_name, node.node_name))
                break
        totals[item.id] = value
    if not errors and all_same:
        errors.append('所有科目金额均未变化，无需提交合同变更')
    if sum(totals.values(), ZERO) <= 0:
        errors.append('变更后合同总额必须大于 0')
    if errors:
        raise ValidationError(errors)
    return totals


def _money(value):
    from .money import fmt_money
    return fmt_money(value, symbol=True)


def _plain_pct(value):
    text = format(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f')
    return text.rstrip('0').rstrip('.') + '%'


# ---------- 密码策略（批次 4b） ----------

#: 系统已知默认口令：明文禁止继续使用
DEFAULT_PASSWORDS = frozenset({
    'admin123', '123456', '12345678', 'password', 'admin', 'qwerty123',
})
#: 种子示例账号 → 默认口令（登录时用于识别"仍在使用默认口令"）
DEFAULT_ACCOUNT_PASSWORDS = {
    'admin': 'admin123',
    'jbr': '123456',
    'viewer': '123456',
}


def validate_password(raw, *, username=None, field='密码', min_length=8):
    """校验新密码：非空、长度 ≥ min_length、不得等于用户名、不得为已知默认口令。"""
    if _is_blank(raw):
        raise _err(field, '必填')
    password = str(raw)
    if len(password) < int(min_length):
        raise _err(field, '长度至少 %d 位' % int(min_length))
    if username and password.strip().lower() == str(username).strip().lower():
        raise _err(field, '不能与用户名相同')
    if password.strip() in DEFAULT_PASSWORDS:
        raise _err(field, '不能使用系统默认口令，请设置更复杂的密码')
    return password

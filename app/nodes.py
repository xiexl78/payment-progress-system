# -*- coding: utf-8 -*-
"""付款节点三维口径（CR-04）

申请 / 开票 / 付款三本账完全并列，互不为前置：

- ``derive_status``：单一只读派生函数，``payment_nodes.status`` 降为兼容缓存；
- ``node_name``：节点名称 = 类型名 + 实际比例，系统中不再有默认比例；
- ``STATUS_LABELS``：页面展示用中文标签；
- ``three_dim``：三维进度（已发生 / 应付）供页面、报表、导出复用。

约束：任何业务校验都不得再读取 ``status``，它只用于展示与查询。
"""

from decimal import Decimal, ROUND_HALF_UP

from .money import CENT, ZERO

__all__ = ['FLOW_STATUS', 'STATUS_LABELS', 'NODE_TYPE_LABELS',
           'derive_status', 'node_name', 'three_dim', 'status_label',
           'node_progress_label', 'payment_state', 'invoice_state']

#: 兼容缓存列的全部取值（顺序即页面/筛选顺序）
FLOW_STATUS = ['未触发', '未开票已申请', '已开票未申请', '已申请',
               '部分付款', '已付款', '超付待处理']

#: 状态 → 页面标签（不改写缓存列取值）
STATUS_LABELS = {
    '未触发': '未开始',
    '未开票已申请': '进行中',
    '已开票未申请': '进行中',
    '已申请': '进行中',
    '部分付款': '进行中',
    '已付款': '已完成',
    '超付待处理': '超付待处理',
}

NODE_TYPE_LABELS = {'first': '首付款', 'progress': '进度款',
                    'acceptance': '验收款', 'warranty': '质保金'}


def derive_status(due, applied, invoice, paid):
    """按三维已发生额派生兼容状态（只用于展示/筛选，不参与校验）。"""
    due = Decimal(due or 0)
    applied = Decimal(applied or 0)
    invoice = Decimal(invoice or 0)
    paid = Decimal(paid or 0)
    if due <= 0:
        return '未触发'
    if paid >= due:
        return '已付款'
    if paid > 0:
        return '部分付款'
    if applied > 0 and invoice > 0:
        return '已申请'
    if applied > 0:
        return '未开票已申请'
    if invoice > 0:
        return '已开票未申请'
    return '未触发'


def _pct_text(ratio):
    """0.2 → '20%'，0.205 → '20.5%'，0.035 → '3.5%'。"""
    pct = (Decimal(ratio or 0) * Decimal('100')).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP)
    text = format(pct, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text + '%'


def node_name(node_type, ratio):
    """节点名称 = 类型名 + 实际比例（比例缺失时只显示类型名）。"""
    label = NODE_TYPE_LABELS.get(node_type, node_type)
    if ratio is None:
        return label
    return label + _pct_text(ratio)


def status_label(status):
    return STATUS_LABELS.get(status, status or '')


def node_progress_label(dimension, due, amount):
    """单个维度的展示标签：未申请/部分申请 ¥A/已足额申请 ¥A 等。"""
    labels = {'apply': '申请', 'invoice': '开票', 'pay': '付款'}
    due = Decimal(due or 0)
    amount = Decimal(amount or 0)
    if amount <= 0:
        return '未' + labels[dimension]
    if amount >= due and due > 0:
        return '已足额' + labels[dimension] + ' ¥' + _fmt(amount)
    return '部分' + labels[dimension] + ' ¥' + _fmt(amount)


def _fmt(value):
    from .money import fmt_money
    return fmt_money(value)


def payment_state(due, applied, paid):
    """付款列：未申请 / 已申请 / 部分付款 / 已付款（展示用，永不参与校验）"""
    due = Decimal(due or 0)
    applied = Decimal(applied or 0)
    paid = Decimal(paid or 0)
    if due > 0 and paid >= due:
        return '已付款'
    if paid > 0:
        return '部分付款'
    if applied > 0:
        return '已申请'
    return '未申请'


def invoice_state(due, invoice):
    """发票列：未开票 / 已开票"""
    return '已开票' if Decimal(invoice or 0) > 0 else '未开票'


def three_dim(node):
    """返回节点的三维进度字典（Decimal 金额 + 展示标签）。"""
    due = Decimal(node.due_amount or 0)
    applied = Decimal(getattr(node, 'applied_amount', 0) or 0)
    invoice = Decimal(node.invoice_amount or 0)
    paid = Decimal(node.paid_amount or 0)
    return {
        'due': due.quantize(CENT),
        'applied': applied.quantize(CENT),
        'invoice': invoice.quantize(CENT),
        'paid': paid.quantize(CENT),
        # CR-06 申请口径派生量（统一从模型属性取，模板不再自行计算）
        'unpaid': node.unpaid_amount.quantize(CENT),
        'unapplied': node.unapplied_amount.quantize(CENT),
        'unpaid_note': node.unpaid_note,
        'apply_label': node_progress_label('apply', due, applied),
        'invoice_label': node_progress_label('invoice', due, invoice),
        'pay_label': node_progress_label('pay', due, paid),
        'payment_state': payment_state(due, applied, paid),
        'invoice_state': invoice_state(due, invoice),
        'status': derive_status(due, applied, invoice, paid),
    }

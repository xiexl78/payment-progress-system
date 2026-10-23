# -*- coding: utf-8 -*-
"""统一金额工具（P0-3 基础层）

设计原则
--------
1. **金额一律用 ``Decimal``**：禁止 ``float`` 参与金额计算或作为构造入参
   （收到 ``float`` 直接 ``TypeError``），比例同样用字符串构造，
   例如 ``Decimal('0.30')`` 表示 30%。
2. **统一舍入**：一律 ``ROUND_HALF_UP`` 后 ``quantize(Decimal('0.01'))``，
   符合财务"四舍五入到分"口径（Python 内置 ``round`` 是银行家舍入）。
3. **范围对齐数据库**：金额字段是 ``NUMERIC(14,2)``，合法范围
   ``0 <= x <= 999,999,999,999.99``；超范围、``nan``、``inf``、科学计数
   （``1e309``）一律视为非法输入。
4. **纯函数、无副作用**：本模块不碰 Flask / SQLAlchemy / 数据库，
   便于单测和后续被 routes / models / contract_engine 共同复用。

本模块当前**尚未接入**现有业务代码（按计划分两步接入，见
``docs/fix-plan-p0.md`` 第 5 节）：第一步替换计算点，第二步替换表单解析。
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

__all__ = [
    'CENT',
    'ZERO',
    'MAX_MONEY',
    'parse_money',
    'mul_ratio',
    'sum_money',
    'fmt_money',
    'allocate_tail',
]

#: 最小货币单位（分）
CENT = Decimal('0.01')
#: 金额零值（两位小数）
ZERO = Decimal('0.00')
#: 与 NUMERIC(14,2) 对齐的金额上限：12 位整数 + 2 位小数
MAX_MONEY = Decimal('999999999999.99')

#: 尾差归集默认顺序：质保金 → 验收款 → 进度款 → 首付款
DEFAULT_TAIL_ORDER = ('warranty', 'acceptance', 'progress', 'first')

# 全角数字/标点 → 半角（仅做输入规范化，不做语义解释）
_FULLWIDTH_TRANS = str.maketrans({
    '０': '0', '１': '1', '２': '2', '３': '3', '４': '4',
    '５': '5', '６': '6', '７': '7', '８': '8', '９': '9',
    '．': '.', '，': ',', '　': ' ',
})

# 单个金额字面量：1~12 位整数 + 可选 1~2 位小数（不带千分位、不带符号）
_PLAIN_NUMBER_RE = None  # 延迟编译，保持模块导入无正则依赖开销

# 带千分位的金额字面量：1~3 位 + 每三位逗号 + 可选小数
_THOUSANDS_RE = None


def _regexes():
    """延迟构造正则（纯性能考虑，避免模块导入期反复编译）。"""
    global _PLAIN_NUMBER_RE, _THOUSANDS_RE
    if _PLAIN_NUMBER_RE is None:
        import re
        _PLAIN_NUMBER_RE = re.compile(r'^\d{1,12}(?:\.\d{1,2})?$')
        _THOUSANDS_RE = re.compile(r'^\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?$')
    return _PLAIN_NUMBER_RE, _THOUSANDS_RE


def _invalid(message):
    """构造统一的输入错误（ValueError，便于上层转成表单错误提示）。"""
    return ValueError(message)


def _parse_str(raw, *, allow_negative):
    """把字符串解析为 Decimal；非法返回 None。

    接受：``'165000'``、``'165,000.00'``、``'￥165,000.00'``、``' 100.30 '``、
    全角数字/逗号/句点。拒绝：空串、非数字、``nan/inf``、科学计数、
    千分位写错（``1,2,3``）、多于 2 位小数、多个小数点/符号。
    """
    if raw is None:
        return None
    text = str(raw).translate(_FULLWIDTH_TRANS).strip()
    if not text:
        return None
    # 去除货币符号（仅允许出现在首尾，避免把符号当数字）
    text = text.replace('￥', '').replace('¥', '').replace('$', '').strip()
    if not text:
        return None

    negative = False
    if text[0] in '+-':
        negative = text[0] == '-'
        text = text[1:].strip()
        if not text:
            return None

    plain_re, thousands_re = _regexes()
    if ',' in text:
        if not thousands_re.match(text):
            return None
        text = text.replace(',', '')
    elif not plain_re.match(text):
        return None

    # 长度再兜一次（千分位去掉后同一规则）
    if not plain_re.match(text):
        return None

    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    if negative:
        if not allow_negative:
            return None
        value = -value
    # 上限按绝对值判断（负值仅调整额场景使用）
    if abs(value) > MAX_MONEY:
        return None
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_money(value, *, allow_negative=False):
    """解析金额（元），返回两位小数的 ``Decimal``；非法输入返回 ``None``。

    Parameters
    ----------
    value:
        字符串 / ``Decimal`` / ``int`` / ``None``。``float`` 与 ``bool`` 会抛
        ``TypeError``（防止浮点误差从入口混入）。
    allow_negative:
        ``False``（默认）时拒绝负数；仅在"调整额/冲销"等明确场景传 ``True``。

    Notes
    -----
    - 空字符串、``None`` 返回 ``None``（由调用方决定是"必填错误"还是"按 0 处理"）；
    - 拒绝 ``nan``/``inf``/科学计数/超过 ``NUMERIC(14,2)`` 范围；
    - 返回值 scale 固定为 2（``Decimal('15.05')``），可直接与数据库比较。
    """
    if value is None:
        return None
    if isinstance(value, float):
        raise TypeError('金额禁止使用 float，请使用 Decimal("...") 或字符串')
    if isinstance(value, bool):
        raise TypeError('金额不接受 bool')
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        if value < 0 and not allow_negative:
            return None
        if abs(value) > MAX_MONEY:
            return None
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    if isinstance(value, int):
        return parse_money(Decimal(value), allow_negative=allow_negative)
    if isinstance(value, str):
        return _parse_str(value, allow_negative=allow_negative)
    raise TypeError('金额类型不支持：%r' % (type(value),))


def _require_decimal(value, *, name, allow_negative=False):
    """内部严格转换：非法抛异常（用于 mul_ratio / sum_money / allocate_tail）。"""
    parsed = parse_money(value, allow_negative=allow_negative)
    if parsed is None:
        raise _invalid('%s 不是有效金额：%r' % (name, value))
    return parsed


def mul_ratio(base, ratio):
    """金额 × 比例，返回 ROUND_HALF_UP 到分的 ``Decimal``。

    ``ratio`` 为**小数比例**（``Decimal('0.30')`` = 30%），禁止传 ``30``；
    传入 ``float`` 抛 ``TypeError``。越界（比例不在 ``[0, 1]``、
    金额为负或超上限）抛 ``ValueError``。

    >>> mul_ratio(Decimal('100.30'), Decimal('0.15'))
    Decimal('15.05')
    """
    if isinstance(base, float) or isinstance(ratio, float):
        raise TypeError('金额/比例禁止使用 float，请使用 Decimal("...") 或字符串')
    base_d = _require_decimal(base, name='金额')
    ratio_d = _require_decimal(ratio, name='比例')
    if ratio_d < 0 or ratio_d > 1:
        raise _invalid('比例必须在 0~1 之间（0.30 表示 30%%）：%r' % (ratio,))
    result = (base_d * ratio_d).quantize(CENT, rounding=ROUND_HALF_UP)
    if result > MAX_MONEY:
        raise _invalid('金额计算结果超出 NUMERIC(14,2) 上限：%r' % (result,))
    return result


def sum_money(values, *, allow_negative=False):
    """金额求和，返回两位小数 ``Decimal``。

    ``values`` 为 Decimal / 字符串 / 整数 / ``None`` 的可迭代对象，
    ``None`` 按 0 处理（对应数据库中可空金额列）。任一元素非法则抛
    ``ValueError``（不静默跳过，避免"少算一笔"）。
    """
    total = ZERO
    for item in values:
        if item is None:
            continue
        total += _require_decimal(item, name='求和元素', allow_negative=allow_negative)
    total = total.quantize(CENT, rounding=ROUND_HALF_UP)
    if not allow_negative and total < 0:
        raise _invalid('金额合计为负数：%r' % (total,))
    if abs(total) > MAX_MONEY:
        raise _invalid('金额合计超出 NUMERIC(14,2) 上限：%r' % (total,))
    return total


def fmt_money(value, *, symbol=False, currency='￥'):
    """格式化为千分位两位小数字符串（用于页面/导出）。

    - ``None`` 输出 ``'0.00'``（页面占位习惯）；
    - ``symbol=True`` 时输出 ``'￥165,000.00'``，负数为 ``'-￥1,234.50'``；
    - 非法输入抛 ``ValueError``（避免把错误数据静默显示成 0）。
    """
    if value is None:
        return ('%s0.00' % currency) if symbol else '0.00'
    parsed = parse_money(value, allow_negative=True)
    if parsed is None:
        raise _invalid('无法格式化金额：%r' % (value,))
    negative = parsed < 0
    body = ('%.2f' % abs(parsed)).replace('-', '')
    # 手动加千分位（避免依赖 format 对 Decimal 的本地化行为）
    int_part, _, dec_part = body.partition('.')
    groups = []
    while len(int_part) > 3:
        groups.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    groups.insert(0, int_part)
    formatted = '%s.%s' % (','.join(groups), dec_part or '00')
    if negative:
        return '-%s%s' % (currency if symbol else '', formatted)
    return ('%s%s' % (currency, formatted)) if symbol else formatted


def allocate_tail(amounts, target, *, order=DEFAULT_TAIL_ORDER):
    """把逐节点四舍五入后的尾差归集到指定节点，返回新的金额字典。

    Parameters
    ----------
    amounts:
        ``{node_type: Decimal}``，已按各节点 ``base × ratio`` 逐项 ROUND_HALF_UP。
    target:
        精确目标合计（未逐项舍入前的 ``Σ base×ratio``，同样 ROUND_HALF_UP）。
    order:
        归集尝试顺序，默认 ``warranty → acceptance → progress → first``；
        不在 ``order`` 中的键按 ``amounts`` 插入顺序追加。

    规则
    ----
    - 尾差全部落到**第一个"金额 > 0 且落差后仍 >= 0"的节点**；
    - 找不到可归集节点（例如全部为 0 或尾差过大）抛 ``ValueError``；
    - 返回字典与 ``amounts`` 键集合一致，且 ``sum == target``（分位口径）。

    例：``{first:30.09, progress:52.16, acceptance:15.05, warranty:3.01}``
    + ``target=100.30`` → 质保金 3.00，合计 100.30。
    """
    if isinstance(target, float):
        raise TypeError('金额禁止使用 float')
    target_d = _require_decimal(target, name='目标金额')
    if not isinstance(amounts, dict) or not amounts:
        raise _invalid('amounts 必须是非空字典')

    result = {}
    for key, value in amounts.items():
        result[key] = _require_decimal(value, name='节点金额(%s)' % key)

    total = sum_money(result.values())
    tail = (target_d - total).quantize(CENT, rounding=ROUND_HALF_UP)
    if tail == 0:
        return result

    candidates = [k for k in order if k in result]
    candidates += [k for k in result if k not in candidates]
    moved = False
    for key in candidates:
        if result[key] > 0:
            adjusted = (result[key] + tail).quantize(CENT, rounding=ROUND_HALF_UP)
            if adjusted >= 0:
                result[key] = adjusted
                moved = True
                break
    if not moved or sum_money(result.values()) != target_d:
        raise _invalid('尾差无法归集：tail=%r target=%r amounts=%r'
                       % (tail, target_d, amounts))
    return result

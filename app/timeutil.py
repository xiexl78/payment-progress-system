# -*- coding: utf-8 -*-
"""时间口径（批次 4c）

- **存储统一 UTC**：所有 ``DateTime`` 列存 naive UTC；
- **展示/筛选按业务时区**：``APP_TIMEZONE``（默认 ``Asia/Shanghai``）换算；
- **业务日期不改**：验收/付款/试运行等是 ``Date``（不带时区的日历日），不做换算。
"""

from datetime import date, datetime, timedelta, timezone

from flask import current_app

_DEFAULT_TZ = 'Asia/Shanghai'


def utcnow():
    """当前 UTC 时间（naive，与数据库列一致）。"""
    return datetime.utcnow()


def _tz():
    name = (current_app.config.get('APP_TIMEZONE') or _DEFAULT_TZ).strip()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        # Windows 可能缺少 IANA 时区库（未装 tzdata）：东亚业务时区回退固定 +8
        if name in ('Asia/Shanghai', 'Asia/Chongqing', 'Asia/Hong_Kong', 'PRC'):
            return timezone(timedelta(hours=8))
        return timezone.utc


def to_local(value):
    """UTC naive datetime → 业务时区 aware datetime；非 datetime 原样返回。"""
    if value is None or not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_tz())


def local_today():
    """业务时区的今天（用于默认付款日期、提醒计算）。"""
    return to_local(utcnow()).date()


def local_datetime_to_utc(value):
    """业务时区的本地 datetime/date → UTC naive（Excel 导入的时间列用）。

    ``date`` 按当天 00:00 处理；已是 aware 的 datetime 直接换算。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_tz())
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def local_date_start_utc(day):
    """业务时区某天 00:00 → UTC naive（用于审计筛选下界）。"""
    aware = datetime(day.year, day.month, day.day, tzinfo=_tz())
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def local_date_end_utc(day):
    """业务时区某天次日 00:00 → UTC naive（用于审计筛选上界，左闭右开）。"""
    return local_date_start_utc(day + timedelta(days=1))


def month_start_local(days_ago=0):
    """（预留）业务时区今天往前 days_ago 天的 00:00。"""
    return local_date_start_utc(local_today() - timedelta(days=days_ago))


def _last_day_of_month(year, month):
    """某年某月的最后一天（不引入 calendar 以外的依赖）。"""
    import calendar
    return calendar.monthrange(year, month)[1]


def add_months(day, months):
    """日期加 N 个自然月（CR-05：质保到期日 = 验收日 + warranty_months）。

    按目标月最后一天对齐：1/31 + 1 月 = 2/28（闰年 2/29）；日期本身是月末时
    结果也是目标月末。``months`` 为空/0 时原样返回；``day`` 为空返回 None。
    """
    if day is None:
        return None
    try:
        months = int(months or 0)
    except (TypeError, ValueError):
        return None
    total = day.year * 12 + (day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    last = _last_day_of_month(year, month)
    return date(year, month, min(day.day, last))


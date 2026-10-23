# -*- coding: utf-8 -*-
"""登录失败限速 / 锁定（批次 4b）

- 按 **用户名 + IP** 计数：5 分钟内失败 5 次 → 锁定 15 分钟；
- 间隔/阈值/锁定时间均可通过 app.config 配置；
- 单实例内存实现（10~20 人内网足够）；多进程部署需改共享存储（阶段 2 说明）；
- 测试模式（显式 config + 默认 SECRET_KEY）默认关闭，避免打挂 QA 基线。
"""

import threading
import time
from collections import deque

_LOCK = threading.Lock()
_FAILURES = {}   # key -> deque[timestamp]
_LOCKED = {}     # key -> unlock timestamp


def _key(username, ip):
    return '%s|%s' % ((username or '').strip().lower(), ip or '')


def _prune(key, window_seconds, now):
    queue = _FAILURES.get(key)
    if not queue:
        return
    while queue and now - queue[0] > window_seconds:
        queue.popleft()
    if not queue:
        _FAILURES.pop(key, None)


def is_locked(username, ip, *, window_seconds=300, lock_seconds=900):
    key = _key(username, ip)
    now = time.monotonic()
    with _LOCK:
        unlock_at = _LOCKED.get(key)
        if unlock_at is not None:
            if unlock_at > now:
                return True
            _LOCKED.pop(key, None)
            _FAILURES.pop(key, None)
    return False


def failure_count(username, ip, *, window_seconds=300):
    key = _key(username, ip)
    now = time.monotonic()
    with _LOCK:
        _prune(key, window_seconds, now)
        return len(_FAILURES.get(key, ()))


def register_failure(username, ip, *, window_seconds=300, max_failures=5, lock_seconds=900):
    """记录一次失败；返回是否触发锁定。"""
    key = _key(username, ip)
    now = time.monotonic()
    with _LOCK:
        _prune(key, window_seconds, now)
        queue = _FAILURES.setdefault(key, deque())
        queue.append(now)
        if len(queue) >= max_failures:
            _LOCKED[key] = now + lock_seconds
            return True
    return False


def reset(username, ip):
    key = _key(username, ip)
    with _LOCK:
        _FAILURES.pop(key, None)
        _LOCKED.pop(key, None)

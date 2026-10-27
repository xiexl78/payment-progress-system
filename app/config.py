# -*- coding: utf-8 -*-
"""配置外置（批次 5）

加载优先级（后者覆盖前者）：
    默认值 < ``PAYMENT_CONFIG_FILE`` 文件 < 进程环境变量 < ``create_app(config=...)`` 显式注入

本模块只做解析/类型/范围校验，不建目录、不连库；目录与数据库等运行环境检查在
``create_app`` 里统一做（见 ``app/__init__.py``）。
"""

import os
from datetime import timedelta

#: 历史默认密钥：生产启动明确拒绝
DEFAULT_SECRET_KEY = 'payment-progress-secret-key-change-in-prod'
#: 生产密钥最小长度（低于该值拒绝启动；设计建议 32，测试环境可放宽）
MIN_SECRET_LENGTH = 16


def _raw(file_values, key):
    """按「环境变量 > 配置文件」取原始字符串。"""
    value = os.environ.get(key)
    if value is not None and str(value).strip() != '':
        return str(value).strip()
    value = file_values.get(key)
    if value is not None and str(value).strip() != '':
        return str(value).strip()
    return None


def _parse_bool(raw, *, key, errors, default=False):
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in ('1', 'true', 'yes', 'on'):
        return True
    if text in ('0', 'false', 'no', 'off'):
        return False
    errors.append('%s 取值不合法（应为 true/false）：%r' % (key, raw))
    return default


def _parse_number(raw, *, key, errors, default, minimum, maximum, cast=float):
    if raw is None:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        errors.append('%s 必须是数字：%r' % (key, raw))
        return default
    if value < minimum or value > maximum:
        errors.append('%s 超出范围 %.2f~%.2f：%r' % (key, minimum, maximum, raw))
        return default
    return value


def read_config_file(path):
    """读取 key=value 配置文件；不存在/不可读返回 {} 并给出错误。"""
    values = {}
    if not path:
        return values, []
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        return values, ['PAYMENT_CONFIG_FILE 不存在或不可读：%s' % path]
    try:
        with open(path, 'r', encoding='utf-8-sig') as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                values[key.strip()] = value.strip().strip('"').strip("'")
    except Exception as exc:
        return values, ['PAYMENT_CONFIG_FILE 读取失败：%s（%s）' % (path, exc)]
    return values, []


def load_config(base_dir, config_file=None, explicit=False):
    """解析配置，返回 ``(cfg, errors, warnings)``。

    ``explicit=True`` 表示调用方会传入 ``create_app(config=...)``（测试/嵌入），
    此时允许使用历史默认密钥，并跳过"生产密钥必填"等强校验，以保持测试可覆盖。
    """
    errors = []
    warnings = []
    file_values, file_errors = read_config_file(config_file)
    errors.extend(file_errors)

    app_env = (_raw(file_values, 'APP_ENV') or 'development').lower()
    if app_env not in ('production', 'development'):
        errors.append('APP_ENV 只能为 production/development：%r' % app_env)
        app_env = 'development'

    data_dir = _raw(file_values, 'DATA_DIR')
    if not data_dir:
        if app_env == 'production':
            program_data = os.environ.get('ProgramData') or 'C:\\ProgramData'
            data_dir = os.path.join(program_data, 'PaymentSystem', 'data')
        else:
            data_dir = os.path.join(base_dir, 'instance')
    data_dir = os.path.abspath(os.path.expanduser(data_dir))

    database_url = _raw(file_values, 'DATABASE_URL')
    if not database_url:
        database_url = 'sqlite:///' + os.path.join(data_dir, 'payment_progress.db')
    if not explicit:
        if not database_url.startswith('sqlite:///'):
            errors.append('阶段 1 仅支持 sqlite:/// 数据库：%r' % database_url)
        else:
            db_path = database_url[len('sqlite:///'):]
            if not os.path.isabs(db_path):
                errors.append('DATABASE_URL 必须是 sqlite:/// 绝对路径：%r' % database_url)

    secret_key = _raw(file_values, 'SECRET_KEY')
    fallbacks_raw = _raw(file_values, 'SECRET_KEY_FALLBACKS') or ''
    fallbacks = [x.strip() for x in fallbacks_raw.split(',') if x.strip()]
    if not explicit:
        if not secret_key:
            errors.append('SECRET_KEY 未配置：请设置环境变量或配置文件（生产必填）')
        elif secret_key == DEFAULT_SECRET_KEY:
            errors.append('SECRET_KEY 不能使用历史默认值')
        elif len(secret_key) < MIN_SECRET_LENGTH:
            errors.append('SECRET_KEY 长度不足 %d 位' % MIN_SECRET_LENGTH)
        elif len(set(secret_key)) == 1:
            errors.append('SECRET_KEY 不能使用全同字符')
        elif len(secret_key) < 32:
            warnings.append('SECRET_KEY 长度小于建议的 32 位，建议更换更强密钥')
    for item in fallbacks:
        if len(item) < MIN_SECRET_LENGTH:
            errors.append('SECRET_KEY_FALLBACKS 每一项长度需 ≥%d：%r'
                          % (MIN_SECRET_LENGTH, item))

    max_upload_mb = _parse_number(_raw(file_values, 'MAX_UPLOAD_MB'),
                                 key='MAX_UPLOAD_MB', errors=errors,
                                 default=20, minimum=1, maximum=100, cast=int)
    session_hours = _parse_number(_raw(file_values, 'SESSION_LIFETIME_HOURS'),
                                  key='SESSION_LIFETIME_HOURS', errors=errors,
                                  default=8, minimum=0, maximum=72)
    if session_hours == 0:
        session_lifetime = timedelta(days=3650)   # 0=不启用过期（不推荐）
        warnings.append('SESSION_LIFETIME_HOURS=0：会话不会按时间过期，不建议生产使用')
    else:
        session_lifetime = timedelta(hours=session_hours)

    secure_default = (app_env == 'production')
    cookie_secure = _parse_bool(_raw(file_values, 'SESSION_COOKIE_SECURE'),
                                key='SESSION_COOKIE_SECURE', errors=errors,
                                default=secure_default)
    debug = _parse_bool(_raw(file_values, 'DEBUG'), key='DEBUG',
                        errors=errors, default=False)
    if app_env == 'production' and debug:
        errors.append('生产环境（APP_ENV=production）禁止开启 DEBUG')

    timezone_name = _raw(file_values, 'APP_TIMEZONE') or 'Asia/Shanghai'

    llm_enabled = _parse_bool(_raw(file_values, 'LLM_ENABLED'), key='LLM_ENABLED',
                              errors=errors, default=False)
    llm_endpoint = _raw(file_values, 'CONTRACT_LLM_ENDPOINT') or ''
    llm_api_key = _raw(file_values, 'CONTRACT_LLM_API_KEY') or ''
    llm_model = _raw(file_values, 'CONTRACT_LLM_MODEL') or ''
    if llm_enabled and not (llm_endpoint and llm_api_key and llm_model):
        errors.append('LLM_ENABLED=true 时必须同时配置 '
                      'CONTRACT_LLM_ENDPOINT / CONTRACT_LLM_API_KEY / CONTRACT_LLM_MODEL')

    password_min = _parse_number(_raw(file_values, 'PASSWORD_MIN_LENGTH'),
                                 key='PASSWORD_MIN_LENGTH', errors=errors,
                                 default=8, minimum=6, maximum=64, cast=int)
    login_max = _parse_number(_raw(file_values, 'LOGIN_MAX_FAILURES'),
                              key='LOGIN_MAX_FAILURES', errors=errors,
                              default=5, minimum=1, maximum=20, cast=int)
    lockout_minutes = _parse_number(
        _raw(file_values, 'LOGIN_LOCKOUT_MINUTES') or _raw(file_values, 'LOGIN_LOCK_MINUTES'),
        key='LOGIN_LOCKOUT_MINUTES', errors=errors, default=15,
        minimum=1, maximum=1440, cast=int)
    failure_window = _parse_number(_raw(file_values, 'LOGIN_FAILURE_WINDOW_MINUTES'),
                                   key='LOGIN_FAILURE_WINDOW_MINUTES', errors=errors,
                                   default=5, minimum=1, maximum=1440, cast=int)
    trusted_proxy = _parse_number(_raw(file_values, 'TRUSTED_PROXY_COUNT'),
                                  key='TRUSTED_PROXY_COUNT', errors=errors,
                                  default=0, minimum=0, maximum=2, cast=int)
    #: 节点操作令牌有效期（分钟），默认 30，范围 1~1440。
    #: 测试可通过 create_app(config={'NODE_TOKEN_TTL_MINUTES': 1/60}) 注入
    #: 更短的值（显式 config 不走本文件的取值范围校验）。
    node_token_ttl_minutes = _parse_number(
        _raw(file_values, 'NODE_TOKEN_TTL_MINUTES'),
        key='NODE_TOKEN_TTL_MINUTES', errors=errors,
        default=30, minimum=1, maximum=1440)

    cfg = {
        'APP_ENV': app_env,
        'DEBUG': debug,
        'SECRET_KEY': secret_key,
        'SECRET_KEY_FALLBACKS': fallbacks,
        'DATA_DIR': data_dir,
        'UPLOAD_DIR': os.path.join(data_dir, 'uploads'),
        'SQLALCHEMY_DATABASE_URI': database_url,
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'MAX_CONTENT_LENGTH': int(max_upload_mb) * 1024 * 1024,
        'PERMANENT_SESSION_LIFETIME': session_lifetime,
        'SESSION_COOKIE_HTTPONLY': True,
        'SESSION_COOKIE_SAMESITE': 'Lax',
        'SESSION_COOKIE_SECURE': cookie_secure,
        'APP_TIMEZONE': timezone_name,
        'LLM_ENABLED': llm_enabled,
        'PASSWORD_MIN_LENGTH': int(password_min),
        'LOGIN_MAX_FAILURES': int(login_max),
        'LOGIN_LOCKOUT_MINUTES': int(lockout_minutes),
        'LOGIN_LOCK_MINUTES': int(lockout_minutes),
        'LOGIN_FAILURE_WINDOW_MINUTES': int(failure_window),
        'TRUSTED_PROXY_COUNT': int(trusted_proxy),
        'NODE_TOKEN_TTL_MINUTES': float(node_token_ttl_minutes),
        'CSRF_ENABLED': True,
        'TESTING': False,
        'FORCE_DEFAULT_PASSWORD_CHANGE': (app_env == 'production'),
        'LOGIN_THROTTLE_ENABLED': True,
    }
    return cfg, errors, warnings

# -*- coding: utf-8 -*-
"""付款进度管理系统 - Flask 应用工厂与配置"""
import os
from datetime import timedelta

from flask import Flask
from sqlalchemy import event
from .models import db

#: 历史默认密钥：生产启动时明确拒绝，仅显式 config 注入（测试/本地）可用
DEFAULT_SECRET_KEY = 'payment-progress-secret-key-change-in-prod'

# 付款节点模板：node_type / 名称 / 比例 / 计算口径 / 触发条件
NODE_TEMPLATES = [
    {
        'node_type': 'first',
        'node_name': '首付款30%',
        'ratio': 0.30,
        'calc_base': 'original',   # 按原金额
        'trigger': '合同签订即触发',
    },
    {
        'node_type': 'progress',
        'node_name': '进度款52%',
        'ratio': 0.52,
        'calc_base': 'total',      # 按总金额
        'trigger': '试运行后触发',
    },
    {
        'node_type': 'acceptance',
        'node_name': '验收款15%',
        'ratio': 0.15,
        'calc_base': 'total',
        'trigger': '验收后触发',
    },
    {
        'node_type': 'warranty',
        'node_name': '质保金3%',
        'ratio': 0.03,
        'calc_base': 'total',
        'trigger': '质保到期后触发（验收日期+3年）',
    },
]

# 付款流程状态
FLOW_STATUS = ['未触发', '未开票已申请', '已申请', '已付款']


def create_app(config=None):
    """应用工厂。

    - ``config`` 为 None（run.py 生产/开发启动路径）：``SECRET_KEY`` 必须来自
      环境变量且不能等于历史默认值，否则拒绝启动；CSRF 强制开启；
    - ``config`` 显式注入（测试/本地/自定义部署）：以注入值为准；未提供
      SECRET_KEY 时回退默认值，默认关闭 CSRF（可用 ``CSRF_ENABLED=True`` 强开）。
    """
    explicit_config = config is not None
    app = Flask(__name__, instance_relative_config=True)
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    app.config.from_mapping(
        SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(base_dir, 'instance', 'payment_progress.db'),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=int(os.environ.get('MAX_UPLOAD_MB', '20')) * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '0').strip() == '1',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        APP_TIMEZONE=os.environ.get('APP_TIMEZONE', 'Asia/Shanghai'),
        CSRF_ENABLED=True,
        DEBUG=False,
        TESTING=False,
        # 批次 4b：密码与登录限速
        PASSWORD_MIN_LENGTH=8,
        LOGIN_MAX_FAILURES=5,
        LOGIN_FAILURE_WINDOW_MINUTES=5,
        LOGIN_LOCK_MINUTES=15,
        LOGIN_THROTTLE_ENABLED=True,
        FORCE_DEFAULT_PASSWORD_CHANGE=True,
    )
    if explicit_config:
        app.config.update(config)
        if not app.config.get('SECRET_KEY'):
            app.config['SECRET_KEY'] = DEFAULT_SECRET_KEY
        if 'CSRF_ENABLED' not in config:
            # 使用默认密钥的显式配置视为测试/本地模式，默认不强制 CSRF
            app.config['CSRF_ENABLED'] = app.config['SECRET_KEY'] != DEFAULT_SECRET_KEY
        if app.config['SECRET_KEY'] == DEFAULT_SECRET_KEY:
            app.logger.warning('使用历史默认 SECRET_KEY（仅限测试/本地），CSRF_ENABLED=%s',
                               app.config['CSRF_ENABLED'])
            # 测试/本地模式默认关闭「默认口令强制改密」与「登录限速」，避免打挂基线
            if 'FORCE_DEFAULT_PASSWORD_CHANGE' not in config:
                app.config['FORCE_DEFAULT_PASSWORD_CHANGE'] = False
            if 'LOGIN_THROTTLE_ENABLED' not in config:
                app.config['LOGIN_THROTTLE_ENABLED'] = False
    else:
        secret = (os.environ.get('SECRET_KEY') or '').strip()
        if not secret or secret == DEFAULT_SECRET_KEY:
            raise RuntimeError(
                'SECRET_KEY 未配置或仍为历史默认值，拒绝启动。'
                '请设置环境变量 SECRET_KEY 为随机密钥（如 '
                'python -c "import secrets;print(secrets.token_urlsafe(48))" 生成）。')
        app.config['SECRET_KEY'] = secret

    os.makedirs(os.path.join(base_dir, 'instance'), exist_ok=True)
    db.init_app(app)

    # SQLite 并发写入稳健性：WAL 模式 + 等待锁超时，避免多请求下 database is locked
    # 注意：必须在任何连接建立之前注册（schema 自动升级会先连库）
    with app.app_context():
        engine = db.engine

        @event.listens_for(engine, 'connect')
        def set_sqlite_pragma(dbapi_connection, connection_record):
            try:
                cursor = dbapi_connection.cursor()
                cursor.execute('PRAGMA journal_mode=WAL')
                cursor.execute('PRAGMA busy_timeout=5000')
                cursor.execute('PRAGMA foreign_keys=ON')
                cursor.close()
            except Exception:
                pass

    from .schema import upgrade_schema
    upgrade_schema(app)

    from .security import init_security
    init_security(app)

    from .routes import bp
    app.register_blueprint(bp)

    return app
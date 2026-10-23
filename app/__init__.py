# -*- coding: utf-8 -*-
"""付款进度管理系统 - Flask 应用工厂与配置

配置加载见 ``app/config.py``（优先级：显式 config > 环境变量 > 配置文件 > 默认）。
本模块负责：建目录、启动自检（fail-fast）、SQLite 连接参数、注册蓝图。
"""

import logging
import os
import sqlite3
from logging.handlers import RotatingFileHandler

from flask import Flask
from sqlalchemy import event
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import DEFAULT_SECRET_KEY, load_config
from .models import db

__all__ = ['create_app', 'DEFAULT_SECRET_KEY', 'NODE_TEMPLATES', 'FLOW_STATUS']

#: 当前进程内由本模块挂上的文件日志 handler（多 app 实例时换目录要换 handler）
_FILE_LOG_HANDLER = None


def _setup_file_logging(app):
    """文件日志 + 轮转（P2-09）：``DATA_DIR/logs/app.log``，5MB × 5 份。

    测试会反复 create_app，用模块级 handler 复用/替换，避免同一 logger 上堆积句柄。
    """
    global _FILE_LOG_HANDLER
    data_dir = app.config.get('DATA_DIR')
    if not data_dir:
        return
    log_dir = os.path.join(data_dir, 'logs')
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as exc:
        app.logger.warning('日志目录无法创建：%s（%s），仅控制台日志', log_dir, exc)
        return
    path = os.path.abspath(os.path.join(log_dir, 'app.log'))
    previous = _FILE_LOG_HANDLER
    if previous is not None and getattr(previous, 'baseFilename', None) == path:
        return
    try:
        handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024,
                                      backupCount=5, encoding='utf-8')
    except OSError as exc:
        app.logger.warning('文件日志初始化失败：%s（%s），仅控制台日志', path, exc)
        return
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s] %(message)s'))
    handler.setLevel(logging.INFO)
    if previous is not None:
        app.logger.removeHandler(previous)
        try:
            previous.close()
        except Exception:
            pass
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)
    _FILE_LOG_HANDLER = handler

# 付款节点模板：node_type / 名称 / 比例 / 计算口径 / 触发条件
NODE_TEMPLATES = [
    {
        'node_type': 'first',
        'node_name': '首付款30%',
        'ratio': 0.30,
        'calc_base': 'total',      # 统一口径：所有节点按原金额+增补金额（尾差归质保金）
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


def _ensure_writable_dir(path, *, label, errors):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, '.write_probe')
        with open(probe, 'w', encoding='utf-8') as handle:
            handle.write('ok')
        os.remove(probe)
        return True
    except Exception as exc:
        errors.append('%s 无法创建或不可写：%s（%s）' % (label, path, exc))
        return False


def _check_database_file(app, errors, warnings):
    """非测试启动路径的数据库自检：缺库/损坏按环境决定 fail 或 warn。"""
    uri = app.config.get('SQLALCHEMY_DATABASE_URI') or ''
    if not uri.startswith('sqlite:///'):
        return
    db_path = uri[len('sqlite:///'):]
    exists = os.path.isfile(db_path)
    if not exists:
        if app.config.get('APP_ENV') == 'production':
            errors.append('数据库不存在：%s；请先运行 python scripts/init_db.py' % db_path)
        else:
            warnings.append('数据库不存在（开发环境）：%s，登录前请先运行 '
                            'python scripts/init_db.py' % db_path)
        return
    try:
        conn = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True, timeout=5)
        try:
            conn.execute('PRAGMA schema_version').fetchone()
            conn.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
        finally:
            conn.close()
    except Exception as exc:
        errors.append('数据库文件损坏或不可读：%s（%s）；请使用备份恢复' % (db_path, exc))


def create_app(config=None):
    """应用工厂。

    - 显式 ``config`` 注入（测试/嵌入）：优先级最高；未提供 SECRET_KEY 时使用
      历史默认值并关闭 CSRF/登录限速/默认口令强制改密（测试模式）；
    - 无显式 config（run.py 生产/开发启动）：读取环境变量与
      ``PAYMENT_CONFIG_FILE``，缺密钥/目录不可写/库损坏等直接拒绝启动。
    """
    explicit_config = config is not None
    app = Flask(__name__, instance_relative_config=True)
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

    cfg, errors, warnings = load_config(
        base_dir, config_file=os.environ.get('PAYMENT_CONFIG_FILE'),
        explicit=explicit_config)
    app.config.from_mapping(cfg)

    if explicit_config:
        app.config.update(config)
        if not app.config.get('SECRET_KEY'):
            app.config['SECRET_KEY'] = DEFAULT_SECRET_KEY
        if 'CSRF_ENABLED' not in config:
            app.config['CSRF_ENABLED'] = (
                app.config['SECRET_KEY'] != DEFAULT_SECRET_KEY)
        if app.config['SECRET_KEY'] == DEFAULT_SECRET_KEY:
            app.config['FORCE_DEFAULT_PASSWORD_CHANGE'] = False
            app.config['LOGIN_THROTTLE_ENABLED'] = False
            warnings.append('测试模式：使用历史默认 SECRET_KEY，CSRF=%s、登录限速=%s'
                            % (app.config['CSRF_ENABLED'],
                               app.config['LOGIN_THROTTLE_ENABLED']))
            for key in ('FORCE_DEFAULT_PASSWORD_CHANGE', 'LOGIN_THROTTLE_ENABLED'):
                if key in config:
                    app.config[key] = config[key]

    if errors:
        raise RuntimeError('配置校验失败: ' + '；'.join(errors))

    # 目录：数据目录 / 上传目录（不可写 → 拒绝启动）
    dir_errors = []
    data_dir = app.config['DATA_DIR']
    upload_dir = app.config['UPLOAD_DIR']
    _ensure_writable_dir(data_dir, label='DATA_DIR', errors=dir_errors)
    _ensure_writable_dir(upload_dir, label='UPLOAD_DIR', errors=dir_errors)
    if not explicit_config:
        _check_database_file(app, dir_errors, warnings)
    if dir_errors:
        raise RuntimeError('启动自检失败: ' + '；'.join(dir_errors))

    if app.config.get('TRUSTED_PROXY_COUNT'):
        app.wsgi_app = ProxyFix(app.wsgi_app,
                                x_for=int(app.config['TRUSTED_PROXY_COUNT']),
                                x_proto=int(app.config['TRUSTED_PROXY_COUNT']))

    _setup_file_logging(app)

    for message in warnings:
        app.logger.warning('[config] %s', message)

    db.init_app(app)

    # SQLite 并发写入稳健性：WAL 模式 + 等待锁超时（仅 sqlite）
    with app.app_context():
        engine = db.engine
        if engine.dialect.name == 'sqlite':
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
    if not app.config.get('SKIP_SCHEMA_UPGRADE'):
        # 只读场景（如导入 dry-run）可跳过，避免对现有库做任何 DDL 写入
        upgrade_schema(app)

    from .security import init_security
    init_security(app)

    from .routes import bp
    app.register_blueprint(bp)

    return app

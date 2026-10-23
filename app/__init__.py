# -*- coding: utf-8 -*-
"""付款进度管理系统 - Flask 应用工厂与配置"""
import os
from flask import Flask
from sqlalchemy import event
from .models import db

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
    app = Flask(__name__, instance_relative_config=True)
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('SECRET_KEY', 'payment-progress-secret-key-change-in-prod'),
        SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(base_dir, 'instance', 'payment_progress.db'),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    )
    if config:
        app.config.update(config)

    os.makedirs(os.path.join(base_dir, 'instance'), exist_ok=True)
    db.init_app(app)

    # SQLite 并发写入稳健性：WAL 模式 + 等待锁超时，避免多请求下 database is locked
    # 注意：db.engine 需要 application context，故在 app_context 内注册事件
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

    from .routes import bp
    app.register_blueprint(bp)

    return app
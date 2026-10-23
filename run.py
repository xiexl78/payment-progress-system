# -*- coding: utf-8 -*-
"""付款进度管理系统 - 启动入口

用法：
    python run.py            # 默认 127.0.0.1:5000
    python run.py --port 8000
"""
import argparse
from app import create_app

app = create_app()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='付款进度管理系统')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--debug', action='store_true', default=False)
    args = parser.parse_args()
    print('=' * 60)
    print('付款进度管理系统已启动')
    print(f'访问地址: http://{args.host}:{args.port}')
    print('默认账号: admin / admin123')
    print('按 Ctrl+C 停止服务')
    print('=' * 60)
    app.run(host=args.host, port=args.port, debug=args.debug)
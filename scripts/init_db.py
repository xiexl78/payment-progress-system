# -*- coding: utf-8 -*-
"""数据库初始化脚本：建表 + 迁移《付款进度表（新增动设备监测）》种子数据 + 默认用户

用法（在工程根目录执行）：
    python scripts/init_db.py [--force]
--force 时删除已有数据库重建；否则仅在不存在的表时创建并写入种子。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app, NODE_TEMPLATES as TEMPLATES
from app.models import db, User, Project, ProjectItem, PaymentNode


def build_seed_project():
    """按 Excel《付款进度表（新增动设备监测）》建立种子项目"""
    project = Project(
        project_no='PJ-2025-DSJ',
        name='新增动设备监测项目',
        status='进行中',
        trial_date=None,          # 由迁移数据回填 2025-11-04
        acceptance_date=None,
        remark='由《付款进度表（新增动设备监测）.xlsx》迁移初始化',
    )
    db.session.add(project)
    db.session.flush()

    seed_items = [
        {'item_name': '设备费', 'tax_rate': '13%', 'original': 131000, 'additional': 0,
         'first': {'invoice': 39300, 'paid': 39300, 'status': '已付款'},
         'progress': {'invoice': 68120, 'paid': 68120, 'status': '已申请'}},
        {'item_name': '集成服务费', 'tax_rate': '6%', 'original': 28000, 'additional': 0,
         'first': {'invoice': 8400, 'paid': 8400, 'status': '已付款'},
         'progress': {'invoice': 14560, 'paid': 14560, 'status': '已申请'}},
        {'item_name': '两年物联网卡', 'tax_rate': '6%', 'original': 6000, 'additional': 0,
         'first': {'invoice': 1800, 'paid': 1800, 'status': '已付款'},
         'progress': {'invoice': 3120, 'paid': 3120, 'status': '已申请'}},
    ]

    for it in seed_items:
        item = ProjectItem(project_id=project.id, item_name=it['item_name'],
                           tax_rate=it['tax_rate'], original_amount=it['original'],
                           additional_amount=it['additional'])
        db.session.add(item)
        db.session.flush()
        for t in TEMPLATES:
            node = PaymentNode(project_id=project.id, item_id=item.id,
                               node_type=t['node_type'], node_name=t['node_name'],
                               ratio=t['ratio'], calc_base=t['calc_base'], status='未触发')
            node.recalc()
            # 首付、进度款迁移实际数据
            key = t['node_type']
            if key in it:
                node.invoice_amount = it[key]['invoice']
                node.paid_amount = it[key]['paid']
                node.status = it[key]['status']
                node.trigger_date = None
            elif t['node_type'] in ('acceptance',):
                node.trigger_date = None
            db.session.add(node)
    return project


def main():
    force = '--force' in sys.argv
    app = create_app()
    with app.app_context():
        db_path = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
        if force and os.path.exists(db_path):
            os.remove(db_path)
            print(f'[init] 已删除旧数据库：{db_path}')

        db.create_all()
        print('[init] 数据表创建完成')

        if User.query.count() == 0:
            admin = User(username='admin', display_name='系统管理员', role='admin')
            admin.set_password('admin123')
            db.session.add(admin)
            operator = User(username='jbr', display_name='经办人', role='operator')
            operator.set_password('123456')
            db.session.add(operator)
            viewer = User(username='viewer', display_name='查看人员', role='viewer')
            viewer.set_password('123456')
            db.session.add(viewer)
            db.session.flush()
            print('[init] 默认用户已创建：admin/admin123（管理员）、jbr/123456（经办）、viewer/123456（查看）')
        else:
            print('[init] 用户已存在，跳过默认用户创建')

        if Project.query.count() == 0:
            p = build_seed_project()
            db.session.flush()
            # 回填试运行日期：按 Excel 中 2025-11-04
            from datetime import date
            p.trial_date = date(2025, 11, 4)
            for node in p.nodes:
                if node.node_type == 'progress':
                    node.trigger_date = date(2025, 11, 4)
            db.session.commit()
            print(f'[init] 种子项目「{p.name}」已创建（{p.project_no}）')
        else:
            db.session.commit()
            print('[init] 项目已存在，跳过种子数据')

        print('[init] 初始化完成')


if __name__ == '__main__':
    main()
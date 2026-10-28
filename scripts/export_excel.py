# -*- coding: utf-8 -*-
"""导出脚本（批次 6a）：项目明细 / 报表汇总 → Excel，审计日志 → CSV。

用法（在工程根目录执行）：
    python scripts/export_excel.py
        导出全部未删除项目明细 + 报表汇总 + 全量审计日志（CSV）。
    python scripts/export_excel.py --project PJ-2025-DSJ
        只导出指定项目（可重复 --project；支持项目编号或数字 ID）。
    python scripts/export_excel.py --no-audit --no-reports
        只导出项目明细。
    python scripts/export_excel.py --audit-date-from 2025-01-01 --audit-date-to 2025-12-31 \
        --audit-actor 张三 --audit-action node.pay
        审计日志按条件导出（与页面筛选口径一致）。

约定：
- 数据库默认 ``instance/payment_progress.db``，可用 ``--db`` 指定；**不存在直接报错**，
  不会新建空库（避免误把空库当数据源）；
- 输出目录默认 ``instance/exports``（已 gitignore），可用 ``--out`` 指定；
- 金额 Decimal 两位小数、时间按 ``APP_TIMEZONE`` 本地化，与页面口径一致；
- 每次导出写一条审计（``export.project`` / ``export.reports`` / ``export.audit``）。

退出码：0 成功；1 数据库/导出失败；2 参数错误。
"""
import argparse
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from decimal import Decimal

from app import create_app  # noqa: E402
from app.audit import record as audit_record  # noqa: E402
from app.exporter import (audit_csv_bytes, build_project_workbook,  # noqa: E402
                          build_reports_workbook, export_filename,
                          workbook_bytes)
from app.models import AuditLog, Project, db  # noqa: E402
from app.money import CENT, sum_money  # noqa: E402
from app.timeutil import (local_date_end_utc, local_date_start_utc,  # noqa: E402
                          local_today)
from app.validation import ValidationError, parse_date  # noqa: E402

DEFAULT_DB = os.path.join(PROJECT_ROOT, 'instance', 'payment_progress.db')
DEFAULT_OUT = os.path.join(PROJECT_ROOT, 'instance', 'exports')


def _print(message=''):
    print(message)


def _err(message):
    print('[ERROR] %s' % message, file=sys.stderr)


def build_app(db_path, data_dir):
    """显式配置建 app：脚本独立运行，不依赖环境变量 SECRET_KEY。"""
    config = {
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + os.path.abspath(db_path),
        'SECRET_KEY': 'cli-export-local-only-%d' % os.getpid(),
        'DATA_DIR': data_dir,
        'UPLOAD_DIR': os.path.join(data_dir, 'uploads'),
        'CSRF_ENABLED': False,
        'LOGIN_THROTTLE_ENABLED': False,
    }
    return create_app(config)


def _load_projects(app, selectors, include_deleted):
    projects = []
    for selector in selectors:
        project = None
        if selector.isdigit():
            project = db.session.get(Project, int(selector))
        if project is None:
            project = Project.query.filter_by(project_no=selector).first()
        if project is None:
            raise LookupError('未找到项目：%s' % selector)
        if project.deleted_at is not None and not include_deleted:
            raise LookupError('项目 %s 已删除（回收站）；如需导出请加 --include-deleted'
                              % selector)
        projects.append(project)
    return projects


def _export_projects(app, projects, out_dir):
    written = []
    for project in projects:
        workbook = build_project_workbook(project)
        payload = workbook_bytes(workbook)
        parts = [project.project_no] + (['回收站'] if project.deleted_at else [])
        filename = export_filename('项目明细', parts, 'xlsx')
        path = os.path.join(out_dir, filename)
        with open(path, 'wb') as handle:
            handle.write(payload)
        audit_record('export.project', 'project', project.id, project.id,
                     after={'format': 'xlsx', 'file_name': filename,
                            'source': 'cli',
                            'sheets': ['项目信息', '费用科目', '付款节点',
                                       '付款申请', '付款记录', '验收记录']},
                     actor=(None, '命令行导出', 'system'), critical=False)
        db.session.commit()
        written.append((path, len(payload)))
        _print('[OK] 项目明细 %s（%.1f KB）' % (path, len(payload) / 1024.0))
    return written


def _report_reminders(projects):
    today = local_today()
    reminders = []
    for project in projects:
        if project.warranty_due_date:
            days = (project.warranty_due_date - today).days
            if days <= 60:
                reminders.append(('warranty', project, days))
        for node in project.nodes:
            # CR-06：申请口径——已申请未付清才提醒（不再看 status 文案）
            if node.unpaid_amount > 0:
                reminders.append(('pending_pay', node, None))
    return reminders


def _export_reports(app, projects, out_dir):
    """报表汇总数据与 /reports 页同口径（CR-06：未付=已申请−已付）。"""
    rows = []
    for project in projects:
        due = sum_money(n.due_amount for n in project.nodes)
        applied = sum_money(Decimal(n.applied_amount or 0) for n in project.nodes)
        invoice = sum_money(n.invoice_amount for n in project.nodes)
        paid = sum_money(n.paid_amount for n in project.nodes)
        unpaid = project.unpaid_amount
        unapplied = project.unapplied_amount
        overpaid_due = sum_money(
            [(Decimal(node.paid_amount or 0) - Decimal(node.due_amount or 0)).quantize(CENT)
             for node in project.nodes
             if Decimal(node.paid_amount or 0) > Decimal(node.due_amount or 0)])
        note_parts = []
        if project.over_paid_application > 0:
            note_parts.append('超付申请 %s' % ('￥' + format(
                project.over_paid_application, ',.2f')))
        if project.over_applied > 0:
            note_parts.append('超额申请 %s' % ('￥' + format(
                project.over_applied, ',.2f')))
        progress = ((paid / due * Decimal('100')).quantize(Decimal('0.1'))
                    if due else Decimal('0'))
        rows.append({'project': project, 'contract': project.total_amount(),
                     'due': due, 'applied': applied, 'invoice': invoice,
                     'paid': paid, 'unpaid': unpaid, 'unapplied': unapplied,
                     'unpaid_note': '；'.join(note_parts),
                     'progress': progress, 'overpaid_due': overpaid_due})
    totals = {
        'contract': sum_money(r['contract'] for r in rows),
        'due': sum_money(r['due'] for r in rows),
        'applied': sum_money(r['applied'] for r in rows),
        'invoice': sum_money(r['invoice'] for r in rows),
        'paid': sum_money(r['paid'] for r in rows),
        'unpaid': sum_money(r['unpaid'] for r in rows),
        'unapplied': sum_money(r['unapplied'] for r in rows),
        'overpaid_due': sum_money(r['overpaid_due'] for r in rows),
    }
    workbook = build_reports_workbook(rows, totals, _report_reminders(projects))
    payload = workbook_bytes(workbook)
    filename = export_filename('报表汇总', ['命令行导出'], 'xlsx')
    path = os.path.join(out_dir, filename)
    with open(path, 'wb') as handle:
        handle.write(payload)
    audit_record('export.reports', 'report', None, None,
                 after={'format': 'xlsx', 'file_name': filename,
                        'source': 'cli', 'projects': len(rows)},
                 actor=(None, '命令行导出', 'system'), critical=False)
    db.session.commit()
    _print('[OK] 报表汇总 %s（%.1f KB）' % (path, len(payload) / 1024.0))
    return [(path, len(payload))]


def _export_audit(app, out_dir, args):
    query = AuditLog.query
    if args.audit_project_id:
        query = query.filter(AuditLog.project_id == args.audit_project_id)
    if args.audit_actor:
        query = query.filter(AuditLog.actor_name.contains(args.audit_actor))
    if args.audit_action:
        query = query.filter(AuditLog.action == args.audit_action)
    date_from = date_to = None
    try:
        date_from = parse_date(args.audit_date_from, field='审计开始日期')
        date_to = parse_date(args.audit_date_to, field='审计结束日期')
    except ValidationError as exc:
        _err('审计筛选参数错误：' + '；'.join(exc.errors))
        return None
    if date_from:
        query = query.filter(AuditLog.ts >= local_date_start_utc(date_from))
    if date_to:
        query = query.filter(AuditLog.ts < local_date_end_utc(date_to))

    logs = query.order_by(AuditLog.ts.desc(), AuditLog.id.desc()).limit(50000).all()
    payload = audit_csv_bytes(logs)
    parts = []
    if args.audit_project_id:
        parts.append('项目%s' % args.audit_project_id)
    if args.audit_actor:
        parts.append(args.audit_actor)
    if args.audit_action:
        parts.append(args.audit_action)
    if args.audit_date_from or args.audit_date_to:
        parts.append('%s-%s' % (args.audit_date_from or '起始',
                                args.audit_date_to or '至今'))
    filename = export_filename('审计日志', parts or ['全量'], 'csv')
    path = os.path.join(out_dir, filename)
    with open(path, 'wb') as handle:
        handle.write(payload)
    audit_record('export.audit', 'audit', None, args.audit_project_id,
                 after={'format': 'csv', 'file_name': filename,
                        'source': 'cli', 'rows': len(logs)},
                 actor=(None, '命令行导出', 'system'), critical=False)
    db.session.commit()
    _print('[OK] 审计日志 %s（%d 行，%.1f KB）'
           % (path, len(logs), len(payload) / 1024.0))
    return [(path, len(payload))]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='导出项目明细/报表汇总（Excel）与审计日志（CSV）')
    parser.add_argument('--db', default=DEFAULT_DB, help='SQLite 数据库路径')
    parser.add_argument('--out', default=DEFAULT_OUT, help='输出目录')
    parser.add_argument('--project', action='append', default=[],
                        metavar='编号/ID', help='只导出指定项目，可重复')
    parser.add_argument('--include-deleted', action='store_true',
                        help='允许导出回收站中的项目')
    parser.add_argument('--no-reports', action='store_true', help='不导出报表汇总')
    parser.add_argument('--no-audit', action='store_true', help='不导出审计日志 CSV')
    parser.add_argument('--audit-project-id', type=int, default=None)
    parser.add_argument('--audit-actor', default=None)
    parser.add_argument('--audit-action', default=None)
    parser.add_argument('--audit-date-from', default=None, help='YYYY-MM-DD')
    parser.add_argument('--audit-date-to', default=None, help='YYYY-MM-DD')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    db_path = os.path.abspath(args.db)
    out_dir = os.path.abspath(args.out)
    if not os.path.isfile(db_path):
        _err('数据库不存在：%s（请先 python scripts/init_db.py）' % db_path)
        return 1
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as exc:
        _err('输出目录不可创建：%s（%s）' % (out_dir, exc))
        return 1

    app = build_app(db_path, os.path.dirname(db_path) or PROJECT_ROOT)
    written = []
    with app.app_context():
        try:
            if args.project:
                projects = _load_projects(app, args.project, args.include_deleted)
            else:
                query = Project.query
                if not args.include_deleted:
                    query = query.filter(Project.deleted_at.is_(None))
                projects = query.order_by(Project.created_at.desc()).all()
            if not projects:
                _print('[WARN] 没有可导出的项目')
            else:
                written += _export_projects(app, projects, out_dir)
            if not args.no_reports:
                written += _export_reports(app, projects, out_dir)
            if not args.no_audit:
                result = _export_audit(app, out_dir, args)
                if result is None:
                    return 2
                written += result
        except LookupError as exc:
            db.session.rollback()
            _err(str(exc))
            return 1
        except Exception as exc:
            db.session.rollback()
            _err('导出失败：%s' % exc)
            return 1

    _print('\n共导出 %d 个文件，目录：%s' % (len(written), out_dir))
    return 0 if written else 1


if __name__ == '__main__':
    sys.exit(main())

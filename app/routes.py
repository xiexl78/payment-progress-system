# -*- coding: utf-8 -*-
"""付款进度管理系统 - 路由与业务逻辑"""
import hashlib
import json
import os
import re
from datetime import date, timedelta, datetime
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, abort
from sqlalchemy import or_, update

from . import NODE_TEMPLATES, FLOW_STATUS
from .audit import action_options, record as audit_record, snapshot as audit_snapshot
from .contract_engine import extract_text, recognize_contract
from .models import (db, User, Project, ProjectItem, PaymentNode, PaymentApplication,
                     PaymentRecord, AcceptanceRecord, ContractFile, AuditLog)
from .money import CENT, MAX_MONEY, ZERO, fmt_money as money_fmt, mul_ratio, sum_money
from .validation import (PROJECT_STATUSES, NODE_TYPES, NODE_LABELS, TAX_RATES,
                         ValidationError, parse_amount, parse_date,
                         validate_contract_submission, validate_node_action,
                         validate_project_dates, validate_project_item_rows,
                         validate_text)

bp = Blueprint('main', __name__)

#: 质保期天数：与原始 Excel 公式 `N3+365*3` 保持一致（P1 口径待业务确认）
WARRANTY_DAYS = 365 * 3


# ---------- 工具函数 ----------

def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    return db.session.get(User, uid)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for('main.login'))
        return f(*args, **kwargs)
    return wrapper


@bp.app_context_processor
def inject_current_user():
    """向所有模板注入当前登录用户，供 base 布局与各页面判断角色"""
    try:
        return {'current_user': current_user()}
    except Exception:
        # 数据库不可用等场景下仍允许错误页渲染
        return {'current_user': None}


# ---------- 统一中文错误页（批次 4a） ----------

@bp.app_errorhandler(403)
def error_forbidden(error):
    message = getattr(error, 'description', None) or '您没有执行该操作的权限。'
    return render_template('error.html', code=403, title='无权限', message=message), 403


@bp.app_errorhandler(404)
def error_not_found(error):
    return render_template('error.html', code=404, title='页面不存在',
                           message='您访问的页面不存在或已被删除，请从左侧菜单重新进入。'), 404


@bp.app_errorhandler(405)
def error_method_not_allowed(error):
    return render_template('error.html', code=405, title='请求方式不支持',
                           message='该地址不支持当前请求方式，请从页面中的按钮重新操作。'), 405


@bp.app_errorhandler(413)
def error_payload_too_large(error):
    return render_template('error.html', code=413, title='上传文件过大',
                           message='上传文件超过 16MB 限制，请压缩或拆分后重试。'), 413


@bp.app_errorhandler(500)
def error_internal(error):
    return render_template('error.html', code=500, title='服务器内部错误',
                           message='系统处理请求时发生错误，请稍后重试；如持续出现请联系管理员。'), 500


def role_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u:
                return redirect(url_for('main.login'))
            if u.role not in roles:
                audit_record('auth.denied', object_type='endpoint', result='denied',
                             after={'endpoint': request.endpoint,
                                    'method': request.method,
                                    'required_roles': list(roles),
                                    'role': u.role},
                             critical=False)
                db.session.commit()
                flash('您没有该操作的权限', 'danger')
                return redirect(url_for('main.projects'))
            return f(*args, **kwargs)
        return wrapper
    return deco


def fmt_money(v):
    """格式化金额：Decimal + 千分位 + 两位小数（统一走 money 模块）。"""
    try:
        return money_fmt(v)
    except (TypeError, ValueError):
        return '0.00'


def make_apply_no():
    return 'AP' + datetime.now().strftime('%Y%m%d%H%M%S') + str(datetime.now().microsecond % 1000)


def sync_project_dates(project, collect=False):
    """根据试运行/验收日期联动节点触发日期与质保到期日（幂等、不 commit）。

    - 项目级 ``warranty_due_date`` = 验收日期 + WARRANTY_DAYS；
    - 进度款 ``trigger_date`` = 试运行日期；验收款 ``trigger_date`` = 验收日期；
      质保金 ``due_date`` = 验收日期 + WARRANTY_DAYS；
    - 已发生资金业务（申请/开票/付款）的节点**冻结日期**，只统计差异，不改写；
    - 日期被清空时，未发生业务的节点派生日期同步清空（修复单向联动）。

    Returns: 变更明细列表（collect=True 时），供差异回显/审计使用。
    """
    trial = project.trial_date
    acceptance = project.acceptance_date
    warranty_due = (acceptance + timedelta(days=WARRANTY_DAYS)) if acceptance else None
    project_changed = project.warranty_due_date != warranty_due
    if project_changed:
        project.warranty_due_date = warranty_due

    changes = []
    for node in project.nodes:
        if node.node_type in ('progress', 'acceptance'):
            desired = trial if node.node_type == 'progress' else acceptance
            field, old = 'trigger_date', node.trigger_date
        elif node.node_type == 'warranty':
            field, old = 'due_date', node.due_date
            desired = warranty_due
        else:
            continue
        if old == desired:
            continue
        if node.has_business:
            # 已发生资金业务：冻结日期，不静默改账（只记录差异）
            if collect:
                changes.append({'node_id': node.id, 'node_type': node.node_type,
                                'field': field, 'old': _date_str(old),
                                'new': _date_str(desired), 'frozen': True})
            continue
        setattr(node, field, desired)
        if collect:
            changes.append({'node_id': node.id, 'node_type': node.node_type,
                            'field': field, 'old': _date_str(old),
                            'new': _date_str(desired), 'frozen': False})
    if collect:
        return changes
    return None


def _date_str(value):
    return value.isoformat() if value else None


# ---------- 审计快照辅助 ----------

_PROJECT_AUDIT_FIELDS = ('id', 'project_no', 'name', 'status', 'trial_date',
                         'acceptance_date', 'warranty_due_date', 'remark',
                         'created_by', 'updated_by')
_ITEM_AUDIT_FIELDS = ('id', 'project_id', 'item_name', 'tax_rate',
                      'original_amount', 'additional_amount', 'updated_by')
_NODE_AUDIT_FIELDS = ('id', 'project_id', 'item_id', 'node_type', 'node_name',
                      'ratio', 'calc_base', 'due_amount', 'due_date', 'trigger_date',
                      'invoice_amount', 'paid_amount', 'status', 'updated_by')


def _project_snapshot(project):
    if project is None:
        return None
    data = audit_snapshot(project, _PROJECT_AUDIT_FIELDS)
    try:
        data['total_amount'] = format(Decimal(project.total_amount()), 'f')
        data['item_count'] = len(project.items)
        data['node_count'] = len(project.nodes)
    except Exception:
        pass
    return data


def _item_snapshot(item):
    return audit_snapshot(item, _ITEM_AUDIT_FIELDS)


def _node_snapshot(node):
    return audit_snapshot(node, _NODE_AUDIT_FIELDS)


def _now_utc():
    return datetime.utcnow()


def _operator_id():
    user = current_user()
    return user.id if user else None


# ---------- 认证 ----------

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user():
        return redirect(url_for('main.projects'))
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            if not user.is_active:
                audit_record('auth.login.failed', 'user', user.id, result='failed',
                             after={'username': username, 'reason': '账号已停用'})
                db.session.commit()
                flash('账号已停用，请联系管理员', 'danger')
                return render_template('login.html')
            session['user_id'] = user.id
            session.permanent = True
            audit_record('auth.login.success', 'user', user.id,
                         after={'username': username})
            db.session.commit()
            flash(f'欢迎回来，{user.display_name}', 'success')
            return redirect(url_for('main.projects'))
        audit_record('auth.login.failed', 'user', user.id if user else None,
                     result='failed', after={'username': username})
        db.session.commit()
        flash('用户名或密码错误', 'danger')
    return render_template('login.html')


@bp.route('/logout', methods=['POST'])
def logout():
    user = current_user()
    if user:
        audit_record('auth.logout', 'user', user.id, after={'username': user.username})
        db.session.commit()
    session.clear()
    flash('已退出登录', 'info')
    return redirect(url_for('main.login'))


# ---------- 项目管理 ----------

@bp.route('/')
@login_required
def index():
    return redirect(url_for('main.projects'))


@bp.route('/projects')
@login_required
def projects():
    keyword = (request.args.get('keyword') or '').strip()
    status = (request.args.get('status') or '').strip()
    q = Project.query
    if keyword:
        q = q.filter(or_(Project.name.contains(keyword), Project.project_no.contains(keyword)))
    if status:
        q = q.filter(Project.status == status)
    project_list = q.order_by(Project.created_at.desc()).all()
    stats = {
        'total': len(project_list),
        'ongoing': sum(1 for p in project_list if p.status == '进行中'),
        'done': sum(1 for p in project_list if p.status == '已完成'),
        'amount': sum(p.total_amount() for p in project_list),
    }
    return render_template('projects.html', projects=project_list, stats=stats,
                           keyword=keyword, status=status)


@bp.route('/projects/new', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'operator')
def project_new():
    templates = NODE_TEMPLATES
    if request.method == 'POST':
        project_no = (request.form.get('project_no') or '').strip()
        name = (request.form.get('name') or '').strip()
        remark = (request.form.get('remark') or '').strip()
        if not project_no or not name:
            flash('项目编号与项目名称必填', 'danger')
            return render_template('project_form.html', templates=templates, project=None)
        if Project.query.filter_by(project_no=project_no).first():
            flash('项目编号已存在', 'danger')
            return render_template('project_form.html', templates=templates, project=None)

        try:
            item_rows = validate_project_item_rows(request.form)
        except ValidationError as exc:
            for msg in exc.errors:
                flash(msg, 'danger')
            return render_template('project_form.html', templates=templates, project=None)

        project = Project(project_no=project_no, name=name, remark=remark,
                          status='进行中', created_by=current_user().id,
                          updated_by=current_user().id)
        db.session.add(project)
        db.session.flush()

        items = []
        for row in item_rows:
            item = ProjectItem(project_id=project.id, item_name=row['item_name'],
                               tax_rate=row['tax_rate'],
                               original_amount=row['original_amount'],
                               additional_amount=row['additional_amount'],
                               updated_by=current_user().id)
            db.session.add(item)
            items.append(item)
        db.session.flush()

        # 为每个科目生成 4 个付款节点
        for it in items:
            for t in NODE_TEMPLATES:
                node = PaymentNode(project_id=project.id, item_id=it.id,
                                   node_type=t['node_type'], node_name=t['node_name'],
                                   ratio=Decimal(str(t['ratio'])), calc_base=t['calc_base'],
                                   status='未触发', updated_by=current_user().id)
                node.recalc()
                db.session.add(node)
        sync_project_dates(project)
        audit_record('project.create', 'project', project.id, project.id,
                     after=_project_snapshot(project), critical=False)
        db.session.commit()
        flash(f'项目「{name}」创建成功，已自动生成付款节点', 'success')
        return redirect(url_for('main.project_detail', project_id=project.id))
    return render_template('project_form.html', templates=templates, project=None)


@bp.route('/projects/<int:project_id>')
@login_required
def project_detail(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    # 按节点类型聚合展示，便于跨科目合计（Decimal 口径）
    node_groups = {t['node_type']: {'meta': t, 'nodes': [], 'sum_due': ZERO,
                                    'sum_invoice': ZERO, 'sum_paid': ZERO}
                   for t in NODE_TEMPLATES}
    for node in project.nodes:
        g = node_groups.get(node.node_type)
        if g is None:
            continue
        g['nodes'].append(node)
        g['sum_due'] += Decimal(node.due_amount or 0)
        g['sum_invoice'] += Decimal(node.invoice_amount or 0)
        g['sum_paid'] += Decimal(node.paid_amount or 0)

    acceptances = AcceptanceRecord.query.filter_by(project_id=project.id).order_by(AcceptanceRecord.acceptance_date.desc()).all()
    return render_template('project_detail.html', project=project, node_groups=node_groups,
                           acceptances=acceptances, templates=NODE_TEMPLATES)


@bp.route('/projects/<int:project_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'operator')
def project_edit(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    if request.method == 'POST':
        try:
            name = validate_text(request.form.get('name') or project.name,
                                 field='项目名称', required=True, max_len=256)
            requested_status = (request.form.get('status') or project.status).strip()
            if current_user().role != 'admin' and requested_status != project.status:
                raise ValidationError('仅管理员可以修改项目状态')
            if requested_status not in PROJECT_STATUSES:
                raise ValidationError('项目状态取值不合法')
            status = requested_status if current_user().role == 'admin' else project.status
            trial = parse_date(request.form.get('trial_date'), field='试运行日期')
            acceptance = parse_date(request.form.get('acceptance_date'), field='验收日期')
            validate_project_dates(trial, acceptance)
        except ValidationError as exc:
            for msg in exc.errors:
                flash(msg, 'danger')
            return redirect(url_for('main.project_edit', project_id=project.id))
        before = _project_snapshot(project)
        project.name = name
        project.remark = (request.form.get('remark') or '').strip()
        project.status = status
        project.trial_date = trial
        project.acceptance_date = acceptance
        project.updated_by = current_user().id
        sync_project_dates(project)
        audit_record('project.edit', 'project', project.id, project.id,
                     before=before, after=_project_snapshot(project), critical=False)
        db.session.commit()
        flash('项目信息已更新', 'success')
        return redirect(url_for('main.project_detail', project_id=project.id))
    return render_template('project_form.html', templates=NODE_TEMPLATES, project=project)


@bp.route('/projects/<int:project_id>/items', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def item_add(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    item_name = (request.form.get('item_name') or '').strip()
    if not item_name:
        flash('科目名称必填', 'danger')
        return redirect(url_for('main.project_detail', project_id=project.id))
    try:
        item_name = validate_text(item_name, field='科目名称', required=True, max_len=128)
        tax_rate = validate_text(request.form.get('tax_rate'), field='tax_rate',
                                 required=False, max_len=32, allowed=TAX_RATES)
        original = parse_amount(request.form.get('original_amount'),
                                field='原金额', allow_zero=True)
        additional = parse_amount(request.form.get('additional_amount'),
                                  field='增补金额', required=False)
    except ValidationError as exc:
        for msg in exc.errors:
            flash(msg, 'danger')
        return redirect(url_for('main.project_detail', project_id=project.id))
    item = ProjectItem(project_id=project.id, item_name=item_name,
                       tax_rate=tax_rate,
                       original_amount=original,
                       additional_amount=additional,
                       updated_by=current_user().id)
    db.session.add(item)
    db.session.flush()
    for t in NODE_TEMPLATES:
        node = PaymentNode(project_id=project.id, item_id=item.id,
                           node_type=t['node_type'], node_name=t['node_name'],
                           ratio=Decimal(str(t['ratio'])), calc_base=t['calc_base'],
                           status='未触发', updated_by=current_user().id)
        node.recalc()
        db.session.add(node)
    sync_project_dates(project)
    audit_record('item.create', 'item', item.id, project.id,
                 after=_item_snapshot(item), critical=False)
    db.session.commit()
    flash(f'科目「{item_name}」已添加，节点已生成', 'success')
    return redirect(url_for('main.project_detail', project_id=project.id))


@bp.route('/projects/<int:project_id>/delete', methods=['POST'])
@login_required
@role_required('admin')
def project_delete(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    before_snapshot = _project_snapshot(project)
    audit_record('project.delete', 'project', project.id, project.id,
                 before=before_snapshot, result='ok')
    # 级联清理关联数据（bulk delete 不触发 ORM cascade，按依赖顺序手动清理）
    node_ids = [n.id for n in PaymentNode.query.filter_by(project_id=project.id).all()]
    if node_ids:
        PaymentRecord.query.filter(PaymentRecord.node_id.in_(node_ids)).delete(synchronize_session=False)
        PaymentApplication.query.filter(PaymentApplication.node_id.in_(node_ids)).delete(synchronize_session=False)
    PaymentNode.query.filter_by(project_id=project.id).delete(synchronize_session=False)
    ProjectItem.query.filter_by(project_id=project.id).delete(synchronize_session=False)
    AcceptanceRecord.query.filter_by(project_id=project.id).delete(synchronize_session=False)
    # 解除合同文件的 project_id 绑定
    for cf in ContractFile.query.filter_by(project_id=project.id).all():
        cf.project_id = None
        cf.status = 'recognized'
    db.session.delete(project)
    db.session.commit()
    flash('项目「%s」已删除' % project.name, 'success')
    return redirect(url_for('main.projects'))


@bp.route('/nodes/<int:node_id>/update', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def node_update(node_id):
    node = db.session.get(PaymentNode, node_id)
    if not node:
        abort(404)
    detail_url = url_for('main.project_detail', project_id=node.project_id)
    actor = current_user()
    action = request.form.get('action')
    before_snapshot = _node_snapshot(node)
    try:
        if action == 'apply':
            parsed = validate_node_action(node, 'apply', request.form)
            invoice_no = (request.form.get('invoice_no') or '').strip()
            desc = (request.form.get('description') or '').strip()
            new_status = '已申请' if invoice_no else '未开票已申请'
            # 原子状态迁移：只有「未触发」可发起申请，并发/重复提交只有一个成功
            result = db.session.execute(
                update(PaymentNode)
                .where(PaymentNode.id == node.id,
                       PaymentNode.status == '未触发')
                .values(status=new_status,
                        invoice_amount=PaymentNode.invoice_amount + parsed['invoice_amount'],
                        updated_at=_now_utc(), updated_by=actor.id))
            if result.rowcount != 1:
                db.session.rollback()
                flash('发起申请失败：该节点已被他人处理或状态已变化，请刷新后重试', 'danger')
                return redirect(detail_url)
            db.session.refresh(node)
            app_obj = PaymentApplication(
                node_id=node.id,
                apply_no=make_apply_no(),
                apply_amount=parsed['apply_amount'],
                invoice_amount=parsed['invoice_amount'],
                invoice_no=invoice_no or None,
                description=desc or None,
                status=new_status,
                applicant_id=actor.id,
                applicant_name=actor.display_name,
            )
            db.session.add(app_obj)
            db.session.flush()
            audit_record('node.apply', 'node', node.id, node.project_id,
                         before=before_snapshot,
                         after=dict(_node_snapshot(node),
                                    apply_no=app_obj.apply_no,
                                    apply_amount=app_obj.apply_amount))
            db.session.commit()
            flash('%s 已发起付款申请（%s）' % (node.node_name, app_obj.apply_no), 'success')

        elif action == 'invoice':
            parsed = validate_node_action(node, 'invoice', request.form)
            invoice_no = (request.form.get('invoice_no') or '').strip()
            # 原子累加开票金额并迁移状态，条件写入保证不超应付
            result = db.session.execute(
                update(PaymentNode)
                .where(PaymentNode.id == node.id,
                       PaymentNode.status == '未开票已申请',
                       PaymentNode.invoice_amount + parsed['invoice_amount'] <= PaymentNode.due_amount)
                .values(invoice_amount=PaymentNode.invoice_amount + parsed['invoice_amount'],
                        status='已申请',
                        updated_at=_now_utc(), updated_by=actor.id))
            if result.rowcount != 1:
                db.session.rollback()
                flash('发票登记失败：节点状态已变化或累计开票将超过应付，请刷新后重试', 'danger')
                return redirect(detail_url)
            db.session.refresh(node)
            # 回填最近一笔申请单的发票信息（按申请时间排序，避免取错单）
            app_pending = (PaymentApplication.query
                           .filter_by(node_id=node.id)
                           .order_by(PaymentApplication.apply_time.desc(),
                                     PaymentApplication.id.desc())
                           .first())
            if app_pending:
                if invoice_no:
                    app_pending.invoice_no = invoice_no
                app_pending.invoice_amount = (Decimal(app_pending.invoice_amount or 0)
                                              + parsed['invoice_amount'])
                app_pending.status = '已申请'
            audit_record('node.invoice', 'node', node.id, node.project_id,
                         before=before_snapshot,
                         after=dict(_node_snapshot(node),
                                    invoice_no=invoice_no,
                                    invoice_amount_delta=parsed['invoice_amount']))
            db.session.commit()
            flash('%s 发票信息已登记' % node.node_name, 'success')

        elif action == 'pay':
            parsed = validate_node_action(node, 'pay', request.form)
            pay_date = parse_date(request.form.get('pay_date'), field='付款日期') or date.today()
            pay_method = (request.form.get('pay_method') or '').strip()
            voucher = (request.form.get('voucher_no') or '').strip()
            # 原子累加已付金额：数据库端条件保证累计不超应付、状态前置为已申请
            result = db.session.execute(
                update(PaymentNode)
                .where(PaymentNode.id == node.id,
                       PaymentNode.status == '已申请',
                       PaymentNode.paid_amount + parsed['paid_amount'] <= PaymentNode.due_amount)
                .values(paid_amount=PaymentNode.paid_amount + parsed['paid_amount'],
                        status='已付款',
                        updated_at=_now_utc(), updated_by=actor.id))
            if result.rowcount != 1:
                db.session.rollback()
                flash('付款失败：节点状态已变化或付款金额超过未付应付，请刷新后重试', 'danger')
                return redirect(detail_url)
            db.session.refresh(node)
            app_obj = (PaymentApplication.query
                       .filter_by(node_id=node.id)
                       .order_by(PaymentApplication.apply_time.desc(),
                                 PaymentApplication.id.desc())
                       .first())
            rec = PaymentRecord(
                application_id=app_obj.id if app_obj else None,
                node_id=node.id,
                paid_amount=parsed['paid_amount'],
                pay_date=pay_date,
                pay_method=pay_method or None,
                voucher_no=voucher or None,
                operator_id=actor.id,
                operator_name=actor.display_name,
            )
            db.session.add(rec)
            db.session.flush()
            if app_obj:
                app_obj.status = '已付款'
                app_obj.invoice_amount = Decimal(node.invoice_amount or 0)
            audit_record('node.pay', 'node', node.id, node.project_id,
                         before=before_snapshot,
                         after=dict(_node_snapshot(node),
                                    record_id=rec.id,
                                    pay_amount=rec.paid_amount,
                                    pay_date=_date_str(rec.pay_date),
                                    pay_method=rec.pay_method))
            db.session.commit()
            flash('%s 已登记实际付款 %s 元' % (node.node_name, fmt_money(parsed['paid_amount'])), 'success')
    except ValidationError as exc:
        db.session.rollback()
        for msg in exc.errors:
            flash(msg, 'danger')
    return redirect(detail_url)


@bp.route('/projects/<int:project_id>/acceptance', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def acceptance_add(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    try:
        acc_date = parse_date(request.form.get('acceptance_date'),
                              field='验收日期', required=True)
        validate_project_dates(project.trial_date, acc_date)
    except ValidationError as exc:
        for msg in exc.errors:
            flash(msg, 'danger')
        return redirect(url_for('main.project_detail', project_id=project.id))
    rec = AcceptanceRecord(
        project_id=project.id,
        acceptance_date=acc_date,
        conclusion=(request.form.get('conclusion') or '').strip() or None,
        acceptor=(request.form.get('acceptor') or '').strip() or None,
        remark=(request.form.get('remark') or '').strip() or None,
    )
    db.session.add(rec)
    db.session.flush()
    before = {'acceptance_date': _date_str(project.acceptance_date),
              'warranty_due_date': _date_str(project.warranty_due_date)}
    project.acceptance_date = acc_date
    project.updated_by = current_user().id
    sync_project_dates(project)
    audit_record('acceptance.add', 'acceptance', rec.id, project.id, before=before,
                 after={'acceptance_date': _date_str(project.acceptance_date),
                        'warranty_due_date': _date_str(project.warranty_due_date),
                        'acceptor': rec.acceptor, 'conclusion': rec.conclusion},
                 critical=False)
    db.session.commit()
    flash(f'验收记录已登记：{acc_date}，质保到期日自动推算为 {project.warranty_due_date}', 'success')
    return redirect(url_for('main.project_detail', project_id=project.id))


@bp.route('/projects/<int:project_id>/trial', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def trial_set(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    try:
        trial_date = parse_date(request.form.get('trial_date'),
                                field='试运行日期', required=True)
        validate_project_dates(trial_date, project.acceptance_date)
    except ValidationError as exc:
        for msg in exc.errors:
            flash(msg, 'danger')
        return redirect(url_for('main.project_detail', project_id=project.id))
    before = {'trial_date': _date_str(project.trial_date)}
    project.trial_date = trial_date
    project.updated_by = current_user().id
    sync_project_dates(project)
    audit_record('project.trial', 'project', project.id, project.id, before=before,
                 after={'trial_date': _date_str(project.trial_date)}, critical=False)
    db.session.commit()
    flash(f'试运行日期已登记：{trial_date}，进度款节点已联动', 'success')
    return redirect(url_for('main.project_detail', project_id=project.id))


@bp.route('/projects/<int:project_id>/status', methods=['POST'])
@login_required
@role_required('admin')
def project_status(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    status = request.form.get('status')
    if status in ('进行中', '已完成', '已归档'):
        before = {'status': project.status}
        project.status = status
        project.updated_by = current_user().id
        audit_record('project.status', 'project', project.id, project.id,
                     before=before, after={'status': project.status}, critical=False)
        db.session.commit()
        flash(f'项目状态已更新为「{status}」', 'success')
    return redirect(url_for('main.project_detail', project_id=project.id))


# ---------- 合同识别 ----------

ALLOWED_CONTRACT_EXTS = {
    'pdf': 'pdf', 'docx': 'docx', 'txt': 'txt', 'md': 'md',
    'jpg': 'img', 'jpeg': 'img', 'png': 'img', 'bmp': 'img', 'webp': 'img',
}
CONTRACT_NODE_ORDER = ('first', 'progress', 'acceptance', 'warranty')
CALC_BASE_LABELS = {'first': '按原金额', 'progress': '按总金额', 'acceptance': '按总金额', 'warranty': '按总金额'}


def contract_upload_dir():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    d = os.path.join(base_dir, 'instance', 'uploads')
    os.makedirs(d, exist_ok=True)
    return d


def _next_contract_project_no():
    """自动生成项目编号：PJ-CT-YYYYMMDD-序号（当天不重复）"""
    today = date.today().strftime('%Y%m%d')
    prefix = 'PJ-CT-' + today + '-'
    row = db.session.query(Project.project_no).filter(
        Project.project_no.like(prefix + '%')).order_by(Project.project_no.desc()).first()
    seq = int(row[0].rsplit('-', 1)[-1]) + 1 if row and row[0].rsplit('-', 1)[-1].isdigit() else 1
    return prefix + '%03d' % seq


def _parse_contract_result(contract):
    """解析 result_json，返回 dict；损坏时返回 None"""
    if not contract.result_json:
        return None
    try:
        return json.loads(contract.result_json)
    except (ValueError, TypeError):
        return None


def _pct_str(ratio):
    """比例（小数，如 0.30）格式化为 '30%'（Decimal，去掉多余零）。"""
    v = (Decimal(str(ratio)) * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    text = format(v, 'f').rstrip('0').rstrip('.')
    return text + '%'


def _pct_plain(value):
    """比例数字格式化为两位小数字符串（如 '30.00'），用于表单回填。"""
    return format(Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f')


@bp.route('/contracts')
@login_required
def contracts():
    contract_list = ContractFile.query.order_by(ContractFile.created_at.desc()).all()
    rows = []
    for c in contract_list:
        result = _parse_contract_result(c)
        amount = float((result or {}).get('contract_amount') or 0)
        creator = db.session.get(User, c.created_by) if c.created_by else None
        rows.append({
            'contract': c,
            'amount': amount if amount else None,
            'warnings': (result or {}).get('warnings') or [],
            'creator_name': creator.display_name if creator else '',
        })
    return render_template('contracts.html', rows=rows)


@bp.route('/contracts/upload', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def contract_upload():
    file = request.files.get('file')
    if not file or not file.filename:
        flash('请选择要上传的合同文件', 'danger')
        return redirect(url_for('main.contracts'))
    ext = (file.filename.rsplit('.', 1)[-1] if '.' in file.filename else '').lower()
    if ext not in ALLOWED_CONTRACT_EXTS:
        flash('不支持的文件类型：%s（支持 PDF / Word(docx) / TXT / MD / 图片）' % (ext or '未知'), 'danger')
        return redirect(url_for('main.contracts'))
    file_type = ALLOWED_CONTRACT_EXTS[ext]

    contract = ContractFile(
        file_name=file.filename, stored_path='', file_type=file_type,
        file_size=0, engine='rules', status='uploaded', created_by=current_user().id,
    )
    db.session.add(contract)
    db.session.flush()

    # 以记录 id 命名保存文件，避免原始文件名中的非法字符；仅存相对路径
    safe_name = f'{contract.id}.{ext}'
    stored = os.path.join(contract_upload_dir(), safe_name)
    file.save(stored)
    contract.stored_path = os.path.join('uploads', safe_name)
    contract.file_size = os.path.getsize(stored) if os.path.exists(stored) else 0

    text, extract_msg = extract_text(stored, file_type)
    if text is None:
        contract.status = 'failed'
        contract.error_msg = extract_msg or '文本提取失败'
        audit_record('contract.upload', 'contract', contract.id, result='failed',
                     after={'file_name': contract.file_name, 'status': 'failed',
                            'error': contract.error_msg}, critical=False)
        db.session.commit()
        flash('文本提取失败：%s' % (extract_msg or '未知错误'), 'danger')
        return redirect(url_for('main.contracts'))

    result, rec_msg = recognize_contract(text, file.filename)
    contract.raw_text = text
    contract.result_json = json.dumps(result, ensure_ascii=False)
    contract.engine = result.get('engine', 'rules')
    contract.status = 'recognized'
    contract.error_msg = rec_msg
    audit_record('contract.upload', 'contract', contract.id,
                 after={'file_name': contract.file_name, 'status': 'recognized',
                        'engine': contract.engine,
                        'contract_amount': (result or {}).get('contract_amount')},
                 critical=False)
    db.session.commit()
    flash('合同识别完成：%s' % (file.filename), 'success')
    return redirect(url_for('main.contract_confirm', contract_id=contract.id))


# ---------- 合同确认：现值回填 / 差异二次确认 / 就地更新 ----------

#: 会改变付款金额口径的差异字段（已发生业务的项目禁止直接应用）
_FINANCIAL_FIELDS = {'original_amount', 'additional_amount'}


def _is_confirmed_update(contract):
    """是否为「已确认合同的修改」：以项目是否存在为准。

    不再只看 ``contract.status == 'confirmed'``：合同重识别失败会把 status 改成
    failed，旧判断会误走「新建项目」，产生重复项目。
    """
    if not contract.project_id:
        return False
    return db.session.get(Project, contract.project_id) is not None


def _contract_display_name(contract):
    if contract.contract_name:
        return contract.contract_name
    name = contract.file_name or ''
    for ext in ('.pdf', '.docx', '.txt', '.md', '.jpg', '.jpeg', '.png', '.bmp', '.webp'):
        if name.lower().endswith(ext):
            return name[:-len(ext)]
    return name


def _payment_method_from_project(project):
    """从历史 remark 中回填付款条件（付款方式尚无结构化列）。"""
    m = re.search(r'付款条件：([^；;]*)', project.remark or '')
    return m.group(1).strip() if m else ''


def _project_contract_values(project):
    """读取项目现值（合同价款科目 + 4 节点），返回表单回填用的字符串字典。"""
    items = list(project.items)
    item = items[0] if items else None
    node_map = {n.node_type: n for n in project.nodes}
    ratios = {}
    for nt in NODE_TYPES:
        node = node_map.get(nt)
        ratios[nt] = _pct_plain(Decimal(node.ratio or 0) * 100) if node else '0.00'
    return {
        'contract_name': project.name,
        'project_no': project.project_no,
        'original_amount': fmt_money(Decimal(item.original_amount or 0)) if item else '0.00',
        'additional_amount': fmt_money(Decimal(item.additional_amount or 0)) if item else '0.00',
        'tax_rate': (item.tax_rate or '') if item else '',
        'payment_method': _payment_method_from_project(project),
        'ratios_pct': ratios,
        'amounts': {nt: (fmt_money(Decimal(node_map[nt].due_amount or 0)) if nt in node_map else '0.00')
                    for nt in NODE_TYPES},
    }


def _recognized_contract_values(contract, result):
    """首次确认：用识别结果预填表单（识别是草稿，提交后以服务端校验为准）。"""
    try:
        original = parse_amount(result.get('contract_amount'), field='识别金额', required=False)
    except ValidationError:
        original = ZERO
    node_map = {nd.get('node_type'): nd for nd in (result.get('nodes') or [])}
    defaults = {'first': '30.00', 'progress': '52.00',
                'acceptance': '15.00', 'warranty': '3.00'}
    ratios = {}
    for nt in NODE_TYPES:
        nd = node_map.get(nt)
        ratios[nt] = defaults[nt]
        if nd and nd.get('ratio') is not None:
            try:
                pct = (Decimal(str(nd['ratio'])) * 100).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP)
                ratios[nt] = format(pct, 'f')
            except (ArithmeticError, ValueError):
                pass
    return {
        'contract_name': _contract_display_name(contract),
        'project_no': '',
        'original_amount': fmt_money(original),
        'additional_amount': '0.00',
        'tax_rate': result.get('tax_rate') or '',
        'payment_method': result.get('payment_method') or '',
        'ratios_pct': ratios,
        'amounts': {},
    }


def _raw_contract_form_values(form):
    """回显用户提交的原始字符串（校验失败时用，避免丢失输入）。"""
    return {
        'contract_name': form.get('contract_name') or '',
        'project_no': form.get('project_no') or '',
        'original_amount': form.get('original_amount') or '',
        'additional_amount': form.get('additional_amount') or '0.00',
        'tax_rate': form.get('tax_rate') or '',
        'payment_method': form.get('payment_method') or '',
        'ratios_pct': {nt: (form.get('ratio_' + nt) or '') for nt in NODE_TYPES},
        'amounts': {},
    }


def _dec_str(value):
    try:
        return Decimal(str(value).replace(',', '').strip()).quantize(CENT)
    except (ArithmeticError, ValueError):
        return ZERO


def _signed_money(value):
    return ('+' if value > 0 else '') + fmt_money(value)


def _diff_contract_values(current, submission):
    """计算「项目现值 → 本次提交值」的逐字段差异（供二次确认页展示）。"""
    diffs = []
    cur_original = _dec_str(current.get('original_amount'))
    if cur_original != submission.original_amount:
        diffs.append({'field': 'original_amount', 'label': '原金额',
                      'old': fmt_money(cur_original),
                      'new': fmt_money(submission.original_amount),
                      'delta': _signed_money(submission.original_amount - cur_original)})
    cur_additional = _dec_str(current.get('additional_amount'))
    if cur_additional != submission.additional_amount:
        diffs.append({'field': 'additional_amount', 'label': '增补金额',
                      'old': fmt_money(cur_additional),
                      'new': fmt_money(submission.additional_amount),
                      'delta': _signed_money(submission.additional_amount - cur_additional)})
    if (current.get('contract_name') or '') != submission.contract_name:
        diffs.append({'field': 'contract_name', 'label': '合同/项目名称',
                      'old': current.get('contract_name') or '—',
                      'new': submission.contract_name, 'delta': ''})
    if (current.get('tax_rate') or '') != submission.tax_rate:
        diffs.append({'field': 'tax_rate', 'label': '税率',
                      'old': current.get('tax_rate') or '—',
                      'new': submission.tax_rate or '—', 'delta': ''})
    if (current.get('payment_method') or '') != submission.payment_method:
        diffs.append({'field': 'payment_method', 'label': '付款方式',
                      'old': current.get('payment_method') or '—',
                      'new': submission.payment_method or '—', 'delta': ''})
    cur_ratios = current.get('ratios_pct') or {}
    for nt in NODE_TYPES:
        cur_pct = _dec_str(cur_ratios.get(nt))
        new_pct = submission.ratios_pct[nt]
        if cur_pct != new_pct:
            delta = new_pct - cur_pct
            diffs.append({'field': 'ratio_' + nt, 'label': NODE_LABELS[nt] + '比例',
                          'old': _pct_plain(cur_pct) + '%',
                          'new': _pct_plain(new_pct) + '%',
                          'delta': ('+' if delta > 0 else '') + _pct_plain(delta) + 'pp'})
    return diffs


def _diff_hash(contract, diff):
    """差异确认凭据：服务端按当前现值重算，客户端无法伪造。"""
    payload = json.dumps({'contract_id': contract.id, 'diff': diff},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _contract_blockers(project):
    """合同回改阻断条件：已发生业务的节点/工程日期，金额必须走变更单。"""
    blockers = []
    items = list(project.items)
    if len(items) != 1:
        blockers.append('项目包含 %d 个费用科目，合同回改只支持单科目「合同价款」项目' % len(items))
    for node in project.nodes:
        if node.has_business:
            blockers.append('节点「%s」已有申请/开票/付款记录，应付金额已冻结' % node.node_name)
        elif node.trigger_date is not None or node.due_date is not None:
            blockers.append('节点「%s」已触发或已生成应付日期，金额已冻结' % node.node_name)
    if project.trial_date or project.acceptance_date:
        blockers.append('项目已登记试运行/验收日期，改价可能影响应付日期，需走变更单')
    return blockers


def _contract_remark(submission, file_name):
    return ('由合同识别生成：合同金额 %s 元；付款条件：%s；税率：%s；原始文件：%s'
            % (fmt_money(submission.contract_total),
               submission.payment_method or '未识别',
               submission.tax_rate or '未识别', file_name))


def _ratio_fraction(pct):
    """百分数 → 节点比例小数（Numeric(6,4)，例如 30.00% → 0.3000）。"""
    return (Decimal(pct) / Decimal('100')).quantize(Decimal('0.0001'),
                                                    rounding=ROUND_HALF_UP)


def _apply_contract_values(project, submission, contract, *, update_amounts=True):
    """就地更新项目口径：只改基线，不触碰节点状态/开票/付款，不删除任何节点。"""
    operator_id = _operator_id()
    project.name = submission.contract_name
    project.remark = _contract_remark(submission, contract.file_name)
    project.updated_by = operator_id
    if not update_amounts:
        return
    items = list(project.items)
    if items:
        item = items[0]
        item.original_amount = submission.original_amount
        item.additional_amount = submission.additional_amount
        item.tax_rate = submission.tax_rate or None
        item.updated_by = operator_id
    node_map = {n.node_type: n for n in project.nodes}
    for nt in NODE_TYPES:
        node = node_map.get(nt)
        if node is None:
            continue
        ratio = _ratio_fraction(submission.ratios_pct[nt])
        node.ratio = ratio
        node.node_name = _node_name(nt, ratio)
        node.calc_base = 'original' if nt == 'first' else 'total'
        node.due_amount = submission.node_amounts[nt]
        node.updated_by = operator_id
    sync_project_dates(project)


def _create_contract_project(submission, contract):
    """首次确认：创建项目 + 单科目 + 4 节点（金额来自服务端校验结果）。"""
    operator_id = _operator_id()
    project = Project(
        project_no=submission.project_no, name=submission.contract_name,
        status='进行中', remark=_contract_remark(submission, contract.file_name),
        created_by=operator_id, updated_by=operator_id,
    )
    db.session.add(project)
    db.session.flush()
    item = ProjectItem(project_id=project.id, item_name='合同价款',
                       tax_rate=submission.tax_rate or None,
                       original_amount=submission.original_amount,
                       additional_amount=submission.additional_amount,
                       updated_by=operator_id)
    db.session.add(item)
    db.session.flush()
    for nt in NODE_TYPES:
        ratio = _ratio_fraction(submission.ratios_pct[nt])
        node = PaymentNode(project_id=project.id, item_id=item.id, node_type=nt,
                           node_name=_node_name(nt, ratio), ratio=ratio,
                           calc_base='original' if nt == 'first' else 'total',
                           due_amount=submission.node_amounts[nt], status='未触发',
                           updated_by=operator_id)
        db.session.add(node)
    sync_project_dates(project)
    return project


def _render_confirm(contract, result, *, values, is_update, project=None,
                    errors=None, diff=None, blockers=None, confirm_token=''):
    return render_template('contract_confirm.html', contract=contract,
                           result=result, values=values, is_update=is_update,
                           project=project, errors=errors or [], diff=diff or [],
                           blockers=blockers or [], confirm_token=confirm_token,
                           node_order=CONTRACT_NODE_ORDER)


@bp.route('/contracts/<int:contract_id>/confirm', methods=['GET'])
@login_required
def contract_confirm(contract_id):
    contract = db.session.get(ContractFile, contract_id)
    if not contract:
        abort(404)
    result = _parse_contract_result(contract)
    if contract.status == 'failed' or result is None:
        flash('该合同识别结果不可用，请重新上传或重新识别', 'danger')
        return redirect(url_for('main.contracts'))
    is_update = _is_confirmed_update(contract)
    project = db.session.get(Project, contract.project_id) if contract.project_id else None
    if contract.status == 'confirmed' and project is None:
        flash('该合同已确认但关联项目不存在，请重新识别或重新上传合同', 'danger')
        return redirect(url_for('main.contracts'))
    if is_update:
        values = _project_contract_values(project)
        blockers = _contract_blockers(project)
    else:
        values = _recognized_contract_values(contract, result)
        blockers = []
    return _render_confirm(contract, result, values=values, is_update=is_update,
                           project=project, blockers=blockers)


@bp.route('/contracts/<int:contract_id>/confirm', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def contract_confirm_submit(contract_id):
    contract = db.session.get(ContractFile, contract_id)
    if not contract or contract.status == 'failed':
        abort(404)
    result = _parse_contract_result(contract) or {}
    is_update = _is_confirmed_update(contract)

    if contract.status == 'confirmed' and not is_update:
        flash('该合同已确认但关联项目不存在，请重新识别或重新上传合同', 'danger')
        return redirect(url_for('main.contracts'))

    try:
        submission = validate_contract_submission(request.form)
    except ValidationError as exc:
        audit_record('contract.confirm.failed', 'contract', contract.id,
                     contract.project_id, result='failed',
                     after={'errors': exc.errors}, critical=False)
        db.session.commit()
        return _render_confirm(contract, result,
                               values=_raw_contract_form_values(request.form),
                               is_update=is_update, errors=exc.errors,
                               project=db.session.get(Project, contract.project_id)
                               if is_update else None)

    if is_update:
        project = db.session.get(Project, contract.project_id)
        if not project:
            flash('关联项目已不存在，请重新确认', 'danger')
            return redirect(url_for('main.contracts'))
        current = _project_contract_values(project)
        diff = _diff_contract_values(current, submission)
        if not diff:
            flash('未检测到变更，项目数据保持不变', 'info')
            return redirect(url_for('main.project_detail', project_id=project.id))

        financial = [d for d in diff
                     if d['field'] in _FINANCIAL_FIELDS or d['field'].startswith('ratio_')]
        blockers = _contract_blockers(project)
        if financial and blockers:
            audit_record('contract.modify.blocked', 'contract', contract.id,
                         project.id, result='blocked',
                         after={'blockers': blockers, 'diff': diff}, critical=False)
            db.session.commit()
            return _render_confirm(contract, result,
                                   values=_raw_contract_form_values(request.form),
                                   is_update=True, project=project,
                                   diff=diff, blockers=blockers)

        expected_token = _diff_hash(contract, diff)
        provided_token = (request.form.get('confirm_token') or '').strip()
        if provided_token != expected_token:
            return _render_confirm(contract, result,
                                   values=_raw_contract_form_values(request.form),
                                   is_update=True, project=project,
                                   diff=diff, confirm_token=expected_token)

        # 二次确认通过：就地更新（不删除节点，不触碰已发生的开票/付款/状态）
        before_values = _project_contract_values(project)
        _apply_contract_values(project, submission, contract,
                               update_amounts=bool(financial))
        contract.contract_name = submission.contract_name
        contract.project_id = project.id
        contract.status = 'confirmed'
        contract.confirmed_by = _operator_id()
        contract.confirmed_at = _now_utc()
        audit_record('contract.modify', 'contract', contract.id, project.id,
                     before=before_values,
                     after={'contract_name': submission.contract_name,
                            'original_amount': submission.original_amount,
                            'additional_amount': submission.additional_amount,
                            'ratios_pct': {k: format(v, 'f')
                                           for k, v in submission.ratios_pct.items()},
                            'node_amounts': {k: format(v, 'f')
                                             for k, v in submission.node_amounts.items()},
                            'diff': diff})
        db.session.commit()
        for msg in submission.warnings:
            flash(msg, 'warning')
        flash('合同「%s」已更新（就地更新，付款节点与记录保持不变）'
              % submission.contract_name, 'success')
        return redirect(url_for('main.project_detail', project_id=project.id))

    # 首次确认：新建项目
    if not submission.project_no:
        submission.project_no = _next_contract_project_no()
    if Project.query.filter_by(project_no=submission.project_no).first():
        flash('项目编号「%s」已存在，请修改后提交' % submission.project_no, 'danger')
        return _render_confirm(contract, result,
                               values=_raw_contract_form_values(request.form),
                               is_update=False)
    project = _create_contract_project(submission, contract)
    contract.project_id = project.id
    contract.contract_name = submission.contract_name
    contract.status = 'confirmed'
    contract.confirmed_by = _operator_id()
    contract.confirmed_at = _now_utc()
    audit_record('contract.confirm', 'contract', contract.id, project.id,
                 after={'project_id': project.id, 'project_no': project.project_no,
                        'contract_name': submission.contract_name,
                        'original_amount': submission.original_amount,
                        'additional_amount': submission.additional_amount,
                        'node_amounts': {k: format(v, 'f')
                                         for k, v in submission.node_amounts.items()}})
    db.session.commit()
    for msg in submission.warnings:
        flash(msg, 'warning')
    flash('合同「%s」确认成功，已生成项目与付款节点（%d个）'
          % (submission.contract_name, len(NODE_TYPES)), 'success')
    return redirect(url_for('main.project_detail', project_id=project.id))


def _node_name(nt, ratio):
    name = {'first': '首付款', 'progress': '进度款',
            'acceptance': '验收款', 'warranty': '质保金'}.get(nt, nt)
    return name + _pct_str(ratio)


@bp.route('/contracts/<int:contract_id>/rerun', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def contract_rerun(contract_id):
    contract = db.session.get(ContractFile, contract_id)
    if not contract:
        abort(404)
    storage = os.path.join('instance', contract.stored_path.replace('\\', '/'))
    if not os.path.exists(storage):
        flash('合同文件已丢失，无法重新识别', 'danger')
        return redirect(url_for('main.contracts'))
    text, extract_msg = extract_text(storage, contract.file_type)
    if text is None:
        contract.status = 'failed'
        contract.error_msg = extract_msg or '文本提取失败'
        audit_record('contract.rerun', 'contract', contract.id, contract.project_id,
                     result='failed', after={'status': 'failed', 'error': contract.error_msg},
                     critical=False)
        db.session.commit()
        flash('重新识别失败：%s' % (extract_msg or '未知错误'), 'danger')
        return redirect(url_for('main.contracts'))
    was_confirmed = contract.status == 'confirmed'
    before = {'status': contract.status, 'engine': contract.engine}
    result, rec_msg = recognize_contract(text, contract.file_name)
    contract.raw_text = text
    contract.result_json = json.dumps(result, ensure_ascii=False)
    contract.engine = result.get('engine', 'rules')
    if not was_confirmed:
        contract.status = 'recognized'
    contract.error_msg = rec_msg
    audit_record('contract.rerun', 'contract', contract.id, contract.project_id,
                 before=before,
                 after={'status': contract.status, 'engine': contract.engine,
                        'contract_amount': (result or {}).get('contract_amount')},
                 critical=False)
    db.session.commit()
    if was_confirmed:
        flash('已更新识别结果（该合同已生成项目，如按新结果重新生成请重新上传合同）', 'info')
    else:
        flash('已重新识别', 'success')
    return redirect(url_for('main.contract_confirm', contract_id=contract.id))


@bp.route('/contracts/<int:contract_id>/raw')
@login_required
def contract_raw(contract_id):
    contract = db.session.get(ContractFile, contract_id)
    if not contract:
        abort(404)
    return render_template('contract_raw.html', contract=contract)


# ---------- 报表与提醒 ----------

@bp.route('/reports')
@login_required
def reports():
    projects = Project.query.order_by(Project.created_at.desc()).all()
    rows = []
    for p in projects:
        due = sum_money(n.due_amount for n in p.nodes)
        invoice = sum_money(n.invoice_amount for n in p.nodes)
        paid = sum_money(n.paid_amount for n in p.nodes)
        unpaid = (due - paid).quantize(CENT)
        progress = ((paid / due * Decimal('100')).quantize(Decimal('0.1'))
                    if due else ZERO)
        rows.append({
            'project': p, 'due': due, 'invoice': invoice, 'paid': paid,
            'unpaid': unpaid, 'progress': progress,
        })
    total_due = sum_money(r['due'] for r in rows)
    total_paid = sum_money(r['paid'] for r in rows)
    total_unpaid = sum_money(r['unpaid'] for r in rows)
    # 待办提醒：质保到期、已申请待付款、已付款节点
    today = date.today()
    reminders = []
    for p in projects:
        if p.warranty_due_date:
            days = (p.warranty_due_date - today).days
            if days <= 60:
                reminders.append(('warranty', p, days))
        for n in p.nodes:
            if n.status in ('未开票已申请', '已申请'):
                reminders.append(('pending_pay', n, None))
    return render_template('reports.html', rows=rows,
                           total_due=total_due, total_paid=total_paid, total_unpaid=total_unpaid,
                           reminders=reminders)


# ---------- 系统管理 ----------

@bp.route('/admin/users')
@login_required
@role_required('admin')
def users():
    user_list = User.query.order_by(User.id).all()
    return render_template('users.html', users=user_list)


@bp.route('/admin/users/new', methods=['POST'])
@login_required
@role_required('admin')
def user_new():
    username = (request.form.get('username') or '').strip()
    display_name = (request.form.get('display_name') or '').strip()
    password = request.form.get('password') or ''
    role = request.form.get('role') or 'viewer'
    if not username or not display_name or not password:
        flash('用户名、姓名、密码均必填', 'danger')
        return redirect(url_for('main.users'))
    if User.query.filter_by(username=username).first():
        flash('用户名已存在', 'danger')
        return redirect(url_for('main.users'))
    if role not in ('admin', 'operator', 'viewer'):
        role = 'viewer'
    user = User(username=username, display_name=display_name, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    audit_record('user.create', 'user', user.id,
                 after={'username': user.username, 'display_name': user.display_name,
                        'role': user.role, 'is_active': True})
    db.session.commit()
    flash(f'用户「{display_name}」已创建', 'success')
    return redirect(url_for('main.users'))


@bp.route('/admin/users/<int:user_id>/toggle', methods=['POST'])
@login_required
@role_required('admin')
def user_toggle(user_id):
    """停用 / 启用用户（P0-02 埋点；停用后不能登录，历史记录保留）。"""
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.id == current_user().id:
        flash('不能停用当前登录账号', 'danger')
        return redirect(url_for('main.users'))
    if user.is_active and user.role == 'admin':
        active_admins = User.query.filter_by(role='admin', is_active=True).count()
        if active_admins <= 1:
            flash('至少需要保留一个启用的管理员账号', 'danger')
            return redirect(url_for('main.users'))
    before = {'is_active': bool(user.is_active)}
    user.is_active = not bool(user.is_active)
    audit_record('user.enable' if user.is_active else 'user.disable',
                 'user', user.id, before=before,
                 after={'is_active': user.is_active, 'username': user.username})
    db.session.commit()
    flash('用户「%s」已%s' % (user.display_name, '启用' if user.is_active else '停用'), 'success')
    return redirect(url_for('main.users'))


@bp.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@login_required
@role_required('admin')
def user_delete(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.id == current_user().id:
        flash('不能删除当前登录账号', 'danger')
        return redirect(url_for('main.users'))
    # 删除前要求：已停用，或显式二次确认（表单 hidden confirm=1）
    if user.is_active and request.form.get('confirm') != '1':
        flash('请先停用该用户，或在删除确认框中二次确认后再删除', 'danger')
        return redirect(url_for('main.users'))
    audit_record('user.delete', 'user', user.id,
                 before={'username': user.username, 'display_name': user.display_name,
                         'role': user.role, 'is_active': user.is_active})
    db.session.delete(user)
    db.session.commit()
    flash(f'用户「{user.display_name}」已删除', 'success')
    return redirect(url_for('main.users'))


# ---------- 操作审计（只读） ----------

AUDIT_PAGE_SIZE = 50


@bp.route('/admin/audit')
@login_required
@role_required('admin')
def audit_logs():
    project_id = request.args.get('project_id', type=int)
    actor = (request.args.get('actor') or '').strip()
    action = (request.args.get('action') or '').strip()
    date_from_raw = (request.args.get('date_from') or '').strip()
    date_to_raw = (request.args.get('date_to') or '').strip()
    errors = []
    date_from = date_to = None
    try:
        date_from = parse_date(date_from_raw, field='开始日期')
    except ValidationError as exc:
        errors.extend(exc.errors)
    try:
        date_to = parse_date(date_to_raw, field='结束日期')
    except ValidationError as exc:
        errors.extend(exc.errors)

    query = AuditLog.query
    if project_id:
        query = query.filter(AuditLog.project_id == project_id)
    if actor:
        if actor.isdigit():
            query = query.filter(or_(AuditLog.actor_id == int(actor),
                                     AuditLog.actor_name.contains(actor)))
        else:
            query = query.filter(AuditLog.actor_name.contains(actor))
    if action:
        query = query.filter(AuditLog.action == action)
    if date_from:
        query = query.filter(AuditLog.ts >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(AuditLog.ts <
                             datetime.combine(date_to + timedelta(days=1), datetime.min.time()))

    total = query.count()
    page = max(1, request.args.get('page', 1, type=int) or 1)
    pages = max(1, (total + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)
    page = min(page, pages)
    rows = (query.order_by(AuditLog.ts.desc(), AuditLog.id.desc())
            .offset((page - 1) * AUDIT_PAGE_SIZE)
            .limit(AUDIT_PAGE_SIZE).all())
    filters = {}
    if project_id:
        filters['project_id'] = project_id
    if actor:
        filters['actor'] = actor
    if action:
        filters['action'] = action
    if date_from_raw:
        filters['date_from'] = date_from_raw
    if date_to_raw:
        filters['date_to'] = date_to_raw
    projects = Project.query.order_by(Project.created_at.desc()).limit(200).all()
    return render_template('audit.html', rows=rows, page=page, pages=pages,
                           total=total, filters=filters, projects=projects,
                           actions=action_options(action or None), errors=errors,
                           page_size=AUDIT_PAGE_SIZE)


# ---------- 通用辅助 ----------

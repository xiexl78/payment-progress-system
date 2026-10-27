# -*- coding: utf-8 -*-
"""付款进度管理系统 - 路由与业务逻辑"""
import hashlib
import io
import json
import os
import re
import secrets
from datetime import date, timedelta, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps

from flask import (Blueprint, render_template, request, redirect, url_for, session,
                   flash, jsonify, abort, current_app, send_file)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from . import NODE_TEMPLATES, FLOW_STATUS, STATUS_LABELS, login_guard, session_guard, timeutil
from .audit import action_options, record as audit_record, snapshot as audit_snapshot
from .contract_engine import (extract_text, merge_shot_texts, recognize_contract,
                              recognize_shots, SHOT_LABELS, SHOT_TYPES)
from .dbhealth import database_status
from .exporter import (audit_csv_bytes, build_project_workbook,
                       build_reports_workbook, export_filename,
                       workbook_bytes)
from .models import (db, User, Project, ProjectItem, PaymentNode, PaymentApplication,
                     PaymentInvoice, PaymentRecord, AcceptanceRecord, ContractFile,
                     ContractScreenshot, NodeActionToken, AuditLog)
from .money import (CENT, MAX_MONEY, ZERO, allocate_tail, fmt_money as money_fmt,
                    mul_ratio, parse_money, sum_money)
from .nodes import (FLOW_STATUS as NODE_FLOW_STATUS, NODE_TYPE_LABELS,
                    derive_status, node_name as make_node_name,
                    node_progress_label, three_dim)
from .validation import (DEFAULT_ACCOUNT_PASSWORDS, PROJECT_STATUSES, NODE_TYPES,
                         NODE_LABELS, TAX_RATES, ValidationError, parse_amount,
                         parse_date, parse_warranty_months,
                         validate_application_payment,
                         validate_apply_no,
                         validate_contract_change_items,
                         validate_contract_submission, validate_node_action,
                         validate_password, validate_project_dates,
                         validate_project_item_rows, validate_project_ratios,
                         validate_text)

bp = Blueprint('main', __name__)

#: 识别只处理前 5 万字符（合同正文远小于此；避免超大文本/异常文本触发正则退化）
MAX_RECOGNIZE_CHARS = 50000


# ---------- 工具函数 ----------

def current_user():
    """当前登录用户：服务端会话校验（登出/改密/过期后旧 Cookie 立即失效）。"""
    uid = session.get('user_id')
    token = session.get(session_guard.SESSION_KEY)
    if not uid or not token:
        return None
    try:
        row = session_guard.load_valid_session(token, user_id=uid)
    except Exception:
        return None
    if row is None:
        session.clear()
        return None
    user = db.session.get(User, uid)
    if user is None or not user.is_active:
        session.clear()
        return None
    return user


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for('main.login'))
        if (user.must_change_password or session.get('force_pw_change')) and \
                request.endpoint not in ('main.change_password', 'main.logout', 'main.login'):
            flash('请先修改初始密码后再继续操作', 'warning')
            return redirect(url_for('main.change_password'))
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
    limit_mb = max(1, int(current_app.config.get('MAX_CONTENT_LENGTH', 20 * 1024 * 1024))
                   // (1024 * 1024))
    return render_template('error.html', code=413, title='上传文件过大',
                           message='上传文件超过 %dMB 限制，请压缩或拆分后重试。' % limit_mb), 413


@bp.app_errorhandler(500)
def error_internal(error):
    return render_template('error.html', code=500, title='服务器内部错误',
                           message='系统处理请求时发生错误，请稍后重试；如持续出现请联系管理员。'), 500


@bp.app_errorhandler(OperationalError)
def error_database(error):
    """数据库不可用（未初始化/损坏/被锁）：中文降级页，而不是 500 英文错误页。"""
    return render_template('error.html', code=503, title='数据库不可用',
                           message='数据库当前不可用（可能未初始化、文件损坏或被占用）。'
                                   '请联系管理员使用备份恢复（见 docs/backup-restore.md）；'
                                   '恢复后可访问 /healthz 查看数据库状态。'), 503


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


_APPLY_NO_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # 去掉易混淆的 I/O/0/1


def make_apply_no(suffix=None):
    """历史自动单号生成器（CR-05 起申请单号改为人工必填，仅保留供旧数据/工具复用）。"""
    now = timeutil.utcnow()
    tail = suffix or ''.join(secrets.choice(_APPLY_NO_ALPHABET) for _ in range(6))
    return 'AP' + now.strftime('%Y%m%d%H%M%S') + '-' + tail


def _commit_application(node, amount, apply_no, actor, before_snapshot):
    """写入一笔申请单（CR-05 需求③：单号人工必填 + 全局唯一）。

    返回 (app_obj, error)：error 为 None / ``'duplicate'``（单号已存在）/
    ``'cas'``（累计将超应付或并发改动），由调用方给出中文提示。
    """
    if PaymentApplication.query.filter_by(apply_no=apply_no).first() is not None:
        return None, 'duplicate'
    result = db.session.execute(
        update(PaymentNode)
        .where(PaymentNode.id == node.id,
               PaymentNode.applied_amount + amount <= PaymentNode.due_amount)
        .values(applied_amount=PaymentNode.applied_amount + amount,
                updated_at=_now_utc(), updated_by=actor.id))
    if result.rowcount != 1:
        db.session.rollback()
        return None, 'cas'
    db.session.refresh(node)
    app_obj = PaymentApplication(
        node_id=node.id, apply_no=apply_no,
        apply_amount=amount, invoice_amount=ZERO,
        status='已申请',
        applicant_id=actor.id, applicant_name=actor.display_name)
    db.session.add(app_obj)
    try:
        db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        if 'apply_no' in str(getattr(exc, 'orig', exc)):
            return None, 'duplicate'
        raise
    node.status = derive_status(node.due_amount, node.applied_amount,
                                node.invoice_amount, node.paid_amount)
    audit_record('node.apply', 'node', node.id, node.project_id,
                 before=before_snapshot,
                 after=dict(_node_snapshot(node), apply_no=app_obj.apply_no,
                            apply_amount=format(amount, 'f')))
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        if 'apply_no' in str(getattr(exc, 'orig', exc)):
            return None, 'duplicate'
        raise
    return app_obj, None


@bp.app_template_filter('localtime')
def _localtime_filter(value, fmt='%Y-%m-%d %H:%M'):
    """模板时间展示：UTC 存储 → APP_TIMEZONE 本地时。"""
    try:
        local = timeutil.to_local(value)
        return local.strftime(fmt) if local else '—'
    except Exception:
        return '—'


def _is_admin():
    user = current_user()
    return bool(user and user.role == 'admin')


def _get_project(project_id, *, include_deleted=False):
    """取项目；已删除项目仅 admin 在 include_deleted 时可取（其它一律 404）。"""
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    if project.deleted_at is not None:
        if not (include_deleted and _is_admin()):
            abort(404)
    return project


def _guard_active_project(project):
    """已删除项目禁止业务变更；返回重定向响应或 None。"""
    if project is not None and project.deleted_at is not None:
        flash('该项目已删除（在回收站中），请先恢复后再操作', 'danger')
        return redirect(url_for('main.project_detail', project_id=project.id))
    return None


def _guard_writable_project(project):
    """已删除/已归档项目禁止业务变更（批次 4c 软删除 + P1-03 归档只读）。"""
    guard = _guard_active_project(project)
    if guard:
        return guard
    if project is not None and project.status == '已归档':
        flash('项目已归档（只读），如需变更请先由管理员按流程解除归档', 'danger')
        return redirect(url_for('main.project_detail', project_id=project.id))
    return None


def sync_project_dates(project, collect=False):
    """根据试运行/验收日期联动节点触发日期与质保到期日（幂等、不 commit）。

    - 项目级 ``warranty_due_date`` = 验收日期 + 质保期（项目 warranty_months，可配置）；
    - 进度款 ``trigger_date`` = 试运行日期；验收款 ``trigger_date`` = 验收日期；
      质保金 ``due_date`` = 验收日期 + 质保期（未填质保期则不推算，不套默认 36）；
    - 已发生资金业务（申请/开票/付款）的节点**冻结日期**，只统计差异，不改写；
    - 日期被清空时，未发生业务的节点派生日期同步清空（修复单向联动）。

    Returns: 变更明细列表（collect=True 时），供差异回显/审计使用。
    """
    trial = project.trial_date
    acceptance = project.acceptance_date
    months = project.warranty_months
    warranty_due = (timeutil.add_months(acceptance, months)
                    if acceptance and months else None)
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
                         'acceptance_date', 'warranty_due_date', 'warranty_months',
                         'remark', 'created_by', 'updated_by')
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


# ---------- 节点操作令牌（CR-05 需求⑤：无状态 HMAC + 防重放） ----------
#
# CR-04 的旧实现把每个节点的 3 个一次性 token 塞进 Cookie 会话：12 节点项目
# 每渲染一次详情页就追加约 1.1KB，第 4 次起超过浏览器 4093B 上限被整包丢弃，
# 页面 HTML 里的新 token 与浏览器实际保存的旧会话对不上 —— 于是永远提示
# 「操作已失效」，刷新只会继续追加，永远恢复不了（需求⑤根因）。
#
# 现方案：token = itsdangerous 签名串（节点/操作/会话 sid 摘要/jti/过期时间），
# 不写入 session（Cookie 体积恒定）；服务端用 node_action_tokens 表以 jti 主键
# 登记消费记录做防重放，因此仍然「一次点击只能成功一次」。
NODE_ACTIONS = ('apply', 'invoice', 'pay')
#: 令牌有效期兜底（秒）：配置项 NODE_TOKEN_TTL_MINUTES 缺失时使用。
#: 实际值优先读 current_app.config['NODE_TOKEN_TTL_MINUTES']（默认 30 分钟，可配）。
NODE_TOKEN_TTL = 30 * 60
NODE_TOKEN_SALT = 'payment-progress.node-action'

NODE_TOKEN_OK = 'ok'
NODE_TOKEN_REPLAY = 'replay'
NODE_TOKEN_EXPIRED = 'expired'
NODE_TOKEN_INVALID = 'invalid'


def _node_token_ttl_seconds():
    """节点操作令牌有效期（秒）：从配置读，缺失/非法时回退到常量。"""
    minutes = current_app.config.get('NODE_TOKEN_TTL_MINUTES')
    if minutes is None:
        return NODE_TOKEN_TTL
    try:
        seconds = int(round(float(minutes) * 60))
    except (TypeError, ValueError):
        return NODE_TOKEN_TTL
    return max(1, seconds)


def _node_token_ttl_label():
    """有效期的中文展示（提示语用），如「30 分钟」「1.5 分钟」「1 秒」。"""
    seconds = _node_token_ttl_seconds()
    if seconds >= 60:
        minutes = seconds / 60.0
        text = ('%d' % round(minutes)) if abs(minutes - round(minutes)) < 1e-9 \
            else ('%.1f' % minutes)
        return text + ' 分钟'
    return '%d 秒' % seconds


def _node_token_serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'],
                                  salt=NODE_TOKEN_SALT)


def _session_digest():
    """当前登录会话 sid 的短摘要：令牌与会话绑定，登出/改密后旧令牌自动失效。"""
    sid = session_guard.current_token() or ''
    return hashlib.sha256(sid.encode('utf-8')).hexdigest()[:8]


def _issue_node_tokens(project):
    """为详情页每个节点的三个操作签发签名令牌（无状态，不写 session）。"""
    session.pop('node_tokens', None)  # 清理 CR-04 遗留在旧会话里的令牌表
    serializer = _node_token_serializer()
    sid_digest = _session_digest()
    ttl = _node_token_ttl_seconds()
    # naive UTC 直接 .timestamp() 会按本机时区解释（Asia/Shanghai 会晚 8 小时），
    # 导致 expires_at 早于 now、消费记录被清理，同令牌可重放（CR-05 自测发现）。
    expires_at = int(timeutil.utcnow().replace(tzinfo=timezone.utc).timestamp()) + ttl
    issued = {}
    for node in project.nodes:
        per_node = {}
        for action in NODE_ACTIONS:
            payload = {'n': node.id, 'a': action, 's': sid_digest,
                       'j': secrets.token_urlsafe(16)[:22], 'e': expires_at}
            per_node[action] = serializer.dumps(payload)
        issued[node.id] = per_node
    return issued


def _consume_node_token(token, node_id, action):
    """校验令牌并登记消费；重复提交由 jti 主键冲突判定（返回状态字符串）。"""
    token = (token or '').strip()
    if not token:
        return NODE_TOKEN_INVALID
    ttl = _node_token_ttl_seconds()
    try:
        data = _node_token_serializer().loads(token, max_age=ttl)
    except SignatureExpired:
        return NODE_TOKEN_EXPIRED
    except BadSignature:
        return NODE_TOKEN_INVALID
    except Exception:
        return NODE_TOKEN_INVALID
    if not isinstance(data, dict):
        return NODE_TOKEN_INVALID
    if data.get('n') != node_id or data.get('a') != action:
        return NODE_TOKEN_INVALID
    sid_digest = _session_digest()
    if not sid_digest or data.get('s') != sid_digest:
        return NODE_TOKEN_INVALID
    jti = data.get('j')
    if not jti or len(str(jti)) > 64:
        return NODE_TOKEN_INVALID
    now = timeutil.utcnow()
    # 清理已过期的历史消费记录（expires_at 已含 TTL 余量，可直接按 now 判断）
    try:
        NodeActionToken.query.filter(
            NodeActionToken.expires_at < now
        ).delete(synchronize_session=False)
    except Exception:
        db.session.rollback()
    expires_at = now + timedelta(seconds=ttl)
    if data.get('e'):
        try:
            expires_at = datetime.utcfromtimestamp(int(data['e']))
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    row = NodeActionToken(jti=str(jti), node_id=node_id, action=action,
                          session_sid=session_guard.current_token(),
                          expires_at=expires_at, consumed_at=now)
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return NODE_TOKEN_REPLAY
    return NODE_TOKEN_OK


# ---------- 认证 ----------

def _login_throttle_config():
    cfg = current_app.config
    return {
        'enabled': bool(cfg.get('LOGIN_THROTTLE_ENABLED', True)),
        'max_failures': max(1, int(cfg.get('LOGIN_MAX_FAILURES', 5) or 5)),
        'window_seconds': max(1, int(float(cfg.get('LOGIN_FAILURE_WINDOW_MINUTES', 5) or 5) * 60)),
        'lock_seconds': max(1, int(float(cfg.get('LOGIN_LOCK_MINUTES', 15) or 15) * 60)),
    }


def _must_change_password(user):
    """是否要求先改密：账号标记，或仍在使用默认口令（生产模式）。"""
    if user.must_change_password:
        return True
    if not current_app.config.get('FORCE_DEFAULT_PASSWORD_CHANGE', True):
        return False
    default_password = DEFAULT_ACCOUNT_PASSWORDS.get(user.username)
    return bool(default_password) and user.check_password(default_password)


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user():
        return redirect(url_for('main.projects'))
    if request.method == 'POST':
        db_state = database_status()
        if not db_state['ok']:
            return render_template('login.html', db_error=db_state)
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        ip = request.remote_addr
        throttle = _login_throttle_config()
        if throttle['enabled'] and login_guard.is_locked(
                username, ip, window_seconds=throttle['window_seconds'],
                lock_seconds=throttle['lock_seconds']):
            audit_record('auth.login.locked', 'user', result='denied',
                         after={'username': username, 'ip': ip})
            db.session.commit()
            flash('登录失败次数过多，账号已临时锁定，请 %d 分钟后再试'
                  % max(1, throttle['lock_seconds'] // 60), 'danger')
            return render_template('login.html')

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            if not user.is_active:
                if throttle['enabled']:
                    login_guard.register_failure(
                        username, ip, window_seconds=throttle['window_seconds'],
                        max_failures=throttle['max_failures'],
                        lock_seconds=throttle['lock_seconds'])
                audit_record('auth.login.failed', 'user', user.id, result='failed',
                             after={'username': username, 'reason': '账号已停用'})
                db.session.commit()
                flash('账号已停用，请联系管理员', 'danger')
                return render_template('login.html')

            login_guard.reset(username, ip)
            session_guard.create_session(user)
            # 账号级敏感操作：登录成功后轮换 CSRF（登录页表单 token 不复用）
            try:
                from .security import rotate_csrf_token
                rotate_csrf_token()
            except Exception:
                current_app.logger.exception('登录后轮换 CSRF token 失败')
            audit_record('auth.login.success', 'user', user.id,
                         after={'username': username})
            db.session.commit()

            if _must_change_password(user):
                # 只在当前会话标记强制改密，不写库（避免影响该用户其它有效会话）
                session['force_pw_change'] = True
                audit_record('auth.login.force_change', 'user', user.id,
                             after={'username': username,
                                    'reason': '初始/默认口令需修改'})
                db.session.commit()
                flash('检测到您正在使用初始/默认口令，请先修改密码', 'warning')
                return redirect(url_for('main.change_password'))
            flash(f'欢迎回来，{user.display_name}', 'success')
            return redirect(url_for('main.projects'))

        locked_now = False
        if throttle['enabled']:
            locked_now = login_guard.register_failure(
                username, ip, window_seconds=throttle['window_seconds'],
                max_failures=throttle['max_failures'],
                lock_seconds=throttle['lock_seconds'])
        audit_record('auth.login.failed', 'user', user.id if user else None,
                     result='failed', after={'username': username})
        db.session.commit()
        if locked_now:
            audit_record('auth.login.locked', 'user', user.id if user else None,
                         result='denied', after={'username': username, 'ip': ip})
            db.session.commit()
            flash('登录失败次数过多，账号已临时锁定，请 %d 分钟后再试'
                  % max(1, throttle['lock_seconds'] // 60), 'danger')
        else:
            flash('用户名或密码错误', 'danger')
    db_state = database_status()
    return render_template('login.html', db_error=None if db_state['ok'] else db_state)


@bp.route('/healthz')
def healthz():
    """健康检查：数据库可用返回 200，不可用返回 503（无需登录）。"""
    db_state = database_status()
    payload = {
        'status': 'ok' if db_state['ok'] else 'degraded',
        'db': db_state,
        'env': current_app.config.get('APP_ENV'),
        'time': timeutil.utcnow().isoformat() + 'Z',
    }
    return jsonify(payload), (200 if db_state['ok'] else 503)


@bp.route('/logout', methods=['POST'])
def logout():
    user = current_user()
    if user:
        audit_record('auth.logout', 'user', user.id, after={'username': user.username})
        db.session.commit()
    session_guard.revoke_current()
    db.session.commit()
    flash('已退出登录', 'info')
    return redirect(url_for('main.login'))


@bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    """修改本人密码：≥8 位、不得与用户名/旧密码相同、不得为默认口令；改后旧会话全部失效。"""
    user = current_user()
    if request.method == 'POST':
        try:
            current_password = request.form.get('current_password') or ''
            new_password = validate_password(
                request.form.get('new_password'), username=user.username,
                field='新密码',
                min_length=int(current_app.config.get('PASSWORD_MIN_LENGTH', 8) or 8))
            confirm_password = request.form.get('confirm_password') or ''
            if new_password != confirm_password:
                raise ValidationError('两次输入的新密码不一致')
            if not user.check_password(current_password):
                raise ValidationError('当前密码不正确')
            if user.check_password(new_password):
                raise ValidationError('新密码不能与当前密码相同')
        except ValidationError as exc:
            for msg in exc.errors:
                flash(msg, 'danger')
            return render_template('change_password.html', must_change=user.must_change_password)
        user.set_password(new_password)
        user.must_change_password = False
        user.password_updated_at = datetime.utcnow()
        session_guard.revoke_user(user.id)
        audit_record('user.change_password', 'user', user.id, result='ok',
                     after={'username': user.username})
        db.session.commit()
        session.clear()
        flash('密码已修改，请使用新密码重新登录', 'success')
        return redirect(url_for('main.login'))
    return render_template('change_password.html', must_change=user.must_change_password)


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
    deleted_view = _is_admin() and (request.args.get('deleted') or '') == '1'
    q = Project.query
    if deleted_view:
        q = q.filter(Project.deleted_at.isnot(None))
    else:
        q = q.filter(Project.deleted_at.is_(None))
    if keyword:
        q = q.filter(or_(Project.name.contains(keyword), Project.project_no.contains(keyword)))
    if status:
        q = q.filter(Project.status == status)
    project_list = q.order_by(Project.created_at.desc()).all()
    stats = {
        'total': len(project_list),
        'ongoing': sum(1 for p in project_list if p.status == '进行中'),
        'done': sum(1 for p in project_list if p.status == '已完成'),
        'amount': sum((p.total_amount() for p in project_list), ZERO),
    }
    return render_template('projects.html', projects=project_list, stats=stats,
                           keyword=keyword, status=status, deleted_view=deleted_view)


def _create_item_nodes(item, ratios, operator_id=None):
    """为科目生成 4 个付款节点：科目总额 × 人工填写的比例，尾差归质保金。

    CR-04：比例 100% 来自合同确认时的人工输入，函数不再读取任何默认值。
    ``ratios``：``{node_type: Decimal(百分数)}``。
    """
    base_total = (Decimal(item.original_amount or 0)
                  + Decimal(item.additional_amount or 0)).quantize(CENT)
    frac = {nt: (Decimal(ratios[nt]) / Decimal('100')).quantize(Decimal('0.000001'))
            for nt in NODE_TYPES}
    raw_dues = {nt: mul_ratio(base_total, frac[nt]) for nt in frac}
    try:
        dues = allocate_tail(raw_dues, base_total)
    except ValueError:
        dues = raw_dues
    nodes = []
    for nt in NODE_TYPES:
        node = PaymentNode(project_id=item.project_id, item_id=item.id,
                           node_type=nt, node_name=make_node_name(nt, frac[nt]),
                           ratio=frac[nt], calc_base='total',
                           due_amount=dues[nt],
                           status='未触发', updated_by=operator_id)
        db.session.add(node)
        nodes.append(node)
    return nodes


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
            ratios = validate_project_ratios(request.form)
            warranty_months = parse_warranty_months(request.form.get('warranty_months'))
        except ValidationError as exc:
            for msg in exc.errors:
                flash(msg, 'danger')
            return render_template('project_form.html', templates=templates, project=None,
                                   form=request.form)

        project = Project(project_no=project_no, name=name, remark=remark,
                          warranty_months=warranty_months,
                          status='进行中', created_by=current_user().id,
                          updated_by=current_user().id)
        try:
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

            # 为每个科目生成 4 个付款节点（人工比例 + 尾差归集）
            for it in items:
                _create_item_nodes(it, ratios, current_user().id)
            sync_project_dates(project)
            audit_record('project.create', 'project', project.id, project.id,
                         after=_project_snapshot(project), critical=False)
            db.session.commit()
        except IntegrityError:
            # 并发创建同一项目编号：唯一索引兜底（flush 即触发），不能 500（P2-07）
            db.session.rollback()
            flash('项目编号「%s」已存在（可能刚被其他人创建），请换一个编号重试' % project_no,
                  'danger')
            return redirect(url_for('main.projects'))
        flash('项目创建成功，已自动生成付款节点（编号：%s）' % project_no, 'success')
        return redirect(url_for('main.project_detail', project_id=project.id))
    return render_template('project_form.html', templates=templates, project=None)


@bp.route('/projects/<int:project_id>')
@login_required
def project_detail(project_id):
    project = _get_project(project_id, include_deleted=True)
    if not project:
        abort(404)
    # 按节点类型聚合展示，便于跨科目合计（Decimal 口径）
    node_groups = {t['node_type']: {'meta': t, 'nodes': [], 'sum_due': ZERO,
                                    'sum_applied': ZERO, 'sum_invoice': ZERO,
                                    'sum_paid': ZERO}
                   for t in NODE_TEMPLATES}
    node_three = {}
    for node in project.nodes:
        g = node_groups.get(node.node_type)
        if g is None:
            continue
        g['nodes'].append(node)
        dims = three_dim(node)
        node_three[node.id] = dims
        g['sum_due'] += dims['due']
        g['sum_applied'] += dims['applied']
        g['sum_invoice'] += dims['invoice']
        g['sum_paid'] += dims['paid']

    # 三维合计（CR-04：申请/开票/付款并列）
    totals_dim = {
        'due': sum_money(n.due_amount for n in project.nodes),
        'applied': sum_money(n.applied_amount for n in project.nodes),
        'invoice': sum_money(n.invoice_amount for n in project.nodes),
        'paid': sum_money(n.paid_amount for n in project.nodes),
    }

    # 超付（合同变更后已付 > 新应付）：详情/报表展示，禁止再付款（批次 8）
    overpaid_total = sum(((Decimal(node.paid_amount or 0)
                           - Decimal(node.due_amount or 0))
                          for node in project.nodes
                          if Decimal(node.paid_amount or 0) > Decimal(node.due_amount or 0)),
                         ZERO).quantize(CENT)
    acceptances = AcceptanceRecord.query.filter_by(project_id=project.id).order_by(AcceptanceRecord.acceptance_date.desc()).all()

    # 付款申请单列表（CR-05 需求④）：已付金额 = 该申请单下 payment_records 合计（动态算）
    applications = []
    for node in project.nodes:
        for app in node.applications:
            paid = sum_money(r.paid_amount for r in app.records)
            apply_amount = Decimal(app.apply_amount or 0).quantize(CENT)
            applications.append({
                'app': app,
                'node': node,
                'item_name': node.item.item_name if node.item else '',
                'apply_amount': apply_amount,
                'paid': paid,
                'remaining': (apply_amount - paid).quantize(CENT),
                'count': len(app.records),
                'over': paid > apply_amount,
                'apply_time_text': (timeutil.to_local(app.apply_time).strftime('%Y-%m-%d %H:%M')
                                    if app.apply_time else ''),
                'records': sorted(app.records,
                                  key=lambda r: (r.pay_date or r.created_at, r.id)),
            })
    applications.sort(key=lambda row: (
        row['app'].apply_time is None, row['app'].apply_time or datetime.min, row['app'].id),
        reverse=True)
    shot_rows = []
    for contract in sorted(project.contracts, key=lambda c: c.id):
        shots = sorted(contract.screenshots, key=lambda s: SHOT_TYPES.index(s.shot_type)) \
            if contract.file_type == 'img' else []
        if shots:
            shot_rows.append({'contract': contract, 'shots': shots})
    return render_template('project_detail.html', project=project, node_groups=node_groups,
                           acceptances=acceptances, templates=NODE_TEMPLATES,
                           applications=applications,
                           node_three=node_three, node_tokens=_issue_node_tokens(project),
                           totals_dim=totals_dim, status_labels=STATUS_LABELS,
                           flow_status=NODE_FLOW_STATUS,
                           shot_rows=shot_rows, shot_labels=SHOT_LABELS,
                           overpaid_total=overpaid_total)


@bp.route('/projects/<int:project_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'operator')
def project_edit(project_id):
    project = _get_project(project_id)
    if not project:
        abort(404)
    guard = _guard_writable_project(project)
    if guard:
        return guard
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
            warranty_months = parse_warranty_months(request.form.get('warranty_months'))
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
        project.warranty_months = warranty_months
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
    project = _get_project(project_id)
    if not project:
        abort(404)
    guard = _guard_writable_project(project)
    if guard:
        return guard
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
        ratios = validate_project_ratios(request.form)
    except ValidationError as exc:
        for msg in exc.errors:
            flash(msg, 'danger')
        return redirect(url_for('main.project_detail', project_id=project.id))
    item = ProjectItem(project_id=project.id, item_name=item_name,
                       tax_rate=tax_rate,
                       original_amount=original,
                       additional_amount=ZERO,
                       updated_by=current_user().id)
    db.session.add(item)
    db.session.flush()
    _create_item_nodes(item, ratios, current_user().id)
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
    project = _get_project(project_id)
    if not project:
        abort(404)
    if project.deleted_at is not None:
        flash('项目已在回收站中', 'info')
        return redirect(url_for('main.projects'))
    before_snapshot = _project_snapshot(project)
    # 软删除（批次 4c）：只打标记，不删节点/申请/付款/验收，不解绑合同
    project.deleted_at = timeutil.utcnow()
    project.deleted_by = current_user().id
    audit_record('project.delete', 'project', project.id, project.id,
                 before=before_snapshot,
                 after={'deleted_at': _date_str(project.deleted_at),
                        'deleted_by': project.deleted_by},
                 result='ok')
    db.session.commit()
    flash('项目已移入回收站（管理员可在「回收站」中查看/恢复）', 'success')
    return redirect(url_for('main.projects'))


@bp.route('/projects/<int:project_id>/restore', methods=['POST'])
@login_required
@role_required('admin')
def project_restore(project_id):
    """从回收站恢复项目（admin）。"""
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    if project.deleted_at is None:
        flash('项目未删除，无需恢复', 'info')
        return redirect(url_for('main.project_detail', project_id=project.id))
    before = {'deleted_at': _date_str(project.deleted_at),
              'deleted_by': project.deleted_by}
    project.deleted_at = None
    project.deleted_by = None
    project.updated_by = current_user().id
    audit_record('project.restore', 'project', project.id, project.id,
                 before=before, after={'deleted_at': None, 'deleted_by': None})
    db.session.commit()
    flash('项目「%s」已恢复' % project.name, 'success')
    return redirect(url_for('main.project_detail', project_id=project.id))


def _flash_node_token_failure(token_status):
    """节点一次性令牌校验失败的统一中文提示（CR-05 需求⑤）。"""
    if token_status == NODE_TOKEN_REPLAY:
        flash('该操作已提交（请不要重复点击），请重新打开项目详情页查看最新状态', 'warning')
    elif token_status == NODE_TOKEN_EXPIRED:
        flash('操作令牌已过期（页面停留超过 %s），请重新打开项目详情页后再试'
              % _node_token_ttl_label(), 'danger')
    else:
        flash('操作已失效（页面已过期或链接无效），请重新打开项目详情页后再试', 'danger')


@bp.route('/nodes/<int:node_id>/update', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def node_update(node_id):
    """节点申请 / 开票 / 付款（CR-04：三本独立账，任意顺序、互不为前置）。"""
    node = db.session.get(PaymentNode, node_id)
    if not node:
        abort(404)
    guard = _guard_writable_project(node.project)
    if guard:
        return guard
    detail_url = url_for('main.project_detail', project_id=node.project_id)
    actor = current_user()
    action = (request.form.get('action') or '').strip()
    if action not in NODE_ACTIONS:
        flash('未知的节点操作', 'danger')
        return redirect(detail_url)
    before_snapshot = _node_snapshot(node)
    # 先做完全部入参校验、再消费令牌：校验失败时令牌不烧掉，用户改对后可直接重提
    # （CR-05 T2b：原实现先消费后校验，改一次金额就变成「已失效」，与原 bug 同症状）。
    try:
        parsed = validate_node_action(node, action, request.form)
        amount = parsed['amount']
        if action == 'apply':
            apply_no = validate_apply_no(request.form.get('apply_no'))
        elif action == 'invoice':
            invoice_no = validate_text(request.form.get('invoice_no'),
                                       field='发票号', required=True, max_len=128)
            invoice_date = parse_date(request.form.get('invoice_date'),
                                      field='开票日期', required=False)
            remark = (request.form.get('invoice_remark') or '').strip()
        elif action == 'pay':
            pay_date = (parse_date(request.form.get('pay_date'), field='付款日期')
                        or timeutil.local_today())
            pay_method = (request.form.get('pay_method') or '').strip()
            voucher = (request.form.get('voucher_no') or '').strip()
    except ValidationError as exc:
        db.session.rollback()
        for msg in exc.errors:
            flash(msg, 'danger')
        return redirect(detail_url)

    token_status = _consume_node_token(request.form.get('confirm_token'),
                                       node.id, action)
    if token_status != NODE_TOKEN_OK:
        _flash_node_token_failure(token_status)
        return redirect(detail_url)

    try:
        if action == 'apply':
            app_obj, error = _commit_application(node, amount, apply_no,
                                                 actor, before_snapshot)
            if error == 'duplicate':
                flash('申请单号 %s 已存在，请换一个单号' % apply_no, 'danger')
                return redirect(detail_url)
            if app_obj is None:
                flash('申请登记失败：累计申请将超过应付，或该节点已被他人修改，请刷新后重试', 'danger')
                return redirect(detail_url)
            flash('%s 已登记申请 %s 元（单号 %s）'
                  % (node.node_name, fmt_money(amount), app_obj.apply_no), 'success')

        elif action == 'invoice':
            duplicate = PaymentInvoice.query.filter_by(
                node_id=node.id, invoice_no=invoice_no).first()
            result = db.session.execute(
                update(PaymentNode)
                .where(PaymentNode.id == node.id,
                       PaymentNode.invoice_amount + amount <= PaymentNode.due_amount)
                .values(invoice_amount=PaymentNode.invoice_amount + amount,
                        updated_at=_now_utc(), updated_by=actor.id))
            if result.rowcount != 1:
                db.session.rollback()
                flash('开票登记失败：累计开票将超过应付，或该节点已被他人修改，请刷新后重试', 'danger')
                return redirect(detail_url)
            db.session.refresh(node)
            inv = PaymentInvoice(node_id=node.id, project_id=node.project_id,
                                 invoice_no=invoice_no,
                                 amount=amount, invoice_date=invoice_date,
                                 remark=remark or None, created_by=actor.id)
            db.session.add(inv)
            db.session.flush()
            node.status = derive_status(node.due_amount, node.applied_amount,
                                        node.invoice_amount, node.paid_amount)
            audit_record('node.invoice', 'node', node.id, node.project_id,
                         before=before_snapshot,
                         after=dict(_node_snapshot(node), invoice_id=inv.id,
                                    invoice_no=invoice_no,
                                    invoice_date=_date_str(invoice_date),
                                    duplicate_invoice_no=bool(duplicate)))
            db.session.commit()
            if duplicate:
                flash('发票 %s 已在本节点登记过（同一发票可覆盖多个节点，系统不阻断），'
                      '本次开票 %s 元已记录，请核对' % (invoice_no, fmt_money(amount)),
                      'warning')
            else:
                flash('%s 已登记开票 %s 元（发票号 %s）'
                      % (node.node_name, fmt_money(amount), invoice_no), 'success')

        elif action == 'pay':
            result = db.session.execute(
                update(PaymentNode)
                .where(PaymentNode.id == node.id,
                       PaymentNode.paid_amount + amount <= PaymentNode.due_amount)
                .values(paid_amount=PaymentNode.paid_amount + amount,
                        updated_at=_now_utc(), updated_by=actor.id))
            if result.rowcount != 1:
                db.session.rollback()
                flash('付款登记失败：累计付款将超过应付，或该节点已被他人修改，请刷新后重试', 'danger')
                return redirect(detail_url)
            db.session.refresh(node)
            rec = PaymentRecord(node_id=node.id, paid_amount=amount,
                                pay_date=pay_date,
                                pay_method=pay_method or None,
                                voucher_no=voucher or None,
                                operator_id=actor.id,
                                operator_name=actor.display_name)
            db.session.add(rec)
            db.session.flush()
            node.status = derive_status(node.due_amount, node.applied_amount,
                                        node.invoice_amount, node.paid_amount)
            audit_record('node.pay', 'node', node.id, node.project_id,
                         before=before_snapshot,
                         after=dict(_node_snapshot(node), record_id=rec.id,
                                    pay_amount=format(amount, 'f'),
                                    pay_date=_date_str(rec.pay_date),
                                    pay_method=rec.pay_method))
            db.session.commit()
            flash('%s 已登记付款 %s 元' % (node.node_name, fmt_money(amount)), 'success')
    except ValidationError as exc:
        db.session.rollback()
        for msg in exc.errors:
            flash(msg, 'danger')
    return redirect(detail_url)


@bp.route('/applications/<int:app_id>/payments', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def application_payment(app_id):
    """申请单增加付款（CR-05 需求④：一张申请可对应多笔付款）。

    与节点直接付款共用 ``action='pay'`` 的服务端一次性令牌；两条上限同时生效：
    申请单剩余可付、节点应付。凭证号重复只提示不阻断。
    """
    application = db.session.get(PaymentApplication, app_id)
    if not application:
        abort(404)
    node = application.node
    if not node:
        abort(404)
    guard = _guard_writable_project(node.project)
    if guard:
        return guard
    detail_url = url_for('main.project_detail', project_id=node.project_id)
    actor = current_user()
    before_snapshot = _node_snapshot(node)
    before_app = dict(application.to_dict())
    # 先校验后消费令牌（CR-05 T2b）：金额/上限校验失败时不烧令牌，改对后可原样重提
    try:
        parsed = validate_application_payment(application, request.form)
        amount = parsed['amount']
        pay_date = (parse_date(request.form.get('pay_date'), field='付款日期')
                    or timeutil.local_today())
        pay_method = (request.form.get('pay_method') or '').strip()
        voucher = (request.form.get('voucher_no') or '').strip()
    except ValidationError as exc:
        db.session.rollback()
        for msg in exc.errors:
            flash(msg, 'danger')
        return redirect(detail_url)

    token_status = _consume_node_token(request.form.get('confirm_token'),
                                       node.id, 'pay')
    if token_status != NODE_TOKEN_OK:
        _flash_node_token_failure(token_status)
        return redirect(detail_url)

    try:
        result = db.session.execute(
            update(PaymentNode)
            .where(PaymentNode.id == node.id,
                   PaymentNode.paid_amount + amount <= PaymentNode.due_amount)
            .values(paid_amount=PaymentNode.paid_amount + amount,
                    updated_at=_now_utc(), updated_by=actor.id))
        if result.rowcount != 1:
            db.session.rollback()
            flash('付款登记失败：累计付款将超过应付，或该节点已被他人修改，请刷新后重试', 'danger')
            return redirect(detail_url)
        db.session.refresh(node)
        duplicate_voucher = bool(voucher) and bool(
            PaymentRecord.query.filter_by(voucher_no=voucher).first())
        rec = PaymentRecord(node_id=node.id, application_id=application.id,
                            paid_amount=amount,
                            pay_date=pay_date,
                            pay_method=pay_method or None,
                            voucher_no=voucher or None,
                            operator_id=actor.id,
                            operator_name=actor.display_name)
        db.session.add(rec)
        db.session.flush()
        node.status = derive_status(node.due_amount, node.applied_amount,
                                    node.invoice_amount, node.paid_amount)
        audit_record('application.pay', 'application', application.id,
                     node.project_id,
                     before=dict(before_snapshot, application=before_app),
                     after=dict(_node_snapshot(node), record_id=rec.id,
                                application_id=application.id,
                                pay_amount=format(amount, 'f'),
                                pay_date=_date_str(rec.pay_date),
                                pay_method=rec.pay_method,
                                voucher_no=rec.voucher_no))
        db.session.commit()
        if duplicate_voucher:
            flash('申请单 %s 已登记付款 %s 元，但凭证号 %s 已在本系统使用过，请核对'
                  % (application.apply_no, fmt_money(amount), voucher), 'warning')
        else:
            flash('申请单 %s 已登记付款 %s 元' % (application.apply_no, fmt_money(amount)),
                  'success')
    except ValidationError as exc:
        db.session.rollback()
        for msg in exc.errors:
            flash(msg, 'danger')
    return redirect(detail_url)


@bp.route('/projects/<int:project_id>/acceptance', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def acceptance_add(project_id):
    project = _get_project(project_id)
    if not project:
        abort(404)
    guard = _guard_writable_project(project)
    if guard:
        return guard
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
    if project.warranty_due_date:
        flash(f'验收记录已登记：{acc_date}，质保到期日按质保期 {project.warranty_months} 个月'
              f'推算为 {project.warranty_due_date}', 'success')
    else:
        flash(f'验收记录已登记：{acc_date}；项目未填写质保期（月），质保到期日暂不推算，'
              '请在项目编辑中补填', 'warning')
    return redirect(url_for('main.project_detail', project_id=project.id))


@bp.route('/projects/<int:project_id>/trial', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def trial_set(project_id):
    project = _get_project(project_id)
    if not project:
        abort(404)
    guard = _guard_writable_project(project)
    if guard:
        return guard
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
    project = _get_project(project_id)
    if not project:
        abort(404)
    status = request.form.get('status')
    if project.status == '已归档' and status != '已归档':
        flash('已归档项目不可回退（只读）；如确需修改请联系管理员按流程处理', 'danger')
        return redirect(url_for('main.project_detail', project_id=project.id))
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
CALC_BASE_LABELS = {'first': '按总金额', 'progress': '按总金额', 'acceptance': '按总金额', 'warranty': '按总金额'}

# CR-05 需求①：三张关键信息截图（①②必传、③选传），单张 ≤10MB
SHOT_REQUIRED = ('amount', 'payment')
SHOT_EXTS = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
             'bmp': 'image/bmp', 'webp': 'image/webp'}
MAX_SHOT_BYTES = 10 * 1024 * 1024



def contract_upload_dir():
    """上传目录：优先取配置的 UPLOAD_DIR（DATA_DIR/uploads），测试可 monkeypatch。"""
    d = current_app.config.get('UPLOAD_DIR')
    if not d:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        d = os.path.join(base_dir, 'instance', 'uploads')
    os.makedirs(d, exist_ok=True)
    return d


def contract_storage_path(contract):
    """合同文件的绝对路径：不依赖进程 CWD（P2-05）。

    ``stored_path`` 形如 ``uploads/<id>.<ext>``（相对 UPLOAD_DIR）；兼容历史绝对路径。
    """
    raw = (contract.stored_path or '').replace('\\', '/').strip()
    if not raw:
        return None
    if os.path.isabs(raw):
        return raw
    name = raw.split('/', 1)[1] if raw.startswith('uploads/') else raw
    return os.path.join(contract_upload_dir(), name)


def _discard_contract_file(contract):
    """删除合同落盘文件（上传失败/校验不过时避免残留）；DB 记录与路径保留供追溯。"""
    path = contract_storage_path(contract)
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as exc:
        current_app.logger.warning('删除合同残留文件失败：%s（%s）', path, exc)
    contract.file_size = 0


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

    try:
        text, extract_msg = extract_text(stored, file_type)
    except Exception as exc:
        # 处理异常也不留落盘残留文件
        _discard_contract_file(contract)
        contract.status = 'failed'
        contract.error_msg = '文本提取异常：%s' % exc
        db.session.commit()
        flash('合同处理失败：%s' % exc, 'danger')
        return redirect(url_for('main.contracts'))
    if text is None:
        _discard_contract_file(contract)
        contract.status = 'failed'
        contract.error_msg = extract_msg or '文本提取失败'
        audit_record('contract.upload', 'contract', contract.id, result='failed',
                     after={'file_name': contract.file_name, 'status': 'failed',
                            'error': contract.error_msg}, critical=False)
        db.session.commit()
        flash('文本提取失败：%s' % (extract_msg or '未知错误'), 'danger')
        return redirect(url_for('main.contracts'))

    result, rec_msg = recognize_contract(text[:MAX_RECOGNIZE_CHARS], file.filename)
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


# ---------- CR-05 需求①：按模板上传关键信息截图 ----------

def screenshot_storage_path(shot):
    """截图原图绝对路径（``stored_path`` 相对 UPLOAD_DIR，兼容历史绝对路径）。"""
    raw = (shot.stored_path or '').replace('\\', '/').strip()
    if not raw:
        return None
    if os.path.isabs(raw):
        return raw
    name = raw.split('/', 1)[1] if raw.startswith('uploads/') else raw
    return os.path.join(contract_upload_dir(), name)


def _shot_file_error(file_obj, shot_type):
    """校验单张截图扩展名与大小，返回 (ext, mime, size, error)。"""
    name = file_obj.filename or ''
    ext = (name.rsplit('.', 1)[-1] if '.' in name else '').lower()
    if ext not in SHOT_EXTS:
        return ext, None, 0, '%s：不支持的图片格式（支持 JPG / PNG / BMP / WEBP）' \
            % SHOT_LABELS[shot_type]
    try:
        file_obj.stream.seek(0, os.SEEK_END)
        size = file_obj.stream.tell()
        file_obj.stream.seek(0)
    except (OSError, AttributeError):
        size = 0
    if size > MAX_SHOT_BYTES:
        return ext, None, size, '%s：图片超过 10MB，请压缩后重传' % SHOT_LABELS[shot_type]
    return ext, SHOT_EXTS[ext], size, None


def _read_shot_text(shot):
    """OCR 单张截图并回写 ocr_status/ocr_text/ocr_msg，返回文本（失败 None）。"""
    path = screenshot_storage_path(shot)
    if not path or not os.path.isfile(path):
        shot.ocr_status = 'failed'
        shot.ocr_msg = '截图文件不存在或已丢失'
        return None
    try:
        text, msg = extract_text(path, 'img')
    except Exception as exc:  # noqa: BLE001 - OCR 失败一律降级为人工填写
        shot.ocr_status = 'failed'
        shot.ocr_msg = 'OCR 异常：%s' % exc
        return None
    if text is None:
        shot.ocr_status = 'failed'
        shot.ocr_msg = msg or 'OCR 未识别到文字'
        return None
    shot.ocr_text = text
    shot.ocr_status = 'ok'
    shot.ocr_msg = None
    return text


def _shot_meta(shots):
    return [{'shot_type': s.shot_type, 'shot_id': s.id,
             'ocr_status': s.ocr_status, 'ocr_msg': s.ocr_msg,
             'text_len': len(s.ocr_text or '')} for s in shots]


@bp.route('/contracts/shots', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def contract_shots_upload():
    """三张截图上传 + 分类型 OCR + 定向识别（CR-05 需求①）。"""
    files = {t: request.files.get('shot_' + t) for t in SHOT_TYPES}
    missing = [SHOT_LABELS[t] for t in SHOT_REQUIRED
               if not (files[t] and files[t].filename)]
    if missing:
        flash('请上传：%s（截图①金额、②付款条件为必传项）' % '、'.join(missing), 'danger')
        return redirect(url_for('main.contracts'))

    prepared = []
    for shot_type in SHOT_TYPES:
        file_obj = files[shot_type]
        if not (file_obj and file_obj.filename):
            continue
        ext, mime, size, error = _shot_file_error(file_obj, shot_type)
        if error:
            flash(error, 'danger')
            return redirect(url_for('main.contracts'))
        prepared.append((shot_type, file_obj, ext, mime, size))

    actor = current_user()
    file_name = '截图识别：' + '、'.join(
        (file_obj.filename or '') for _, file_obj, _, _, _ in prepared)
    contract = ContractFile(
        file_name=file_name[:256], stored_path='', file_type='img',
        file_size=sum(p[4] for p in prepared), engine='rules',
        status='uploaded', source='shots', created_by=actor.id,
    )
    db.session.add(contract)
    db.session.flush()

    shot_dir = os.path.join(contract_upload_dir(), 'shots', str(contract.id))
    os.makedirs(shot_dir, exist_ok=True)
    sections = {}
    shots = []
    for shot_type, file_obj, ext, mime, size in prepared:
        unique = secrets.token_hex(4)
        safe_name = '%s-%s.%s' % (shot_type, unique, ext)
        stored = os.path.join(shot_dir, safe_name)
        file_obj.save(stored)
        rel_path = 'uploads/shots/%d/%s' % (contract.id, safe_name)
        shot = ContractScreenshot(
            contract_id=contract.id, shot_type=shot_type,
            file_name=(file_obj.filename or safe_name)[:256],
            stored_path=rel_path, mime=mime, file_size=size or None,
            ocr_status='pending', created_by=actor.id,
        )
        db.session.add(shot)
        db.session.flush()
        text = _read_shot_text(shot)
        if text:
            sections[shot_type] = text
        shots.append(shot)
        audit_record('contract.shot.ocr', 'contract', contract.id, None,
                     result=shot.ocr_status,
                     after={'shot_id': shot.id, 'shot_type': shot_type,
                            'ocr_status': shot.ocr_status,
                            'text_len': len(text or ''),
                            'error_code': (shot.ocr_msg or '')[:80]},
                     critical=False)

    merged = merge_shot_texts(sections)
    meta = [{'shot_type': s.shot_type, 'shot_id': s.id,
             'ocr_status': s.ocr_status, 'ocr_msg': s.ocr_msg,
             'text_len': len(s.ocr_text or '')} for s in shots]
    result = recognize_shots(sections, file_name, shot_meta=meta)
    contract.raw_text = merged
    contract.result_json = json.dumps(result, ensure_ascii=False)
    contract.engine = result.get('engine', 'rules')
    contract.status = 'recognized'
    contract.error_msg = None
    failed = [t for t in SHOT_TYPES
              if any(s.shot_type == t and s.ocr_status != 'ok' for s in shots)]
    if failed:
        contract.error_msg = '截图 OCR 失败：%s（可在确认页手工填写）' % '、'.join(
            SHOT_LABELS[t] for t in failed)
    audit_record('contract.shots.upload', 'contract', contract.id, None,
                 after={'shot_types': [s.shot_type for s in shots],
                        'sizes': {s.shot_type: s.file_size for s in shots},
                        'mimes': {s.shot_type: s.mime for s in shots},
                        'ocr_status': {s.shot_type: s.ocr_status for s in shots}},
                 critical=False)
    db.session.commit()
    flash('截图识别完成，请对照原图核对识别结果后确认', 'success')
    return redirect(url_for('main.contract_confirm', contract_id=contract.id))


@bp.route('/contracts/shots/<int:shot_id>/image')
@login_required
def contract_shot_image(shot_id):
    """截图原图回显（Q9：仅 admin/operator 可看，viewer 403；禁缓存）。"""
    shot = db.session.get(ContractScreenshot, shot_id)
    if not shot:
        abort(404)
    user = current_user()
    if not user or user.role not in ('admin', 'operator'):
        audit_record('contract.shot.image_access', 'contract', shot.contract_id, None,
                     result='403', after={'shot_id': shot_id,
                                          'user_id': getattr(user, 'id', None)},
                     critical=False)
        db.session.commit()
        abort(403)
    path = screenshot_storage_path(shot)
    if not path or not os.path.isfile(path):
        audit_record('contract.shot.image_access', 'contract', shot.contract_id, None,
                     result='404', after={'shot_id': shot_id},
                     critical=False)
        db.session.commit()
        abort(404, description='截图文件不存在或已丢失')
    response = send_file(path, mimetype=shot.mime or 'application/octet-stream',
                         as_attachment=False, download_name=shot.file_name)
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


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
        'warranty_months': ('' if project.warranty_months is None
                            else str(project.warranty_months)),
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
    ratios = {}
    for nt in NODE_TYPES:
        nd = node_map.get(nt)
        # CR-04：系统不提供任何默认比例；未识别到就是空，必须人工填写。
        ratios[nt] = ''
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
        'warranty_months': ('' if result.get('warranty_months') in (None, '')
                            else str(result.get('warranty_months'))),
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
        'warranty_months': form.get('warranty_months') or '',
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
    cur_months = (current.get('warranty_months') or '').strip()
    new_months = '' if submission.warranty_months is None else str(submission.warranty_months)
    if cur_months != new_months:
        diffs.append({'field': 'warranty_months', 'label': '质保期（月）',
                      'old': cur_months or '—', 'new': new_months or '—', 'delta': ''})
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
    # CR-05 需求②：质保期是日期口径，先更新再 sync（合同变更不修改它）
    project.warranty_months = submission.warranty_months
    if not update_amounts:
        sync_project_dates(project)
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
        node.node_name = make_node_name(nt, ratio)
        node.calc_base = 'total'   # 统一口径：首付款也按合同总额
        node.due_amount = submission.node_amounts[nt]
        node.updated_by = operator_id
    sync_project_dates(project)


def _create_contract_project(submission, contract):
    """首次确认：创建项目 + 单科目 + 4 节点（金额来自服务端校验结果）。"""
    operator_id = _operator_id()
    project = Project(
        project_no=submission.project_no, name=submission.contract_name,
        status='进行中', remark=_contract_remark(submission, contract.file_name),
        warranty_months=submission.warranty_months,
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
                           node_name=make_node_name(nt, ratio), ratio=ratio,
                           calc_base='total',
                           due_amount=submission.node_amounts[nt], status='未触发',
                           updated_by=operator_id)
        db.session.add(node)
    sync_project_dates(project)
    return project


def _render_confirm(contract, result, *, values, is_update, project=None,
                    errors=None, diff=None, blockers=None, confirm_token=''):
    shots = sorted(contract.screenshots, key=lambda s: SHOT_TYPES.index(s.shot_type)) \
        if contract.file_type == 'img' else []
    return render_template('contract_confirm.html', contract=contract,
                           result=result, values=values, is_update=is_update,
                           project=project, errors=errors or [], diff=diff or [],
                           blockers=blockers or [], confirm_token=confirm_token,
                           node_order=CONTRACT_NODE_ORDER, shots=shots,
                           shot_labels=SHOT_LABELS, shot_types=SHOT_TYPES)


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
    guard = _guard_active_project(project)
    if guard:
        flash('关联项目已删除，请先在回收站恢复后再修改合同', 'danger')
        return redirect(url_for('main.contracts'))
    if project is not None and project.status == '已归档':
        flash('关联项目已归档（只读），不能修改合同；请先解除归档', 'danger')
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

    project = None
    if is_update:
        project = db.session.get(Project, contract.project_id)
        if not project:
            flash('关联项目已不存在，请重新确认', 'danger')
            return redirect(url_for('main.contracts'))
        if project.deleted_at is not None:
            flash('关联项目已删除，请先在回收站恢复后再修改合同', 'danger')
            return redirect(url_for('main.contracts'))
        if project.status == '已归档':
            flash('关联项目已归档（只读），不能修改合同；请先解除归档', 'danger')
            return redirect(url_for('main.contracts'))

    # ①② 截图均未识别出文字：必须人工对照原图核对后勾选确认，否则不生成项目
    shots = list(contract.screenshots) if contract.file_type == 'img' else []
    if shots:
        failed_types = {s.shot_type for s in shots if s.ocr_status != 'ok'}
        if all(t in failed_types for t in SHOT_REQUIRED) \
                and not (request.form.get('manual_confirm') or '').strip():
            return _render_confirm(
                contract, result,
                values=_raw_contract_form_values(request.form),
                is_update=is_update, project=project,
                errors=['截图①②均未能识别，请勾选「以上内容为人工录入，已对照原图核对」后再提交'])

    try:
        # CR-04：确认页不再有增补输入；回改保留项目当前增补合计，不清零。
        submission = validate_contract_submission(
            request.form,
            additional_amount=(project.total_additional() if project else ZERO))
    except ValidationError as exc:
        audit_record('contract.confirm.failed', 'contract', contract.id,
                     contract.project_id, result='failed',
                     after={'errors': exc.errors}, critical=False)
        db.session.commit()
        return _render_confirm(contract, result,
                               values=_raw_contract_form_values(request.form),
                               is_update=is_update, errors=exc.errors,
                               project=project)

    if is_update:
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
        for shot in shots:
            shot.project_id = project.id
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
                            'warranty_months': submission.warranty_months,
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
    for shot in shots:
        shot.project_id = project.id
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
    shots = list(contract.screenshots) if contract.file_type == 'img' else []
    if shots:
        # 只重跑失败/待识别的截图；已成功的不重复 OCR（4.7.3）
        sections = {}
        for shot in sorted(shots, key=lambda s: SHOT_TYPES.index(s.shot_type)):
            if shot.ocr_status == 'ok' and shot.ocr_text:
                sections[shot.shot_type] = shot.ocr_text
                continue
            text = _read_shot_text(shot)
            if text:
                sections[shot.shot_type] = text
            audit_record('contract.shot.ocr', 'contract', contract.id,
                         contract.project_id, result=shot.ocr_status,
                         after={'shot_id': shot.id, 'shot_type': shot.shot_type,
                                'ocr_status': shot.ocr_status,
                                'text_len': len(text or ''),
                                'error_code': (shot.ocr_msg or '')[:80]},
                         critical=False)
        result = recognize_shots(sections, contract.file_name,
                                 shot_meta=_shot_meta(shots))
        contract.raw_text = merge_shot_texts(sections)
        contract.result_json = json.dumps(result, ensure_ascii=False)
        contract.engine = result.get('engine', 'rules')
        if contract.status != 'confirmed':
            contract.status = 'recognized'
        contract.error_msg = None
        audit_record('contract.rerun', 'contract', contract.id, contract.project_id,
                     after={'status': contract.status,
                            'shot_ocr': {s.shot_type: s.ocr_status for s in shots}},
                     critical=False)
        db.session.commit()
        flash('已重新识别失败/待识别的截图（已成功的图未重复 OCR）', 'success')
        return redirect(url_for('main.contract_confirm', contract_id=contract.id))

    storage = contract_storage_path(contract)
    if not storage or not os.path.exists(storage):
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
    result, rec_msg = recognize_contract(text[:MAX_RECOGNIZE_CHARS], contract.file_name)
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


# ---------- 合同变更（批次 8） ----------

def _change_amount(raw, field, *, allow_negative=False):
    """合同变更金额解析：增补金额允许负数；原金额不允许负数。"""
    text = (raw or '').strip()
    if not text:
        raise ValidationError('%s：必填' % field)
    try:
        value = parse_money(text, allow_negative=allow_negative)
    except TypeError:
        raise ValidationError('%s：格式错误' % field)
    if value is None:
        raise ValidationError('%s：金额无效（最多两位小数%s）'
                              % (field, '，允许负数' if allow_negative else '，不能为负'))
    return value


def _split_amount(total, weights):
    """按权重把 amount 分摊到各科（ROUND_HALF_UP），尾差给最后一项。

    ``weights`` 为 Decimal 列表（非负）；返回与 weights 等长的 Decimal 列表，
    合计恰好等于 ``total``。最后一项过大时把超出部分从其它项回退。
    """
    count = len(weights)
    if count == 0:
        return []
    weight_sum = sum(weights, Decimal('0'))
    if weight_sum <= 0:
        weights = [Decimal('1')] * count
        weight_sum = Decimal(count)
    parts = [(Decimal(total) * w / weight_sum).quantize(CENT, rounding=ROUND_HALF_UP)
             for w in weights]
    residual = (Decimal(total) - sum(parts, Decimal('0'))).quantize(CENT)
    parts[-1] = (parts[-1] + residual).quantize(CENT)
    # 负数回退：原金额不允许负数，若最后一项为负则从其它项挪
    if parts[-1] < 0:
        need = -parts[-1]
        parts[-1] = ZERO
        for index in range(count - 2, -1, -1):
            if need <= 0:
                break
            move = min(parts[index], need)
            parts[index] -= move
            need -= move
        if need > 0:
            raise ValidationError('金额过小，无法按科目分摊，请调整金额')
    return parts


def _build_contract_change_plan(project, new_totals):
    """CR-04 逐科目合同变更计划。

    - 已付款节点（paid_amount > 0）的 due/paid/status 全部冻结不动；
    - 科目内差额（新总额 − 已付节点应付合计）按未付节点原应付比例分摊；
    - 尾差按 质保金→验收→进度→首付 归集，保证科目节点合计 = 科目新总额；
    - 新总额 < 已付合计 / 已开票 > 新应付 → 逐科目列出缺口并阻断。
    """
    items = list(project.items)
    if not items:
        raise ValidationError('项目没有费用科目，无法做合同变更')
    item_changes = []
    node_plan = []
    diff_rows = []
    node_rows = []
    errors = []

    for item in items:
        new_total = Decimal(new_totals.get(item.id) or 0).quantize(CENT)
        old_original = Decimal(item.original_amount or 0)
        old_additional = Decimal(item.additional_amount or 0)
        old_total = item.total_amount
        nodes = {n.node_type: n for n in item.nodes}
        missing = [nt for nt in NODE_TYPES if nt not in nodes]
        if missing:
            errors.append('科目「%s」付款节点不完整（缺 %s）'
                          % (item.item_name,
                             '、'.join(NODE_LABELS[nt] for nt in missing)))
            continue

        frozen = [n for n in item.nodes if Decimal(n.paid_amount or 0) > 0]
        frozen_due = sum((Decimal(n.due_amount or 0) for n in frozen), ZERO).quantize(CENT)
        if new_total < frozen_due:
            errors.append(
                '科目「%s」变更后总额 %s 小于已付节点应付合计 %s'
                '（已付节点冻结，不能下调到该金额以下）'
                % (item.item_name, fmt_money(new_total), fmt_money(frozen_due)))
        unfrozen = [n for n in item.nodes if n not in frozen]
        remaining = (new_total - frozen_due).quantize(CENT)

        share_map = {n.node_type: ZERO for n in unfrozen}
        if unfrozen:
            weights = [(Decimal(n.due_amount or 0) if Decimal(n.due_amount or 0) > 0
                        else Decimal(n.ratio or 0)) for n in unfrozen]
            if sum(weights, ZERO) <= 0:
                weights = [Decimal('1')] * len(unfrozen)
            try:
                shares = _split_amount(remaining, weights)
                share_map = {n.node_type: shares[i] for i, n in enumerate(unfrozen)}
                order = [nt for nt in ('warranty', 'acceptance', 'progress', 'first')
                         if nt in share_map]
                share_map = allocate_tail(share_map, remaining, order=order)
            except (ValidationError, ValueError) as exc:
                errors.extend(exc.errors if isinstance(exc, ValidationError)
                              else [str(exc)])
        elif remaining != 0:
            errors.append('科目「%s」已无未付节点，变更后总额必须等于已付节点应付合计 %s'
                          % (item.item_name, fmt_money(frozen_due)))

        for node in item.nodes:
            old_due = Decimal(node.due_amount or 0)
            paid = Decimal(node.paid_amount or 0)
            invoice = Decimal(node.invoice_amount or 0)
            if node in frozen:
                new_due = old_due
                new_status = node.status
            else:
                new_due = share_map.get(node.node_type, ZERO)
                new_status = derive_status(new_due, Decimal(node.applied_amount or 0),
                                           invoice, paid)
            node_plan.append({
                'node': node, 'item': item, 'node_type': node.node_type,
                'old_due': old_due, 'new_due': new_due,
                'ratio': Decimal(node.ratio or 0),
                'paid': paid, 'invoice': invoice,
                'frozen': node in frozen, 'new_status': new_status,
            })
            if invoice > new_due:
                errors.append(
                    '科目「%s」节点「%s」已开票 %s 大于变更后应付 %s，无法变更'
                    % (item.item_name, node.node_name, fmt_money(invoice),
                       fmt_money(new_due)))
            delta = (new_due - old_due).quantize(CENT)
            if delta != 0:
                node_rows.append({
                    'label': '%s · %s' % (item.item_name, node.node_name),
                    'old': fmt_money(old_due), 'new': fmt_money(new_due),
                    'delta': _signed_money(delta),
                    'frozen': node in frozen,
                })

        new_additional = (new_total - old_original).quantize(CENT)
        item_changes.append({
            'item': item,
            'old_original': old_original,
            'old_additional': old_additional,
            'old_total': old_total,
            'new_original': old_original,
            'new_additional': new_additional,
            'new_total': new_total,
            'frozen_due': frozen_due,
        })
        diff_rows.append({
            'label': '科目「%s」总额' % item.item_name,
            'old': fmt_money(old_total), 'new': fmt_money(new_total),
            'delta': _signed_money(new_total - old_total),
        })

    if errors:
        raise ValidationError(errors)
    new_total_sum = sum((c['new_total'] for c in item_changes), ZERO).quantize(CENT)
    return {'new_total': new_total_sum, 'item_changes': item_changes,
            'node_plan': node_plan, 'diff_rows': diff_rows,
            'node_rows': node_rows,
            'frozen_count': sum(1 for row in node_plan if row['frozen'])}


def _contract_change_token(project, new_totals):
    """差异确认凭据：与项目当前现值 + 各节点已发生额绑定，服务端重算。"""
    state = []
    for item in project.items:
        state.append([item.id, format(item.total_amount, 'f'), [
            [n.id, format(Decimal(n.due_amount or 0), 'f'),
             format(Decimal(n.paid_amount or 0), 'f'),
             format(Decimal(n.invoice_amount or 0), 'f')]
            for n in item.nodes]])
    payload = {'project_id': project.id, 'state': state,
               'new': [[iid, format(value, 'f')]
                       for iid, value in sorted(new_totals.items())]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


def _contract_change_inputs(project):
    """读取表单：每个科目一个 ``item_total_<id>``（变更后总额）。"""
    try:
        return validate_contract_change_items(project, request.form)
    except ValidationError:
        raise


def _contract_change_values(project):
    return {item.id: format(item.total_amount, 'f') for item in project.items}


def _apply_contract_change(project, plan):
    """应用变更：科目增补金额 + 未付节点应付/状态 + 节点变更备注；已付节点不动。"""
    operator_id = _operator_id()
    before = {'items': {c['item'].id: {'name': c['item'].item_name,
                                       'total': format(c['old_total'], 'f')}
                        for c in plan['item_changes']},
              'nodes': {row['node'].id: {'due': format(row['old_due'], 'f'),
                                         'paid': format(row['paid'], 'f'),
                                         'status': row['node'].status}
                        for row in plan['node_plan']}}
    for change in plan['item_changes']:
        item = change['item']
        item.additional_amount = change['new_additional']
        item.updated_by = operator_id
    after_nodes = {}
    for row in plan['node_plan']:
        node = row['node']
        if row['frozen']:
            # 已付款节点：due/paid/status 一律冻结
            continue
        note = '合同变更：应付 %s → %s（%s）' % (
            fmt_money(row['old_due']), fmt_money(row['new_due']),
            _signed_money(row['new_due'] - row['old_due']))
        node.due_amount = row['new_due']
        node.calc_base = 'total'
        node.status = row['new_status']
        node.change_note = (note if not node.change_note
                            else (node.change_note + '；' + note))[:255]
        node.updated_by = operator_id
        after_nodes[node.id] = {'due': format(row['new_due'], 'f'),
                                'status': row['new_status'],
                                'frozen': False}
    project.updated_by = operator_id
    audit_record(
        'project.contract_change', 'project', project.id, project.id,
        before=before,
        after={'items': {c['item'].id: {'name': c['item'].item_name,
                                        'total': format(c['new_total'], 'f')}
                         for c in plan['item_changes']},
               'nodes': after_nodes,
               'frozen_node_ids': [row['node'].id for row in plan['node_plan']
                                   if row['frozen']],
               'warranty_months': project.warranty_months,
               'due_dates_untouched': True,
               'total': format(plan['new_total'], 'f')},
        critical=True)
    db.session.commit()
    return plan['new_total']


def _render_contract_change(project, *, stage='form', plan=None, values=None,
                            token=''):
    items = list(project.items)
    return render_template('contract_change.html', project=project, items=items,
                           stage=stage, plan=plan, values=values or {},
                           confirm_token=token)


@bp.route('/projects/<int:project_id>/contract-change', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'operator')
def project_contract_change(project_id):
    """合同变更（CR-04）：逐科目改总额，已付节点冻结，差额分摊到未付节点。"""
    project = _get_project(project_id)
    guard = _guard_writable_project(project)
    if guard:
        return guard
    if not list(project.items):
        flash('项目没有费用科目，无法做合同变更', 'danger')
        return redirect(url_for('main.project_detail', project_id=project.id))
    values = _contract_change_values(project)

    if request.method == 'GET':
        return _render_contract_change(project, values=values)

    try:
        new_totals = _contract_change_inputs(project)
        plan = _build_contract_change_plan(project, new_totals)
    except ValidationError as exc:
        for message in exc.errors:
            flash(message, 'danger')
        return _render_contract_change(project, values=values)

    token = _contract_change_token(project, new_totals)
    submitted = (request.form.get('confirm_token') or '').strip()
    if submitted != token:
        # 第一步：差异清单 + 二次确认；不写库
        return _render_contract_change(project, stage='confirm', plan=plan,
                                       values=values, token=token)

    new_total = _apply_contract_change(project, plan)
    flash('合同变更已保存：合同总额 %s 元；%d 个已付节点冻结未改，差额已分摊到未付节点'
          % (fmt_money(new_total), plan['frozen_count']), 'success')
    return redirect(url_for('main.project_detail', project_id=project.id))


@bp.route('/projects/<int:project_id>/contract-change/preview', methods=['POST'])
@login_required
@role_required('admin', 'operator')
def project_contract_change_preview(project_id):
    """兼容旧前端：合同变更第一步（只出差异清单，不写库）。"""
    project = _get_project(project_id)
    guard = _guard_writable_project(project)
    if guard:
        return guard
    values = _contract_change_values(project)
    try:
        new_totals = _contract_change_inputs(project)
        plan = _build_contract_change_plan(project, new_totals)
    except ValidationError as exc:
        for message in exc.errors:
            flash(message, 'danger')
        return _render_contract_change(project, values=values)
    return _render_contract_change(project, stage='confirm', plan=plan,
                                   values=values,
                                   token=_contract_change_token(project, new_totals))


# ---------- 报表与提醒 ----------

def _reports_data():
    """报表数据（页面与 Excel 导出同源，避免两套口径）。"""
    projects = (Project.query.filter(Project.deleted_at.is_(None))
                .order_by(Project.created_at.desc()).all())
    rows = []
    for p in projects:
        due = sum_money(n.due_amount for n in p.nodes)
        applied = sum_money(Decimal(n.applied_amount or 0) for n in p.nodes)
        invoice = sum_money(n.invoice_amount for n in p.nodes)
        paid = sum_money(n.paid_amount for n in p.nodes)
        unpaid = (due - paid).quantize(CENT)
        if unpaid < 0:
            unpaid = ZERO   # 超付部分单列 overpaid，未付不出现负数
        overpaid = sum_money(
            [(Decimal(n.paid_amount or 0) - Decimal(n.due_amount or 0)).quantize(CENT)
             for n in p.nodes
             if Decimal(n.paid_amount or 0) > Decimal(n.due_amount or 0)])
        progress = ((paid / due * Decimal('100')).quantize(Decimal('0.1'))
                    if due else ZERO)
        rows.append({
            'project': p, 'due': due, 'applied': applied,
            'invoice': invoice, 'paid': paid,
            'unpaid': unpaid, 'progress': progress, 'overpaid': overpaid,
        })
    totals = {
        'due': sum_money(r['due'] for r in rows),
        'applied': sum_money(r['applied'] for r in rows),
        'invoice': sum_money(r['invoice'] for r in rows),
        'paid': sum_money(r['paid'] for r in rows),
        'unpaid': sum_money(r['unpaid'] for r in rows),
        'overpaid': sum_money(r['overpaid'] for r in rows),
    }
    # 待办提醒：质保到期、已申请待付款
    today = timeutil.local_today()
    reminders = []
    for p in projects:
        if p.warranty_due_date:
            days = (p.warranty_due_date - today).days
            if days <= 60:
                reminders.append(('warranty', p, days))
        for n in p.nodes:
            if n.status in ('未开票已申请', '已申请'):
                reminders.append(('pending_pay', n, None))
    return projects, rows, totals, reminders


@bp.route('/reports')
@login_required
def reports():
    _projects, rows, totals, reminders = _reports_data()
    return render_template('reports.html', rows=rows,
                           total_due=totals['due'], total_applied=totals['applied'],
                           total_paid=totals['paid'],
                           total_unpaid=totals['unpaid'],
                           total_overpaid=totals['overpaid'], reminders=reminders)


# ---------- 导出（批次 6a） ----------

XLSX_MIMETYPE = ('application/vnd.openxmlformats-officedocument'
                 '.spreadsheetml.sheet')


def _record_export(action, object_type, object_id, project_id, after):
    """导出操作写审计；失败不阻断下载（只回滚审计，不影响只读导出）。"""
    try:
        audit_record(action, object_type, object_id, project_id,
                     after=after, critical=False)
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        return False


@bp.route('/projects/<int:project_id>/export.xlsx')
@login_required
def project_export(project_id):
    """导出单个项目明细（项目信息/科目/节点/申请/付款/验收）。"""
    project = _get_project(project_id, include_deleted=True)
    workbook = build_project_workbook(project)
    filename = export_filename(
        '项目明细', [project.project_no] + (['回收站'] if project.deleted_at else []),
        'xlsx')
    payload = workbook_bytes(workbook)
    _record_export('export.project', 'project', project.id, project.id, {
        'format': 'xlsx', 'file_name': filename,
        'sheets': ['项目信息', '费用科目', '付款节点', '付款申请', '付款记录', '验收记录'],
    })
    return send_file(io.BytesIO(payload), as_attachment=True,
                     download_name=filename, mimetype=XLSX_MIMETYPE)


@bp.route('/reports/export.xlsx')
@login_required
def reports_export():
    """导出付款台账报表汇总 + 待办提醒。"""
    _projects, rows, totals, reminders = _reports_data()
    workbook = build_reports_workbook(rows, totals, reminders)
    filename = export_filename('报表汇总', ['全部项目'], 'xlsx')
    payload = workbook_bytes(workbook)
    _record_export('export.reports', 'report', None, None, {
        'format': 'xlsx', 'file_name': filename, 'projects': len(rows),
        'sheets': ['付款台账总览', '待办提醒'],
    })
    return send_file(io.BytesIO(payload), as_attachment=True,
                     download_name=filename, mimetype=XLSX_MIMETYPE)


@bp.route('/admin/audit/export.csv')
@login_required
@role_required('admin')
def audit_export():
    """按当前筛选条件导出审计日志 CSV（不分页、只读）。"""
    project_id, actor, action, date_from, date_to, filters, errors = _audit_filters()
    if errors:
        for message in errors:
            flash(message, 'danger')
        return redirect(url_for('main.audit_logs', **filters))
    query = _audit_query(project_id, actor, action, date_from, date_to)
    logs = (query.order_by(AuditLog.ts.desc(), AuditLog.id.desc())
            .limit(AUDIT_EXPORT_LIMIT).all())
    payload = audit_csv_bytes(logs)
    filename = export_filename('审计日志', _audit_filename_parts(filters), 'csv')
    _record_export('export.audit', 'audit', None, project_id, {
        'format': 'csv', 'file_name': filename, 'rows': len(logs),
        'filters': filters,
    })
    return send_file(io.BytesIO(payload), as_attachment=True,
                     download_name=filename, mimetype='text/csv')


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
    try:
        password = validate_password(
            password, username=username, field='初始密码',
            min_length=int(current_app.config.get('PASSWORD_MIN_LENGTH', 8) or 8))
    except ValidationError as exc:
        for msg in exc.errors:
            flash(msg, 'danger')
        return redirect(url_for('main.users'))
    user = User(username=username, display_name=display_name, role=role,
                must_change_password=True)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    audit_record('user.create', 'user', user.id,
                 after={'username': user.username, 'display_name': user.display_name,
                        'role': user.role, 'is_active': True,
                        'must_change_password': True})
    db.session.commit()
    flash(f'用户「{display_name}」已创建（首次登录需修改密码）', 'success')
    return redirect(url_for('main.users'))


@bp.route('/admin/users/<int:user_id>/reset-password', methods=['POST'])
@login_required
@role_required('admin')
def user_reset_password(user_id):
    """管理员重置密码：需 ≥8 位、非默认口令；该用户下次登录强制改密。"""
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    try:
        new_password = validate_password(
            request.form.get('password'), username=user.username, field='新密码',
            min_length=int(current_app.config.get('PASSWORD_MIN_LENGTH', 8) or 8))
    except ValidationError as exc:
        for msg in exc.errors:
            flash(msg, 'danger')
        return redirect(url_for('main.users'))
    user.set_password(new_password)
    user.must_change_password = True
    user.password_updated_at = datetime.utcnow()
    session_guard.revoke_user(user.id)
    audit_record('user.reset_password', 'user', user.id,
                 after={'username': user.username, 'must_change_password': True})
    db.session.commit()
    flash('已重置「%s」的密码；该用户下次登录需先修改密码' % user.display_name, 'success')
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
    if not user.is_active:
        session_guard.revoke_user(user.id)   # 停用后该用户旧会话立即失效
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
    session_guard.revoke_user(user.id)
    db.session.delete(user)
    db.session.commit()
    flash(f'用户「{user.display_name}」已删除', 'success')
    return redirect(url_for('main.users'))


# ---------- 操作审计（只读） ----------

AUDIT_PAGE_SIZE = 50
#: 单次 CSV 导出上限（防止误导出百万行拖垮内存/浏览器）
AUDIT_EXPORT_LIMIT = 50000


def _audit_filters():
    """解析审计筛选参数（页面与 CSV 导出共用，保证同条件同结果）。"""
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
    return project_id, actor, action, date_from, date_to, filters, errors


def _audit_query(project_id, actor, action, date_from, date_to):
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
        query = query.filter(AuditLog.ts >= timeutil.local_date_start_utc(date_from))
    if date_to:
        query = query.filter(AuditLog.ts < timeutil.local_date_end_utc(date_to))
    return query


def _audit_filename_parts(filters):
    """把审计筛选条件转成文件名片段（如 项目3_张三_node.pay_20251001-20251023）。"""
    parts = []
    if filters.get('project_id'):
        parts.append('项目%s' % filters['project_id'])
    if filters.get('actor'):
        parts.append(filters['actor'])
    if filters.get('action'):
        parts.append(filters['action'])
    if filters.get('date_from') or filters.get('date_to'):
        parts.append('%s-%s' % (filters.get('date_from', '起始'),
                                filters.get('date_to', '至今')))
    return parts or ['全量']


@bp.route('/admin/audit')
@login_required
@role_required('admin')
def audit_logs():
    project_id, actor, action, date_from, date_to, filters, errors = _audit_filters()
    query = _audit_query(project_id, actor, action, date_from, date_to)

    total = query.count()
    page = max(1, request.args.get('page', 1, type=int) or 1)
    pages = max(1, (total + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)
    page = min(page, pages)
    rows = (query.order_by(AuditLog.ts.desc(), AuditLog.id.desc())
            .offset((page - 1) * AUDIT_PAGE_SIZE)
            .limit(AUDIT_PAGE_SIZE).all())
    projects = Project.query.order_by(Project.created_at.desc()).limit(200).all()
    return render_template('audit.html', rows=rows, page=page, pages=pages,
                           total=total, filters=filters, projects=projects,
                           actions=action_options(action or None), errors=errors,
                           page_size=AUDIT_PAGE_SIZE)


# ---------- 通用辅助 ----------

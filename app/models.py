# -*- coding: utf-8 -*-
"""付款进度管理系统 - SQLAlchemy 数据模型"""
from datetime import datetime
from decimal import Decimal

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

from .money import CENT, ZERO, mul_ratio

db = SQLAlchemy()


class User(db.Model):
    """系统用户"""
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    display_name = db.Column(db.String(64), nullable=False)
    role = db.Column(db.String(16), nullable=False, default='viewer')  # admin / operator / viewer
    is_active = db.Column(db.Boolean, nullable=False, default=True)    # 停用后不能登录
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)  # 首次/重置后强制改密
    password_updated_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)      # 存储 UTC，展示转本地时

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    def to_dict(self):
        return {
            'id': self.id, 'username': self.username,
            'display_name': self.display_name, 'role': self.role,
            'is_active': bool(self.is_active),
        }


class Project(db.Model):
    """项目主表"""
    __tablename__ = 'projects'
    id = db.Column(db.Integer, primary_key=True)
    project_no = db.Column(db.String(64), unique=True, nullable=False, index=True)
    name = db.Column(db.String(256), nullable=False)
    status = db.Column(db.String(16), nullable=False, default='进行中')  # 进行中/已完成/已归档
    trial_date = db.Column(db.Date, nullable=True)          # 试运行日期
    acceptance_date = db.Column(db.Date, nullable=True)     # 验收日期
    warranty_due_date = db.Column(db.Date, nullable=True)   # 质保到期日期 = 验收日期+质保期（CR-05 起可配置）
    # CR-05：质保期（月）。空 = 未填，不推算到期日（不再写死 36）
    warranty_months = db.Column(db.Integer, nullable=True)
    remark = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)      # 存储 UTC，展示转本地时
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.Column(db.Integer, nullable=True)   # 最后修改人（不建外键，避免删用户受阻）
    # 软删除（批次 4c）：删除=回收站，仍可看/恢复；归档是业务结束，仍可见
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_by = db.Column(db.Integer, nullable=True)

    items = db.relationship('ProjectItem', backref='project', cascade='all, delete-orphan', lazy='selectin')
    nodes = db.relationship('PaymentNode', backref='project', cascade='all, delete-orphan', lazy='selectin')
    acceptances = db.relationship('AcceptanceRecord', backref='project', cascade='all, delete-orphan', lazy='selectin')

    def total_original(self):
        return sum((Decimal(i.original_amount or 0) for i in self.items), ZERO)

    def total_additional(self):
        return sum((Decimal(i.additional_amount or 0) for i in self.items), ZERO)

    def total_amount(self):
        return (self.total_original() + self.total_additional()).quantize(CENT)

    def to_dict(self):
        return {
            'id': self.id, 'project_no': self.project_no, 'name': self.name,
            'status': self.status,
            'trial_date': self.trial_date.isoformat() if self.trial_date else None,
            'acceptance_date': self.acceptance_date.isoformat() if self.acceptance_date else None,
            'warranty_due_date': self.warranty_due_date.isoformat() if self.warranty_due_date else None,
            'warranty_months': self.warranty_months,
            'total_original': self.total_original(),
            'total_additional': self.total_additional(),
            'total_amount': self.total_amount(),
            'remark': self.remark,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else None,
        }


class ProjectItem(db.Model):
    """项目科目（费用项）"""
    __tablename__ = 'project_items'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    item_name = db.Column(db.String(128), nullable=False)
    tax_rate = db.Column(db.String(32), nullable=True)     # 如 13% / 6%
    original_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    additional_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.Column(db.Integer, nullable=True)

    nodes = db.relationship('PaymentNode', backref='item', cascade='all, delete-orphan', lazy='selectin')

    @property
    def total_amount(self):
        return (Decimal(self.original_amount or 0)
                + Decimal(self.additional_amount or 0)).quantize(CENT)

    def to_dict(self):
        return {
            'id': self.id, 'project_id': self.project_id,
            'item_name': self.item_name, 'tax_rate': self.tax_rate,
            'original_amount': float(self.original_amount or 0),
            'additional_amount': float(self.additional_amount or 0),
            'total_amount': float(self.total_amount),
        }


class PaymentNode(db.Model):
    """付款节点（实例）：每个科目下按节点模板生成"""
    __tablename__ = 'payment_nodes'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('project_items.id'), nullable=False)
    node_type = db.Column(db.String(16), nullable=False)   # first/progress/acceptance/warranty
    node_name = db.Column(db.String(64), nullable=False)   # 首付款30% / 进度款52% / 验收款15% / 质保金3%
    ratio = db.Column(db.Numeric(6, 4), nullable=False)    # 0.30 / 0.52 / 0.15 / 0.03
    calc_base = db.Column(db.String(16), nullable=False)   # original=按原金额 / total=按总金额
    due_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)   # 应付
    due_date = db.Column(db.Date, nullable=True)           # 应付日期（质保金=验收日期+3年）
    trigger_date = db.Column(db.Date, nullable=True)       # 实际触发日期（试运行/验收日期等）
    applied_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)  # 申请累计（CR-04）
    invoice_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    paid_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    status = db.Column(db.String(16), nullable=False, default='未触发')  # 兼容缓存，见 app/nodes.py
    change_note = db.Column(db.String(255), nullable=True)  # 合同变更前后金额差异备注（CR-04）
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.Column(db.Integer, nullable=True)

    applications = db.relationship('PaymentApplication', backref='node', cascade='all, delete-orphan', lazy='selectin')
    invoices = db.relationship('PaymentInvoice', backref='node', cascade='all, delete-orphan', lazy='selectin')

    def recalc(self):
        """根据科目金额与配置重算应付金额（Decimal + ROUND_HALF_UP）"""
        base = ZERO
        if self.item_id:
            it = db.session.get(ProjectItem, self.item_id)
            if it is not None:
                base = (Decimal(it.original_amount or 0) if self.calc_base == 'original'
                        else it.total_amount)
        self.due_amount = mul_ratio(base, Decimal(self.ratio or 0))

    @property
    def remaining_amount(self):
        """未付应付 = 应付 - 已付（Decimal，两位小数）"""
        return (Decimal(self.due_amount or 0) - Decimal(self.paid_amount or 0)).quantize(CENT)

    @property
    def has_business(self):
        """节点是否已发生资金业务（付款/开票/状态变化/申请单）。

        合同回改时这类节点必须冻结，不允许被口径变更改写。
        """
        return (self.status != '未触发'
                or Decimal(self.applied_amount or 0) > 0
                or Decimal(self.invoice_amount or 0) > 0
                or Decimal(self.paid_amount or 0) > 0
                or len(self.applications) > 0
                or len(self.invoices) > 0)

    @property
    def has_occurred(self):
        """节点是否已有业务痕迹（含试运行/验收联动日期）。"""
        return (self.has_business
                or self.trigger_date is not None
                or self.due_date is not None)

    def to_dict(self):
        return {
            'id': self.id, 'project_id': self.project_id, 'item_id': self.item_id,
            'item_name': self.item.item_name if self.item else None,
            'node_type': self.node_type, 'node_name': self.node_name,
            'ratio': float(self.ratio or 0), 'calc_base': self.calc_base,
            'due_amount': float(self.due_amount or 0),
            'due_date': self.due_date.isoformat() if self.due_date else None,
            'trigger_date': self.trigger_date.isoformat() if self.trigger_date else None,
            'applied_amount': float(self.applied_amount or 0),
            'invoice_amount': float(self.invoice_amount or 0),
            'paid_amount': float(self.paid_amount or 0),
            'status': self.status,
        }


class PaymentInvoice(db.Model):
    """发票登记明细（CR-04）：与申请、付款完全并列，可无申请直接登记。"""
    __tablename__ = 'payment_invoices'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    node_id = db.Column(db.Integer, db.ForeignKey('payment_nodes.id'), nullable=False)
    invoice_no = db.Column(db.String(128), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    invoice_date = db.Column(db.Date, nullable=True)
    remark = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)   # UTC
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    __table_args__ = (db.Index('ix_payment_invoices_node_id', 'node_id'),
                      db.Index('ix_payment_invoices_project_id', 'project_id'))

    def to_dict(self):
        return {
            'id': self.id, 'node_id': self.node_id, 'project_id': self.project_id,
            'invoice_no': self.invoice_no, 'amount': float(self.amount or 0),
            'invoice_date': self.invoice_date.isoformat() if self.invoice_date else None,
            'remark': self.remark,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else None,
        }


class PaymentApplication(db.Model):
    """付款申请单"""
    __tablename__ = 'payment_applications'
    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, db.ForeignKey('payment_nodes.id'), nullable=False)
    apply_no = db.Column(db.String(64), unique=True, nullable=False)
    apply_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    invoice_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    invoice_no = db.Column(db.String(128), nullable=True)
    #: 已废弃（CR-05 需求③：说明字段取消）；列保留以兼容历史数据与回滚，代码不再读写。
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(16), nullable=False, default='未开票已申请')  # 未开票已申请/已申请/已付款
    applicant_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    applicant_name = db.Column(db.String(64), nullable=True)
    apply_time = db.Column(db.DateTime, default=datetime.utcnow)   # UTC

    records = db.relationship('PaymentRecord', backref='application', cascade='all, delete-orphan', lazy='selectin')

    def to_dict(self):
        return {
            'id': self.id, 'node_id': self.node_id,
            'apply_no': self.apply_no, 'apply_amount': float(self.apply_amount or 0),
            'invoice_amount': float(self.invoice_amount or 0), 'invoice_no': self.invoice_no,
            'status': self.status,
            'applicant_name': self.applicant_name,
            'apply_time': self.apply_time.strftime('%Y-%m-%d %H:%M') if self.apply_time else None,
        }


class PaymentRecord(db.Model):
    """实际付款记录"""
    __tablename__ = 'payment_records'
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('payment_applications.id'), nullable=True)
    node_id = db.Column(db.Integer, db.ForeignKey('payment_nodes.id'), nullable=False)
    paid_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    pay_date = db.Column(db.Date, nullable=True)
    pay_method = db.Column(db.String(64), nullable=True)   # 银行转账/支票/电汇等
    voucher_no = db.Column(db.String(128), nullable=True)
    operator_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    operator_name = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)   # UTC

    def to_dict(self):
        return {
            'id': self.id, 'application_id': self.application_id, 'node_id': self.node_id,
            'paid_amount': float(self.paid_amount or 0),
            'pay_date': self.pay_date.isoformat() if self.pay_date else None,
            'pay_method': self.pay_method, 'voucher_no': self.voucher_no,
            'operator_name': self.operator_name,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else None,
        }


class ContractFile(db.Model):
    """合同文件上传与识别记录"""
    __tablename__ = 'contract_files'
    id = db.Column(db.Integer, primary_key=True)
    file_name = db.Column(db.String(256), nullable=False)       # 原始文件名
    stored_path = db.Column(db.String(512), nullable=False)     # 存储相对路径（instance/uploads/...）
    file_type = db.Column(db.String(16), nullable=False)        # pdf / docx / txt / md / img
    file_size = db.Column(db.Integer, default=0)
    engine = db.Column(db.String(32), default='rules')          # rules=本地规则 / llm=大模型适配器
    status = db.Column(db.String(16), default='uploaded')       # uploaded / recognized / confirmed / failed
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    contract_name = db.Column(db.String(256), nullable=True)    # 确认时使用的项目/合同名称
    result_json = db.Column(db.Text, nullable=True)             # 识别结果 JSON（字段/节点/出处）
    raw_text = db.Column(db.Text, nullable=True)                # 提取的合同全文
    error_msg = db.Column(db.String(512), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)   # UTC
    confirmed_by = db.Column(db.Integer, nullable=True)      # 最后一次确认/回改人
    confirmed_at = db.Column(db.DateTime, nullable=True)     # 最后一次确认/回改时间（UTC）
    # CR-05：file=上传合同文件（旧入口兜底）/ shots=按模板上传的关键信息截图任务
    source = db.Column(db.String(16), nullable=False, default='file')

    project = db.relationship('Project', backref='contracts', lazy='selectin')

    def to_dict(self):
        return {
            'id': self.id, 'file_name': self.file_name, 'file_type': self.file_type,
            'file_size': self.file_size, 'engine': self.engine, 'status': self.status,
            'project_id': self.project_id, 'contract_name': self.contract_name,
            'source': self.source,
            'error_msg': self.error_msg,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else None,
        }


class ContractScreenshot(db.Model):
    """合同关键信息截图（CR-05 需求①）。

    一次「截图识别」= 一条 ``contract_files`` 任务（``file_type='img'``、
    ``source='shots'``）+ 2~3 条截图明细；确认生成项目后回填 ``project_id``，
    项目详情据此直接展示原图（无需再翻识别任务）。
    """
    __tablename__ = 'contract_screenshots'
    id = db.Column(db.Integer, primary_key=True)
    contract_id = db.Column(db.Integer, db.ForeignKey('contract_files.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    shot_type = db.Column(db.String(16), nullable=False)   # amount / payment / warranty
    file_name = db.Column(db.String(256), nullable=False)  # 原始文件名
    stored_path = db.Column(db.String(512), nullable=False)  # 相对 UPLOAD_DIR
    mime = db.Column(db.String(64), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)
    ocr_text = db.Column(db.Text, nullable=True)           # 该张截图 OCR 文本（复核识别来源）
    ocr_status = db.Column(db.String(16), nullable=False, default='pending')  # pending/ok/failed/skipped
    ocr_msg = db.Column(db.String(255), nullable=True)
    created_by = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)   # UTC

    contract = db.relationship('ContractFile', backref='screenshots', lazy='selectin')

    __table_args__ = (db.Index('ix_contract_screenshots_contract_id', 'contract_id'),
                      db.Index('ix_contract_screenshots_project_type', 'project_id', 'shot_type'))

    def to_dict(self):
        return {
            'id': self.id, 'contract_id': self.contract_id, 'project_id': self.project_id,
            'shot_type': self.shot_type, 'file_name': self.file_name,
            'mime': self.mime, 'file_size': self.file_size,
            'ocr_status': self.ocr_status, 'ocr_msg': self.ocr_msg,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else None,
        }


class NodeActionToken(db.Model):
    """节点操作令牌的消费记录（CR-05 需求⑤防重放）。

    令牌本身是无状态 HMAC（不入 Cookie 会话）；提交时把令牌里的 ``jti``
    插入本表，主键冲突即代表「同一令牌已提交过」。
    """
    __tablename__ = 'node_action_tokens'
    jti = db.Column(db.String(64), primary_key=True)
    node_id = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(16), nullable=False)      # apply / invoice / pay
    session_sid = db.Column(db.String(64), nullable=True)  # 绑定的登录会话
    expires_at = db.Column(db.DateTime, nullable=False)    # UTC
    consumed_at = db.Column(db.DateTime, nullable=False)   # UTC
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.Index('ix_node_action_tokens_expires_at', 'expires_at'),)


class AcceptanceRecord(db.Model):
    """验收记录"""
    __tablename__ = 'acceptance_records'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    acceptance_date = db.Column(db.Date, nullable=False)
    conclusion = db.Column(db.String(256), nullable=True)   # 验收结论
    acceptor = db.Column(db.String(64), nullable=True)      # 验收人
    remark = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)   # UTC

    def to_dict(self):
        return {
            'id': self.id, 'project_id': self.project_id,
            'acceptance_date': self.acceptance_date.isoformat() if self.acceptance_date else None,
            'conclusion': self.conclusion, 'acceptor': self.acceptor,
            'remark': self.remark,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else None,
        }


class ImportBatch(db.Model):
    """Excel 导入批次记录（批次 6b）

    - 每次写库一条；``dry_run=True`` 的试运行也留一条（status='dry-run'）；
    - ``file_hash`` = 源文件 SHA-256，用于追溯"同一份表是否重复导入"；
    - 计数为本次实际写入的行数，便于对账；
    - 回滚：当前只做到「按批次查出导入了哪些行」（``ImportBatchRow``），
      回滚脚本留 TODO（见 docs/roadmap.md 阶段 2）。
    """
    __tablename__ = 'import_batches'
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    file_name = db.Column(db.String(256), nullable=False)
    file_hash = db.Column(db.String(64), nullable=False, index=True)
    mode = db.Column(db.String(16), nullable=False, default='import')   # import / dry-run
    status = db.Column(db.String(16), nullable=False, default='success')  # success / dry-run / failed
    projects_created = db.Column(db.Integer, nullable=False, default=0)
    items_created = db.Column(db.Integer, nullable=False, default=0)
    nodes_created = db.Column(db.Integer, nullable=False, default=0)
    applications_created = db.Column(db.Integer, nullable=False, default=0)
    records_created = db.Column(db.Integer, nullable=False, default=0)
    acceptances_created = db.Column(db.Integer, nullable=False, default=0)
    # 更新模式（--update-existing）计数
    projects_updated = db.Column(db.Integer, nullable=False, default=0)
    items_updated = db.Column(db.Integer, nullable=False, default=0)
    nodes_updated = db.Column(db.Integer, nullable=False, default=0)
    frozen_count = db.Column(db.Integer, nullable=False, default=0)    # 有业务、禁止改金额的节点
    skipped_count = db.Column(db.Integer, nullable=False, default=0)   # 已存在项目/记录跳过
    operator = db.Column(db.String(64), nullable=True)     # 命令行导出/系统
    report_path = db.Column(db.String(512), nullable=True)
    error_msg = db.Column(db.Text, nullable=True)

    rows = db.relationship('ImportBatchRow', backref='batch',
                           cascade='all, delete-orphan', lazy='selectin')

    def to_dict(self):
        return {
            'id': self.id,
            'ts': self.ts.strftime('%Y-%m-%d %H:%M:%S') if self.ts else None,
            'file_name': self.file_name, 'file_hash': self.file_hash,
            'mode': self.mode, 'status': self.status,
            'projects_created': self.projects_created,
            'items_created': self.items_created,
            'nodes_created': self.nodes_created,
            'applications_created': self.applications_created,
            'records_created': self.records_created,
            'acceptances_created': self.acceptances_created,
            'projects_updated': self.projects_updated,
            'items_updated': self.items_updated,
            'nodes_updated': self.nodes_updated,
            'frozen_count': self.frozen_count,
            'skipped_count': self.skipped_count,
            'operator': self.operator, 'report_path': self.report_path,
            'error_msg': self.error_msg,
        }


class ImportBatchRow(db.Model):
    """导入批次明细（批次 6b）：记录本次导入涉及的每一行，供按批次追溯。

    ``action``：create / update / skip / frozen。
    回滚脚本待做（TODO）：按 batch_id 逆序删除 create 行、恢复 update 的 before 值。
    """
    __tablename__ = 'import_batch_rows'
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey('import_batches.id'),
                         nullable=False, index=True)
    action = db.Column(db.String(16), nullable=False, index=True)
    object_type = db.Column(db.String(16), nullable=False)   # project/item/node/application/record/acceptance
    object_id = db.Column(db.Integer, nullable=True)
    project_no = db.Column(db.String(64), nullable=True)
    item_name = db.Column(db.String(128), nullable=True)
    node_type = db.Column(db.String(16), nullable=True)
    detail = db.Column(db.Text, nullable=True)
    # 更新行的原值/新值（JSON），供「按批次回滚」恢复；新建行为空
    before_json = db.Column(db.Text, nullable=True)
    after_json = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            'id': self.id, 'batch_id': self.batch_id, 'action': self.action,
            'object_type': self.object_type, 'object_id': self.object_id,
            'project_no': self.project_no, 'item_name': self.item_name,
            'node_type': self.node_type, 'detail': self.detail,
            'before_json': self.before_json, 'after_json': self.after_json,
        }


class UserSession(db.Model):
    """服务端会话：登出/改密/重置后旧 Cookie 立即失效；敏感操作可轮换当前会话。"""
    __tablename__ = 'user_sessions'
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    ip = db.Column(db.String(45), nullable=True)
    ua = db.Column(db.String(256), nullable=True)


class AuditLog(db.Model):
    """操作审计日志（只追加，不更新不删除；P0-02）

    - ``ts`` 统一存 UTC；页面按 UTC 展示并标注；
    - ``before_json`` / ``after_json`` 存关注字段快照（JSON 字符串），
      金额/状态/日期类变更必须留 before/after；
    - 不建外键，保证项目/用户/合同被物理删除后日志仍然完整。
    """
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    actor_id = db.Column(db.Integer, nullable=True)
    actor_name = db.Column(db.String(64), nullable=True)
    actor_role = db.Column(db.String(16), nullable=True)            # 操作人角色快照
    action = db.Column(db.String(64), nullable=False, index=True)   # 如 project.edit / node.pay
    object_type = db.Column(db.String(32), nullable=True)           # project / item / node / contract / user / auth
    object_id = db.Column(db.Integer, nullable=True)
    project_id = db.Column(db.Integer, nullable=True, index=True)   # 便于按项目检索
    before_json = db.Column(db.Text, nullable=True)
    after_json = db.Column(db.Text, nullable=True)
    ip = db.Column(db.String(45), nullable=True)
    ua = db.Column(db.String(256), nullable=True)
    result = db.Column(db.String(16), nullable=False, default='ok')  # ok / failed / denied / blocked

    __table_args__ = (
        db.Index('ix_audit_actor_ts', 'actor_id', 'ts'),
        db.Index('ix_audit_project_ts', 'project_id', 'ts'),
        db.Index('ix_audit_action_ts', 'action', 'ts'),
        db.Index('ix_audit_object_ts', 'object_type', 'object_id', 'ts'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'ts': self.ts.strftime('%Y-%m-%d %H:%M:%S') if self.ts else None,
            'actor_id': self.actor_id, 'actor_name': self.actor_name,
            'actor_role': self.actor_role,
            'action': self.action, 'object_type': self.object_type,
            'object_id': self.object_id, 'project_id': self.project_id,
            'before_json': self.before_json, 'after_json': self.after_json,
            'ip': self.ip, 'ua': self.ua, 'result': self.result,
        }
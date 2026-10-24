-- CR-04 三本账迁移（SQLite；生产升级可用 SQL Server 对应语法手工执行）
-- 说明：应用启动时 app/schema.py 也会幂等补列/建表；本文件是给运维留档的等价 DDL。

-- 1) 付款节点：申请累计金额、合同变更备注
ALTER TABLE payment_nodes ADD COLUMN applied_amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE payment_nodes ADD COLUMN change_note VARCHAR(255);

-- 2) 开票明细（一张发票可覆盖多个节点时，按节点各记一行；发票号不设唯一约束）
CREATE TABLE IF NOT EXISTS payment_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects (id),
    node_id INTEGER NOT NULL REFERENCES payment_nodes (id),
    invoice_no VARCHAR(64) NOT NULL,
    amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    invoice_date DATE,
    remark VARCHAR(255),
    created_by INTEGER REFERENCES users (id),
    created_at DATETIME
);
CREATE INDEX IF NOT EXISTS ix_payment_invoices_project_id ON payment_invoices (project_id);
CREATE INDEX IF NOT EXISTS ix_payment_invoices_node_id ON payment_invoices (node_id);
CREATE INDEX IF NOT EXISTS ix_payment_invoices_invoice_no ON payment_invoices (invoice_no);

-- 3) 历史数据回填：已申请但未记录累计的节点按“已申请/未开票已申请”回填
UPDATE payment_nodes
   SET applied_amount = due_amount
 WHERE (applied_amount IS NULL OR applied_amount = 0)
   AND status IN ('已申请', '未开票已申请')
   AND paid_amount = 0;

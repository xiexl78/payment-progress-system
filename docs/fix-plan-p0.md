# P0 修复设计：合同回改数据保护（P0-1/P0-2）与服务端金额校验（P0-3）

- 版本：v1（设计稿，**不含代码改动**）
- 基线：`docs/review-logic.md`（问题定位）+ `docs/review-technical.md`（架构路线）之后的落地设计
- 基线代码：`app/routes.py` 805 行、`app/models.py` 245 行、`app/__init__.py` 78 行（文中行号为**改造前基线行号**，落地后以函数名定位）
- 约束：本文只描述方案；测试基线跑完、Chief 通知后再开工。所有涉及表结构的变化都必须走迁移脚本，**禁止再用 `db.create_all()` 改生产库**。
- **口径变更（2026-10-23 已定并生效）**：首付款与其他节点**统一按「原金额+增补金额」计算**，节点合计=合同总额（尾差归集到质保金优先）。本文中凡出现「首付按原金额」「base：首付=原金额」「增补是否参与首付待确认」等旧描述，**一律以现行口径为准**；对应 V9/P1-1 的待确认项关闭，不再按旧口径实现或验收。

---

## 0. 范围、目标与不变量

### 0.1 本文覆盖

| 编号 | 问题 | 本文覆盖 |
| --- | --- | --- |
| P0-1 | 合同「修改/二次确认」bulk delete 付款节点 → 有子记录 500 / 无子记录静默清空金额与状态 | 第 1 节（就地更新 + 不变量 + 历史保留 + 禁止条件） |
| P0-2 | 确认页回填识别快照而非项目现值 → 一提交覆盖 | 第 1 节 1.7~1.8（现值回填 + 差异回显 + 二次确认） |
| P0-3 | 服务端零校验（比例合计、正负、NaN/Inf、尾差） | 第 2 节（规则 + 函数 + 回归用例） |
| 关联 | P0-4 审计留痕（与 P0-1 共用一张表）、P1-3 日期联动、P1-6 舍入、P1-7 并发 | 文中以「依赖/顺带」标注，不在本文展开全量 |

### 0.2 目标（可验收）

1. 合同回改**永不删除付款节点**：节点 `id` 稳定，`payment_applications` / `payment_records` 明细不丢；
2. 已发生（有申请/付款/开票/触发）的节点，口径变更后**应付金额锁定不变**，差额单独记账，可追溯、可解释；
3. 修改内容在提交前**逐字段回显「现值 → 新值 → 差额」**，用户显式确认后才写库；
4. 服务端对金额/比例的校验与前端解耦，直接 POST 也无法写入错误金额；
5. 并发双击/双人修改不产生重复项目、重复节点、覆盖丢失；
6. 任何口径变更都有 `contract_revisions` + `audit_logs` 证据，且与业务事实（申请/付款）分离。

### 0.3 三条不变量（写入代码注释与测试）

- **I1 交易数据只增不改**：`payment_applications`、`payment_records`、`acceptance_records` 只允许新增；更正走「冲销/变更单」，不允许直接 UPDATE/DELETE。
- **I2 口径与事实分离**：`baseline_amount`（当前口径计算值）可被口径变更改写；`due_amount`（系统应付）对已发生节点锁定；`invoice_amount/paid_amount/status` 只能由明细动作或事务内重算写入，口径变更不得触碰。
- **I3 派生字段单一来源**：`projects.warranty_due_date`、节点 `trigger_date`/`due_date` 统一由 `sync_project_dates()` 幂等推导，其他路径不得直写。

---

## 1. P0-1 / P0-2：口径变更与交易数据分离

### 1.1 字段可更新矩阵（明确「哪些字段允许直接更新」）

| 对象 | 允许直接更新 | 条件 | 不可变 / 只由流程写 |
| --- | --- | --- | --- |
| `Project` | `name`、`remark`、`payment_method`（新增）、`status`、`trial_date`、`acceptance_date` | `status` 走状态机且仅 admin；日期变更受 1.6 约束并写审计 | `id`、`project_no`、`created_by/at`、合同绑定（只经合同确认流程） |
| `ProjectItem` | `item_name`、`tax_rate`、`original_amount`、`additional_amount` | 仅当项目「未发生」或差额可归集（1.5）；写审计 | `id`、`project_id` |
| `PaymentNode` | `ratio`、`node_name`、`calc_base`、`baseline_amount` | 已发生节点走「锁定 + 差额」；未发生节点直接改 | `id`、`project_id`、`item_id`、`node_type`；`invoice_amount`、`paid_amount`、`status`、`trigger_date`、`due_date` 只能由明细/派生写入 |
| `ContractFile` | `status`、`contract_name`、`project_id`（仅首次确认）、`confirmed_values_json`、`confirmed_at/by`、`revision_no` | 流程内部写 | `result_json`（识别快照，永久只读）、`raw_text`、`file_name` |
| `PaymentApplication` / `PaymentRecord` / `AcceptanceRecord` | 无 | — | 全部只增不改（I1） |

**口径变更只允许改「基线」相关字段**，不碰交易字段；这是整个方案的核心边界。

### 1.2 数据模型新增（P0-1 需要）

#### 1.2.1 `payment_nodes` 新增列

| 列 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `baseline_amount` | NUMERIC(14,2) | 回填 `due_amount` | 按当前口径（base×ratio）计算出的应付基线 |
| `adjustment_amount` | NUMERIC(14,2) | 0 | 已发生节点因口径变更而锁定的差额（可正可负） |
| `version` | INTEGER | 0 | 乐观锁版本号，每次变更 +1 |
| `updated_at` | DATETIME | NULL | UTC |
| `updated_by` | INTEGER FK `users.id` | NULL | 最后一次修改人 |

**金额恒等式（写入模型属性、校验与报表必须遵守）**

```
due_amount = baseline_amount + adjustment_amount
```

- 未发生节点：`adjustment_amount = 0`，`due_amount = baseline_amount = calc(base, ratio)`；
- 已发生节点：口径变更时只改 `baseline_amount`，同时把差额写进 `adjustment_amount`，保持 `due_amount` 不变（锁账）；
- 后续「变更单/冲销」审批通过时，才把 `adjustment_amount` 冲减并更新 `due_amount`（二期，本文只留字段与约束）。

#### 1.2.2 `projects` 新增列

| 列 | 类型 | 默认 | 用途 |
| --- | --- | --- | --- |
| `payment_method` | VARCHAR(256) | NULL | 结构化保存付款方式，供确认页回填（当前只存在 `remark` 文本里） |
| `version` | INTEGER | 0 | 乐观锁（P1-7，顺带） |
| `updated_at` / `updated_by` | DATETIME / INTEGER | NULL | 审计 |
| `deleted_at` / `deleted_by` | DATETIME / INTEGER | NULL | 软删除（删除守卫，1.6.3） |

#### 1.2.3 `project_items` 新增列

`version INTEGER DEFAULT 0`、`updated_at DATETIME NULL`、`updated_by INTEGER NULL`（与节点同步变更，便于并发检测）。

#### 1.2.4 `contract_files` 新增列

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `confirmed_values_json` | TEXT | 最近一次**实际应用**的口径快照（JSON：原金额、增补、税率、付款方式、比例、各节点应付、revision_no）。用于项目被删/合同解绑时的回显兜底与审计对账 |
| `confirmed_at` / `confirmed_by` | DATETIME / INTEGER | 最近一次确认/回改时间与人 |
| `revision_no` | INTEGER DEFAULT 0 | 当前版本号（与 `contract_revisions.revision_no` 对应） |

`result_json` 语义固定为「**识别快照**」，确认/回改**永不回写**；这样「识别值」与「现值」在页面上可以同时展示（P0-2）。

#### 1.2.5 新表 `contract_revisions`（合同口径版本与二次确认凭据）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PK | |
| `contract_id` | INTEGER FK `contract_files.id` NOT NULL | |
| `project_id` | INTEGER FK `projects.id` NULL | 确认前可为空 |
| `revision_no` | INTEGER NOT NULL | 合同内递增，唯一约束 `(contract_id, revision_no)` |
| `action` | VARCHAR(16) NOT NULL | `confirm` / `modify` / `reimport` |
| `submitted_json` | TEXT NOT NULL | 规范化后的提交值（Decimal 字符串、比例百分比字符串） |
| `diff_json` | TEXT NULL | 与现值的逐字段差异（见 1.7） |
| `diff_hash` | VARCHAR(64) NULL | `sha256(canonical(submitted_json) + project.version + node versions)`，二次确认凭据 |
| `status` | VARCHAR(16) NOT NULL | `pending` / `applied` / `blocked` / `rejected` |
| `blocked_reason` | VARCHAR(512) NULL | 已发生节点导致的阻断原因 |
| `operator_id` / `operator_name` | INTEGER / VARCHAR(64) | 快照 |
| `created_at` / `applied_at` | DATETIME | UTC |

#### 1.2.6 新表 `audit_logs`（与 P0-4 共用，一次建表）

字段采用 `docs/review-technical.md` §10.2 设计：`id, occurred_at, actor_id, actor_name, actor_role, action, entity_type, entity_id, project_id, before_json, after_json, ip, user_agent, request_id, result, error_msg`；索引 `occurred_at`、`(actor_id, occurred_at)`、`(project_id, occurred_at)`。P0-1 先埋：合同确认/回改、节点口径变更、项目/科目变更、删除；其余埋点随 P0-4 补齐。

> 不新增 `payment_node_changes` 表：节点级 before/after 由 `audit_logs` 承载，节点「历史」由 `contract_revisions`（口径版本）+ 只增明细（交易事实）+ 锁定的 `due_amount` 三者共同构成，避免表数量膨胀。

### 1.3 「已发生」判定（新增函数，唯一口径）

新增于 `app/routes.py` 工具区（或后续 `services/nodes.py`）：

```python
def node_has_occurred(node) -> bool:
    """节点是否已发生业务，口径变更时应付锁定"""
    return (
        node.status != '未触发'
        or float(node.invoice_amount or 0) > 0
        or float(node.paid_amount or 0) > 0
        or len(node.applications) > 0
        or node.trigger_date is not None
        or node.due_date is not None          # 验收已发生 → 质保金应付日已生成
    )

def project_occurred_blockers(project) -> list[dict]:
    """返回阻断项：[{node_type, node_name, reason}]"""
    reasons = []
    for n in project.nodes:
        if node_has_occurred(n):
            reasons.append({'node_id': n.id, 'node_type': n.node_type,
                            'node_name': n.node_name,
                            'reason': '已有申请/付款/开票/触发日期，应付金额已锁定'})
    if project.acceptance_date or project.trial_date:
        reasons.append({'node_id': None, 'node_type': None,
                        'node_name': '工程节点', 'reason': '已登记试运行/验收日期'})
    return reasons
```

判定采用「就宽不就窄」：只要可能已有业务痕迹，就按已发生处理，宁可让用户走变更单，也不能静默改账。

### 1.4 禁止删除 / 禁止重建的条件

| 操作 | 禁止条件 | 结果 |
| --- | --- | --- |
| 合同回改删除/重建节点 | **任何情况下都不允许再删除节点**（P0-1 根因） | 改造后代码中不得出现 `PaymentNode.query...delete()` 于确认流程 |
| 已发生节点改 `due_amount` | `node_has_occurred(node) == True` | 拒绝直接改，只允许 `baseline + adjustment` 锁账 |
| 修改已发生节点的 `status/invoice/paid` | 任何口径变更路径 | 一律拒绝（I1/I2） |
| 改项目「已发生」状态下的科目金额 | `project_occurred_blockers(project)` 非空 | 不允许无差额地直接改，走差额归集（1.5） |
| 物理删除项目 | 存在任一 `payment_application` / `payment_record` / 节点 `invoice_amount>0` / `paid_amount>0` / `acceptance_records` / `trial_date` / `acceptance_date` / 该项目绑定的合同 | 只允许软删除（`deleted_at`），且管理员二次确认 + 审计 |
| 物理删除用户 | 存在 `projects.created_by` / `applicant_id` / `operator_id` / `contract_files.created_by` 引用 | 改为「停用」（`users.is_active=false`，P1-5，依赖 P0 之外的一次迁移；本文先禁止 500，返回业务提示） |
| 删除合同文件 | 已绑定项目 | 禁止（只允许重新识别） |

补充：**空壳项目**（无科目、无节点业务、无验收/试运行、未绑合同）才允许物理删除；其余走软删除，列表默认过滤 `deleted_at IS NULL`。

### 1.5 就地更新算法（替换 `routes.py:589-621`）

#### 1.5.1 函数划分（按职责拆小，便于测试）

```python
# app/validation.py（新增）
@dataclass(frozen=True)
class ContractValues:
    project_name: str
    project_no: str | None
    original_amount: Decimal      # 元，2 位
    additional_amount: Decimal    # 元，2 位
    tax_rate: str | None
    payment_method: str | None
    ratios: dict[str, Decimal]    # 百分比，如 Decimal('30.00')，键为 4 类节点
    node_amounts: dict[str, Decimal]  # 已含尾差归集
    raw: dict                     # 原始表单值，审计用

def parse_contract_form(form) -> tuple[ContractValues | None, list[str]]  # 规则见第 2 节
```

```python
# app/routes.py
def _project_contract_values(project) -> ContractValues | None: ...
    # 从 Project + 唯一「合同价款」科目 + 4 节点读取现值（并发字段：project.version / node.version）

def _contract_snapshot_for_form(contract) -> dict: ...
    # 识别快照（result_json）仅供页面「识别值」提示，不参与回填

def _diff_contract_values(current, submitted) -> list[dict]: ...
    # 返回 [{field, label, old, new, delta, unit}]，金额按 Decimal 差，比例按百分点

def _diff_hash(diff, project_version, node_versions) -> str: ...
    # sha256(canonical_json(sorted(diff)) + "|" + project_version + "|" + 各节点 version)

def _apply_contract_values(project, values, *, operator, revision) -> list[dict]: ...
    # 事务内就地更新，返回审计变更列表；不 commit

def _contract_revision(contract, project, values, diff, action, status, **kw): ...
    # 写 contract_revisions 一行（pending/applied/blocked）
```

#### 1.5.2 应用步骤（单事务，失败整体回滚）

1. 读取现值和 `project.version`、各节点 `version`；
2. 计算 `diff`（1.7）；无差异 → 幂等成功（302，不写库、不新增 revision 也可，记 no-op 审计可选）；
3. `project_occurred_blockers(project)` 非空 → 整单 `blocked`：写 `contract_revisions(status='blocked', blocked_reason=...)` + 审计，返回 422 页面（不写业务数据）；
4. 更新项目：`name`、`remark`、`payment_method`、`version += 1`；
5. 更新唯一科目：`original_amount`、`additional_amount`、`tax_rate`（若存在多科目，合同项目只应有 1 个「合同价款」，多科目 → 拒绝并提示走项目编辑）；
6. 逐个节点（按 `item_id + node_type` 匹配，**绝不删除/新建**）：
   - `ratio`、`node_name`、`calc_base`、`baseline_amount = calc(base, ratio)`；
   - 未发生：`adjustment_amount = 0`，`due_amount = baseline_amount`；
   - 已发生：`adjustment_amount = old_due_amount - baseline_amount`，`due_amount = old_due_amount`（锁账）；
   - `version += 1`、`updated_at/updated_by`；
7. `sync_project_dates(project, collect=True)`（1.9），把派生日期变更并入 diff 审计；
8. 写 `contract_revisions(status='applied', diff_json, diff_hash, applied_at)`、`contract_files.confirmed_values_json/confirmed_at/by/revision_no += 1`、逐条 `audit_logs`；
9. 单次 `db.session.commit()`；异常 → rollback + 统一 409/422/500 中文提示（不再裸露 FK 500）。

#### 1.5.3 已发生节点锁账的金额示例（供实现与测试对照）

| 场景 | 变更前 | 变更后 | 说明 |
| --- | --- | --- | --- |
| 进度款节点已付 50,000，合同增补从 50,000 改 0 | `baseline=111800, adjustment=0, due=111800` | `baseline=85800, adjustment=+26000, due=111800` | 已发生锁账，差额 26,000 可见可审 |
| 同节点改回原值 | `baseline=111800, adjustment=0, due=111800` | 同上反向，`adjustment=0` | 幂等、可逆 |
| 未发生验收款节点比例 15%→10% | `baseline=32250, adjustment=0, due=32250` | `baseline=21500, adjustment=0, due=21500` | 未发生直接改，无差额 |
| 已发生节点差额冲销（二期） | `adjustment=+26000` | 生成冲销单后再调整 `due_amount` | 本期只留字段与提示，不做 |

### 1.6 已发生节点的历史保留方案

1. **节点不删除、不改 id**：口径变更只改基线，节点与明细分的关系不断链；
2. **明细即历史**：`payment_applications`、`payment_records`、`acceptance_records` 只增不改（I1），金额/日期/凭证/操作人永久保留；
3. **应付锁账即历史**：已发生节点的 `due_amount` 不再变化，历史应付款不会因为事后改合同而被"改账"；
4. **差额单列即解释**：`adjustment_amount` 说明「按新口径应付 X，历史上锁定差额 Y」；
5. **版本快照即证据**：每次提交（含被阻断的）写 `contract_revisions`（`submitted_json/diff_json/diff_hash/operator`），被阻断也留痕；
6. **审计即时间线**：`audit_logs` 按项目/节点记录 before/after、动作、人、IP，可查询导出（P0-4 页面）。

### 1.7 修改前后差异回显与二次确认（P0-2 的交互与安全）

#### 1.7.1 三段式提交（防误改、防重放、防并发）

```
GET  /contracts/<id>/confirm
      → 以项目现值回填；识别值仅作「识别值：…」提示（1.8）

POST /contracts/<id>/confirm            # 第一段：预览
      → 解析+校验（第 2 节）
      → 计算 diff
      → diff 为空：直接幂等应用（302，不写 revision 或写 no-op 审计）
      → diff 非空：不写业务数据，渲染差异确认页（422/200）：
          表格：字段 | 现值 | 提交值 | 差额
          阻断项：已发生节点列表 + 原因 + 建议（变更单/冲销）
          隐藏字段：project_id、project_version、diff_hash、revision_no
          按钮：确认提交 / 返回修改

POST /contracts/<id>/confirm            # 第二段：确认
      带 diff_hash（+ 版本号）
      → 校验 diff_hash 与当前重算结果一致（防篡改）
      → 校验 project.version / node.version 未变（防并发覆盖）
      → 有阻断项：写 blocked revision + 审计，返回 422（不写业务数据）
      → 无阻断：按 1.5.2 应用，单事务提交
      → 版本冲突：409「数据已被他人修改，请刷新后重试」
```

#### 1.7.2 差异显示规则

| 字段 | 显示 | 示例 |
| --- | --- | --- |
| 原金额/增补金额 | 千分位 2 位小数 + 差额 | `165,000.00 → 180,000.00（+15,000.00）` |
| 比例 | 百分点 2 位 + 差额 | `首付款 30.00% → 25.00%（-5.00pp）` |
| 各节点应付 | 现值/新值/差额；已发生节点标注「锁定」 | `进度款 111,800.00 → 111,800.00（锁定，差额 +26,000.00 计入调整）` |
| 项目名称/税率/付款方式 | 旧 → 新 | — |
| 工程日期联动 | 单独分组提示 | `质保到期 2029-03-09 → 2029-03-09（未变）` |

#### 1.7.3 安全点

- `diff_hash` 由服务端根据 DB 现值 + 版本号重算，客户端只回传，不可构造（避免"确认了 A 却提交 B"）；
- 第二段提交必须**重新校验**阻断项与版本，不能只信第一段结果；
- 同一 `contract_id + revision_no` 唯一约束 + 事务，防重复应用（并发双击 → 一个成功、另一个 409/幂等）；
- 已发生的阻断项无论是否带 `diff_hash` 都拒绝（除二期变更单流程）。

### 1.8 确认页回填项目现值而不是识别快照（P0-2 落地）

#### 1.8.1 后端

- `contract_confirm`（`app/routes.py:543-554`）改造：

```python
is_update = bool(contract.project_id) and db.session.get(Project, contract.project_id) is not None
current = _project_contract_values(project) if is_update else None
recognized = _parse_contract_result(contract) or {}
return render_template('contract_confirm.html',
                       contract=contract, result=recognized,
                       values=current, is_update=is_update,
                       node_order=CONTRACT_NODE_ORDER)
```

- 修复 `is_update` 判定：不再用 `contract.status == 'confirmed'`（`app/routes.py:564`），改为「`project_id` 存在且项目存在」。这样合同重识别失败（`status=failed`）也不会误走新建、产生重复项目（顺带修 P2-4）。
- `contract_confirm_submit` 使用同一个 `_project_contract_values()` 作为比较基线。

#### 1.8.2 模板（`app/templates/contract_confirm.html`）

| 行号（基线） | 现状 | 改为 |
| --- | --- | --- |
| `:39` | 项目编号空值 | 更新态只读展示项目编号（隐藏 `project_id/project_version`） |
| `:43` | `contract.contract_name or 文件名` | `values.project_name`（现值） |
| `:49` | `result.contract_amount` | `values.original_amount`，下方灰字「识别值：…（仅参考）」 |
| `:58` | 硬编码 `value="0.00"` | `values.additional_amount` |
| `:65-70` | 识别税率 | `values.tax_rate` 选中 |
| `:79` | `result.payment_method` | `values.payment_method` |
| `:103-113` | 识别比例 | `values.ratios[nt]`（保留 `result` 出处提示） |
| `:129-137` | 合计 | 服务端回显合计与「比例合计」校验结果 |
| `:159-181` | JS 实时联动 | 保留（纯展示），但增加一行「以提交后服务端校验为准」；金额显示用 `toFixed(2)` |

- 增加差异确认区块（仅第二段 POST 渲染）：`diff` 表格 + 阻断项 + `diff_hash` 隐藏字段 + 「确认提交」。
- 已发生阻断时：显示原因列表与「联系管理员走变更单」，提交按钮禁用（服务端仍二次校验）。

#### 1.8.3 快照与现值的关系

- `result_json` = 识别快照（只读，出处提示用）；
- `confirmed_values_json` = 最近一次实际应用值（审计与项目缺失时兜底）；
- 页面回填一律以 `Project/ProjectItem/PaymentNode` 为真相源，避免"快照覆盖现值"。

### 1.9 `sync_project_dates` 改造（补 P1-3 + 支撑 P0-1 原子性）

现状：`app/routes.py:69-83`，只在日期「有值」时联动、内部 `commit()`、清空不回退。

改造后签名与规则：

```python
WARRANTY_DAYS = 365 * 3   # 收敛到 app/__init__.py 常量（或 config），替换 routes.py:76,82 字面量

def sync_project_dates(project, *, collect=False) -> list[dict]:
    """幂等：只改派生字段，不 commit；collect=True 时返回变更明细供审计/差异回显"""
```

| 输入状态 | 派生动作（仅未发生节点） | 已发生节点 |
| --- | --- | --- |
| `trial_date` 有值 | 进度款 `trigger_date = trial_date` | 保留原 `trigger_date`，差异记 audit（不静默改） |
| `trial_date` 清空 | 进度款 `trigger_date = None` | 有验收/付款痕迹时禁止清空（校验层拦截） |
| `acceptance_date` 有值 | 验收款 `trigger = acc`；质保金 `due_date = acc + 1095d`；`project.warranty_due_date = acc + 1095d` | 保留原值，差异记 audit |
| `acceptance_date` 清空 | 上述三处派生值清空 | 存在 `AcceptanceRecord` 时禁止清空（校验层拦截） |
| 无日期 | 不动 | 不动 |

配套改动：

- 删除 `app/routes.py:83` 的内部 `commit()`；调用方（`project_edit:233-234`、`item_add:263-264`、`acceptance_add:397-398`、`trial_set:415-416`、合同更新路径）统一在路由层**一次提交**；
- 合同更新路径必须调用（现在完全没调，`app/routes.py:589-621`），否则重建后节点日期为 NULL（实测 T5d）；
- 幂等性：重复调用结果一致（测试覆盖）；
- 日期校验放 `app/validation.py`：验收 ≥ 试运行、日期范围 1900-01-01 ~ 2100-12-31、清空验收/试运行需无对应记录（1.4）。

### 1.10 并发、幂等与错误处理（P0-1 的必要配套）

| 场景 | 方案 |
| --- | --- |
| 双击确认/重复提交 | `contract_revisions(contract_id, revision_no)` 唯一约束；`contract_files.revision_no` 条件更新；第二段 `diff_hash` + `revision_no` 校验 → 重复请求幂等返回既有结果 |
| 两人同时改同一合同 | 隐藏 `project_version` + 节点 `version`；写库时 `UPDATE ... WHERE version=:old`（MSSQL 加 `WITH (UPDLOCK, ROWLOCK)`），rowcount=0 → 409 中文提示、不写半截 |
| 并发建项目编号 | 顺带修 `_next_contract_project_no`（`app/routes.py:453-460`）：唯一冲突重试 3 次或改号段表（P1-7，可与本改造同批） |
| FK/唯一冲突 | 统一 `@app.errorhandler(IntegrityError/409/422/413/500)`，返回中文页面；不得再出现裸 500 |
| 事务边界 | 路由函数内一次 commit；`sync_project_dates` 不提交；审计与业务同事务写入（审计失败 → 整体回滚，保证"有业务必有审计"） |

### 1.11 P0-1/P0-2 涉及文件与行号清单

| 文件 | 位置（基线） | 改动 |
| --- | --- | --- |
| `app/models.py` | `PaymentNode` 101-141、`recalc` 120-127 | 新增 5 列 + `due = baseline + adjustment`；`recalc` 改用 `money.calc_due` 写 `baseline_amount`；新增 `has_occurred()` 属性/方法 |
| `app/models.py` | `Project` 33-72、`ProjectItem` 75-98、`ContractFile` 197-224 | 新增版本/审计/软删/合同快照列 |
| `app/models.py` | 新增类 | `ContractRevision`、`AuditLog` 模型 + 索引/唯一约束 |
| `app/routes.py` | `sync_project_dates` 69-83 | 按 1.9 重写（幂等、不 commit、可收集变更） |
| `app/routes.py` | `contract_confirm` 543-554 | 现值回填 + `is_update` 修正 + 识别值单独传递 |
| `app/routes.py` | `contract_confirm_submit` 557-654 | 拆分三段落：预览/确认/应用；删除 589-621 的删表重建逻辑；补校验、锁账、审计、revision |
| `app/routes.py` | 新增工具函数 | `_project_contract_values`、`_diff_contract_values`、`_diff_hash`、`_apply_contract_values`、`_contract_revision`、`project_occurred_blockers`、`node_has_occurred` |
| `app/routes.py` | `project_delete` 269-291 | 物理删除加守卫，改软删除；移除手写级联（依赖 P2-3 统一处理） |
| `app/routes.py` | `project_edit` 218-237、`acceptance_add` 377-400、`trial_set` 403-418 | 日期校验 + 一次提交 +（operator 状态越权另见 P1-4，可同批收口） |
| `app/routes.py` | `contract_rerun` 662-693 | `is_update` 一致性使用；失败不改 `confirmed` 状态（P2-4） |
| `app/templates/contract_confirm.html` | `:39/:43/:49/:58/:65-70/:79/:103-113/:129-137` | 现值回填 + 识别值提示 + 差异确认区块 |
| `app/templates/project_detail.html` | `:120/:140` | 展示实际比例/锁定/调整说明（P2-1，顺带） |
| `app/__init__.py` | 9-38、41 | 新增 `WARRANTY_DAYS` 常量；`NODE_SPEC` 收敛（可选） |
| 新增文件 | `app/money.py`、`app/validation.py`、`app/audit.py` | 见第 2 节与 P0-4 |

---

## 2. P0-3：服务端校验规则与实现

### 2.1 新增 `app/money.py`（唯一金额入口）

| 函数 | 签名 | 职责 |
| --- | --- | --- |
| `to_decimal` | `to_decimal(raw, *, field, allow_blank=False, allow_negative=False) -> Decimal` | 规范化输入：去空格/全角/`￥¥`/千分位；只接受 `^[+-]?\d{1,12}(\.\d{1,2})?$`；**显式拒绝** `nan/inf/1e309/E 记法/多小数位/多个小数点/多个负号**；量化 `ROUND_HALF_UP` 到 0.01 |
| `parse_money` | `parse_money(raw, *, field, min_value=0, max_value=999999999999.99) -> Decimal` | 金额字段（元） |
| `parse_percent` | `parse_percent(raw, *, field, min_value=0, max_value=100) -> Decimal` | 比例字段（百分数，2 位小数） |
| `calc_due` | `calc_due(base: Decimal, ratio_pct: Decimal) -> Decimal` | `(base * ratio_pct / 100).quantize(0.01, ROUND_HALF_UP)` |
| `expected_total` | `expected_total(original, additional, ratios, calc_bases) -> Decimal` | 按口径基数求精确目标合计（未舍入前） |
| `allocate_tail` | `allocate_tail(amounts: dict[str, Decimal], target: Decimal) -> dict[str, Decimal]` | 尾差归集（2.3） |
| `format_money` | `format_money(v) -> str` | 千分位 2 位（替换 `routes.py:58-62` 的 float 版） |

**基准常量**：金额上限 `999999999999.99`（`NUMERIC(14,2)`）、比例 2 位小数、尾差容差 `0.01`。

### 2.2 校验规则清单（服务端强制，前端仅提示）

| # | 规则 | 失败处理 |
| --- | --- | --- |
| V1 | 原金额必填且 `0 < 原金额 ≤ 999999999999.99` | 422 + 字段错误 |
| V2 | 增补金额可空（视为 0），`0 ≤ 增补金额`；合计 ≤ 上限 | 422 |
| V3 | 每个比例必填，`0 ≤ 比例 ≤ 100`，最多 2 位小数；不允许负/超 100 | 422（逐字段） |
| V4 | 比例合计 `|Σ - 100| ≤ 0.01`（容差 0.01 个百分点） | 422，回显实际合计 |
| V5 | 4 类节点必须齐全且不重复；重复提交同名参数（`getlist` 长度 >1）→ 拒绝（防参数走私） | 422 |
| V6 | 金额/比例输入拒绝 `nan`、`inf`、`1e309`、`E` 记法、非法字符、多个小数点、空字符串（比例） | 422，提示「请输入有效数字（最多两位小数）」 |
| V7 | 节点应付 `due ≥ 0`（尾差归集后仍为负 → 拒绝） | 422 |
| V8 | 勾稽：`Σdue` 必须等于 `expected_total`（按各节点 calc_base 的精确目标，量化到分）；尾差按 2.3 归集，归集后必须相等 | 422 |
| V9 | 口径勾稽（现行口径下应为阻断）：`Σdue` 必须等于「原金额+增补金额」按比例计算的精确目标；2026-10-23 起增补全额参与首付款，故不再存在 `(1-首付比例)×增补` 的差额豁免；历史数据/识别草稿出现该差额时提示并按 V8 处理 | 422（口径已统一，默认阻断；如需过渡保留配置开关 `CONTRACT_TOTAL_STRICT`） |
| V10 | 金额尾差绝对值和 ≤ `0.01 × 节点数` 的合理性检查（防异常比例导致大额"尾差"） | 422 |
| V11 | 日期：`acceptance ≥ trial`、范围 1900-01-01 ~ 2100-12-31、格式 `%Y-%m-%d` | 422 |
| V12 | 文本：项目名 ≤ 256、税率 ∈ `{'13%','9%','6%','3%','0%',''}`、付款方式 ≤ 256（P2-11 顺带） | 422 |
| V13 | 节点操作：`apply_amount > 0`、`paid_amount > 0`、`累计已付 + 本次 ≤ due_amount`、`invoice_amount ≤ due_amount`、状态合法（P1-2 状态机的前置校验） | 422 |
| V14 | 所有金额服务端重新计算，**不信任前端 `recompute()` 结果** | — |

### 2.3 尾差归集算法（明确"归集到哪个节点"）

```
target(精确) = Σ_i  base_i × ratio_i / 100        （Decimal，未量化）
rounded_i    = quantize(target_i, 0.01, HALF_UP)  （逐节点）
tail         = quantize(target, 0.01, HALF_UP) - Σ rounded_i
若 tail == 0 → 结束
归集顺序：warranty → acceptance → progress → first 中第一个 ratio > 0 的节点；
  node.due += tail；若节点修改后 due < 0 → 回退尝试下一个；
  全部尝试后仍无法归集（tail 剩余非 0）→ 拒绝（V8）
```

- 默认尾差归集到**质保金**（最后付、金额最小，业务最易接受）；质保金比例 0 → 归到验收款，依次往前；
- 归集结果写入 `baseline_amount`，并记入 `contract_revisions.diff_json`（`tail_adjust: {node_type, amount}`）；
- 业务若确认"尾差单列"口径，只需改 `allocate_tail` 的归集目标（配置 `TAIL_TARGET='warranty'|'separate'`），其余不变。

**示例（可直接做测试期望）**：原金额 100.30、增补 0、比例 30/52/15/3：

| 节点 | 精确值 | 逐节点舍入 | 归集后 |
| --- | --- | --- | --- |
| 首付款 30% | 30.0900 | 30.09 | 30.09 |
| 进度款 52% | 52.1560 | 52.16 | 52.16 |
| 验收款 15% | 15.0450 | 15.05 | 15.05 |
| 质保金 3% | 3.0090 | 3.01 | 3.00（尾差 -0.01） |
| 合计 | 100.3000 | 100.31 | **100.30** ✅ |

**增补口径示例（P1-1 沿用现状）**：原金额 165,000、增补 50,000、比例 30/52/15/3：`target = 49,500 + 111,800 + 32,250 + 6,450 = 200,000`，合同总额 215,000，差异 15,000 = 30%×增补 —— 按 V9 默认只提示，等业务确认后再决定是否阻断。

### 2.4 函数与行号改动范围

| 文件/函数 | 基线位置 | 改动 |
| --- | --- | --- |
| `routes._to_num` | `app/routes.py:792-796` | 删除或改薄为 `parse_money` 包装；所有调用点（`168-169,253-254,303,329,345,569-570,586`）替换 |
| `routes.fmt_money` | `app/routes.py:58-62` | 改 `money.format_money`（Decimal） |
| `routes._to_date` | `app/routes.py:799-805` | 加范围/格式校验，返回错误而非 None 静默 |
| `routes.project_new` | `app/routes.py:140-192` | 科目金额解析/校验（多个 `getlist` 对齐校验），节点 `calc_due` |
| `routes.item_add` | `app/routes.py:240-266` | 同上 |
| `routes.node_update` | `app/routes.py:294-374` | V13：申请/发票/付款金额、上限、状态前置校验 |
| `routes.contract_confirm_submit` | `app/routes.py:557-654` | 全量 V1-V12 + 2.3 尾差 + 不信任前端 |
| `routes._pct_str` / `_node_name` | `app/routes.py:473-475,657-659` | 比例格式化统一 Decimal（`%` 2 位或去尾零需统一） |
| `models.PaymentNode.recalc` | `app/models.py:120-127` | `money.calc_due`；写 `baseline_amount`（口径变更路径由 `_apply_contract_values` 调用） |
| `models.ProjectItem.total_amount` / `Project.total_*` | `app/models.py:51-58,87-89` | Decimal 求和，不再混 float |
| `models.to_dict` | `app/models.py:91-98,129-141` | JSON 输出用字符串（`"123.45"`）或保留 float 但注明仅供展示；导出/接口用字符串 |
| `contract_engine` 识别结果 | `app/contract_engine.py:410-460,463-499,502-516` | 加 `validate_recognized_result()`：比例缺失已有警告；新增「合计≠100 / 比例越界 / 首付基数」警告；不阻断（识别是草稿，确认页服务端校验为准） |
| 模板 JS | `app/templates/contract_confirm.html:159-181` | 仅展示；增加 2 位小数格式化与「服务端为准」提示 |

### 2.5 P0-3 回归用例清单（输入 → 期望）

> 统一通过 Flask `test_client` 直接 POST `/contracts/<id>/confirm`（不依赖 JS），断言 HTTP 码、DB 中的 `baseline_amount/due_amount`、`contract_revisions`、`audit_logs`。

| # | 输入（原金额 / 增补 / 比例 | 期望 |
| --- | --- | --- |
| M1 | `165000 / 0 / 30,52,15,3` | 201/302 应用；各节点 `49,500 / 85,800 / 24,750 / 4,950`；合计 = 165,000 |
| M2 | `165000 / 50000 / 30,52,15,3` | 应用；`49,500 / 111,800 / 32,250 / 6,450`；合计 200,000；响应含 V9 口径提示；审计记录 |
| M3 | `165000 / 0 / 30,52,15,3.01` | 拒绝；提示合计 100.01 超容差；DB 无变更 |
| M4 | `165000 / 0 / 30,52,15,2.99` | 拒绝；合计 99.99 超容差 |
| M5 | `165000 / 0 / 30,52,45,-27` | 拒绝（负比例，V3） |
| M6 | `165000 / 0 / 150,0,0,0` | 拒绝（比例 >100，V3） |
| M7 | `0 / 0 / 30,52,15,3` | 拒绝（原金额必须 > 0，V1） |
| M8 | `-1 / 0 / ...` | 拒绝（负金额，V1） |
| M9 | `100.30 / 0 / 30,52,15,3` | 应用；`30.09 / 52.16 / 15.05 / 3.00`；尾差 -0.01 归质保金（2.3 示例） |
| M10 | `100.30 / 0 / 30,52,18,0` | 应用；尾差归集到验收款；质保金 0.00；合计 100.30 |
| M11 | 比例填 `nan` / `inf` / `1e309` / `NaN` | 全部拒绝（V6），提示格式错误 |
| M12 | 金额填 `nan` / `inf` / `1e309` / `12e5` | 全部拒绝（V6） |
| M13 | 金额填 `165,000.00` / `￥165000.00` / 全角 `１６５０００` | 规范化后接受（V6/`to_decimal`） |
| M14 | 金额填 `1,2,3` / `1.2.3` / `--5` | 拒绝（V6） |
| M15 | 金额填 `999999999999.99` | 接受（上限）；`1000000000000.00` 拒绝 |
| M16 | 增补 `-50000` | 拒绝（V2，待业务确认是否允许调减；开关控制） |
| M17 | 金额/比例重复提交同名参数（如两个 `ratio_first`） | 拒绝（V5） |
| M18 | 比例 `0/0/0/0` | 拒绝（V4 合计 0） |
| M19 | 首付款基数：`165000 / 50000 / 30,52,15,3` | 统一口径：首付 = (165,000+50,000)×30% = **64,500**（旧口径 49,500 已作废；V8/口径按原+增补） |
| M20 | 合同金额与节点合计勾稽：`165000 / 0 / 30,52,15,3` | `Σdue == 165,000.00`（V8） |
| M21 | 直接 POST 跳过 JS（无脚本） | 与浏览器提交结果一致（V14） |
| M22 | 草稿识别结果合计 ≠100（如 30/50/15/3） | 确认页警告 + 提交被 V4 拒绝；不能"默认放行" |
| M23 | 并发/双击两次相同提交 | 只产生 1 个项目/1 次 revision；第二次幂等或 409（见第 1.10） |
| M24 | 已发生节点项目提交不同口径 | 422，DB 金额不变，`contract_revisions(status='blocked')` 一条 |
| M25 | 未发生项目修改金额/比例 | 节点 id 不变、无新增/删除节点、`baseline/due` 更新、`audit_logs` 有 before/after |

单测（`tests/test_money.py`）另覆盖：`to_decimal` 边界（空、空白、全角、千分位、负数、上限、超 2 位小数）、`calc_due` 的 HALF_UP 边界（`100.30×15%`、`0.005` 类）、`allocate_tail` 的归集顺序与失败分支、`expected_total` 的 calc_base 组合。

### 2.6 DB 层约束（与 2.1/2.2 双保险）

`payment_nodes` 增加：

```sql
CHECK (ratio >= 0 AND ratio <= 1)
CHECK (due_amount >= 0)
CHECK (baseline_amount >= 0)
```

`projects`：`CHECK (acceptance_date IS NULL OR trial_date IS NULL OR acceptance_date >= trial_date)`（按业务确认后启用）。
状态字符串 CHECK 暂不加（状态机 P1-2 会扩展"部分付款"等状态，避免连续迁移）；状态合法性在应用层校验。

---

## 3. 数据库结构变更与迁移脚本

### 3.1 变更清单（哪些改动动表）

| # | 对象 | 变更 | 是否数据迁移 | 关联 |
| --- | --- | --- | --- | --- |
| D1 | `payment_nodes` | + `baseline_amount NUMERIC(14,2)`、`adjustment_amount NUMERIC(14,2) NOT NULL DEFAULT 0`、`version INTEGER NOT NULL DEFAULT 0`、`updated_at DATETIME`、`updated_by INTEGER FK` | 回填 `baseline_amount = due_amount` | P0-1 |
| D2 | `projects` | + `payment_method VARCHAR(256)`、`version`、`updated_at/updated_by`、`deleted_at/deleted_by` | 无 | P0-1/P0-2 |
| D3 | `project_items` | + `version`、`updated_at/updated_by` | 无 | P0-1 |
| D4 | `contract_files` | + `confirmed_values_json TEXT`、`confirmed_at`、`confirmed_by`、`revision_no INTEGER DEFAULT 0` | 无（旧合同 `confirmed_values_json` 为空，回显以项目现值兜底） | P0-2 |
| D5 | 新表 `contract_revisions` | 见 1.2.5，含唯一 `(contract_id, revision_no)`、索引 `(project_id)`、`(created_at)` | 无 | P0-1/P0-2 |
| D6 | 新表 `audit_logs` | 见 1.2.6（`docs/review-technical.md` §10.2） | 无 | P0-1/P0-4 |
| D7 | 索引 | `payment_nodes(project_id, node_type)`、`payment_nodes(item_id)`、`payment_applications(node_id)`、`payment_records(node_id)`、`payment_records(application_id)`、`project_items(project_id)`、`acceptance_records(project_id)`、`contract_files(project_id)`、各表 `created_at` | 无 | 顺带 P1 |
| D8 | 约束 | `payment_nodes` 的 3 个 CHECK；日期 CHECK（待确认）；`contract_files.project_id` 唯一性需业务确认（当前允许一项目多合同） | 无 | P0-3 |
| D9 | 用户停用 | `users.is_active BOOLEAN NOT NULL DEFAULT 1` | 回填 1 | P1-5（可与本批同迁移） |

**不动**：`payment_applications`、`payment_records`、`acceptance_records` 结构（只增不改即可满足本方案）；`contract_files.result_json` 语义不变。

### 3.2 迁移工具落地（替换 `db.create_all`）

1. 新增依赖：`alembic>=1.13` 写入 `requirements.txt`；
2. 目录：`alembic.ini`、`migrations/env.py`（`target_metadata = db.metadata`，连接串读 `DATABASE_URL` 环境变量，`render_as_batch=True` 兼容 SQLite）、`migrations/versions/`；
3. 版本规划：
   - `0001_baseline.py`：**当前**生产 schema 的基线（用 autogenerate 生成后人工核对；不含本次新列）；
   - `0002_p0_contract_separation.py`：D1-D8（+ D9 可选）；
   - 后续 P0-4/P0-5/P0-6 各自独立 revision。
4. 对已有库的接入流程（关键，别删库）：
   ```bash
   # 1) 备份（db 文件 + uploads）并校验可恢复
   # 2) 对现有库打基线（不执行建表）
   alembic stamp 0001_baseline
   # 3) 升级
   alembic upgrade head
   ```
5. `scripts/init_db.py` 改造：
   - 开发/全新库：`alembic upgrade head` + 种子数据（保留 `--force` 但**只在 `APP_ENV=development` 下允许**）；
   - 生产：禁止 `db.create_all()`/删库；新增 `scripts/deploy.py`（备份 → upgrade → 一致性自检 → 打印结果）。
6. `0002` 关键写法（需兼容 SQLite / MSSQL）：
   ```python
   # SQLite: batch_alter_table（重建表以加 NOT NULL/CHECK/FK）；MSSQL: add_column + create_check_constraint
   with op.batch_alter_table('payment_nodes') as b:
       b.add_column(sa.Column('baseline_amount', sa.Numeric(14, 2), nullable=True))
       b.add_column(sa.Column('adjustment_amount', sa.Numeric(14, 2), nullable=False,
                              server_default='0'))
       b.add_column(sa.Column('version', sa.Integer(), nullable=False, server_default='0'))
       b.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))
       b.add_column(sa.Column('updated_by', sa.Integer(), nullable=True))
   op.execute("UPDATE payment_nodes SET baseline_amount = due_amount WHERE baseline_amount IS NULL")
   # 回填后再收紧 NOT NULL（SQLite batch 重建 / MSSQL ALTER COLUMN）
   ```
   `contract_revisions`、`audit_logs` 用 `op.create_table` + `op.create_index`；JSON 文本列用 `sa.Text()`（MSSQL 映射 NVARCHAR(MAX)）。
7. 回滚：`0002` 提供 `downgrade()`（删索引/表/列，SQLite 用 batch）；**回滚前必须备份**，因为会丢 `audit_logs`、`contract_revisions`、`adjustment_amount`；演练脚本记录在 `docs/`。
8. 迁移验收（DoD）：
   - 空 SQLite 库 `upgrade head` 成功；现有生产副本库 `stamp + upgrade` 成功且数据行数/金额不变（`Σdue` 回填前后一致）；
   - `downgrade -1` 成功（在副本上）；
   - MSSQL 测试实例（若已就绪）跑一遍 0001→0002；
   - 迁移后 `check_consistency.py`（节点 `due = baseline + adjustment`、`Σdue` 与合同额勾稽、明细与汇总一致）全部通过。

### 3.3 数据回填与兼容

- `baseline_amount = due_amount`、`adjustment_amount = 0`、`version = 0`：保证迁移前后报表金额不变；
- `contract_files.confirmed_values_json` 为空的历史合同：确认页回显走项目现值（本方案的必需路径），不依赖该字段；
- 旧 `result_json` 不动；
- 迁移期间停止写入（维护窗口），或按 3.2 第 4 步先停服务；
- 迁移后跑 `scripts/e2e_test.py`（修订版断言）+ P0-3 用例，确认金额合计与迁移前一致。

---

## 4. 文件影响面与测试影响

### 4.1 新增/修改文件汇总

| 类型 | 文件 |
| --- | --- |
| 新增 | `app/money.py`、`app/validation.py`、`app/audit.py`、`migrations/`（env + 2 个 revision）、`tests/test_money.py`、`tests/test_contract_flow.py`、`tests/test_migration.py`、`scripts/check_consistency.py` |
| 修改 | `app/routes.py`、`app/models.py`、`app/__init__.py`、`app/templates/contract_confirm.html`、`app/templates/project_detail.html`（小改）、`requirements.txt`、`scripts/init_db.py`、`scripts/e2e_test.py`（断言修订） |
| 不改 | `app/contract_engine.py`（除识别结果警告增强，可选）、`app/static/*`、`scripts/ocr_win.ps1` |

### 4.2 对现有 e2e 的影响（必须同步修订，不能沿用旧断言）

- `scripts/e2e_test.py:253-257`「重复确认被拦截」：现状是"静默改写项目金额"，改造后预期变为「第一次预览 → 二次确认应用」，用例需改为断言 revision/差异页/项目金额不被静默改；
- `scripts/e2e_test.py:274-296`「合同回改重新生成」：改造后断言「节点 id 不变、无删除、baseline 更新」，不能再断言节点重建；
- `scripts/e2e_test.py:300-305`（删除前造申请/付款）：保留，用于验证"回改被阻断"路径；
- 基线跑完后先保存 `docs/baseline-e2e.md`（当前 93 项通过结果），改造期间用它区分「预期变化」与「回归」。

---

## 5. 建议开工顺序（以及为什么）

### 5.1 总原则

先止损、再地基、最后动表；每一步都可独立合入、独立回滚；测试基线跑完并留档后再动 `app/`。

### 5.2 顺序

| 步骤 | 内容 | 预估 | 为什么排这里 |
| --- | --- | --- | --- |
| **0** | 基线收口：备份、`git init`/快照、保存当前 e2e 结果、把 `docs/review-logic.md` 的 T1-T14 和本文用例写成失败测试（红） | 0.5 天 | 后面每一步都要靠它判断"改坏了没有"；e2e 旧断言本身编码了 bug，必须先修订/留档 |
| **1** | **P0-3：金额/校验基础层**（`money.py` + `validation.py` + 替换全部调用点 + 单测 + 合同提交强校验） | 1.5-2 天 | 无表结构变更、可独立合入；P0-1/P0-2 的"差异计算、尾差、锁账金额"都依赖同一套 Decimal/校验，先做避免二次返工；且立刻消除负比例/105%/尾差 |
| **2** | **P0-2：现值回填 + 已发生阻断（止损）**（`contract_confirm` GET 回填、提交前 `project_occurred_blockers` 直接 422、识别值仅提示） | 1-1.5 天 | 改动集中（2 个函数 + 1 个模板），最快消除"点一次修改就清空数据"的最大风险；不需要动表；完整 diff 二次确认留到第 3 步 |
| **3** | **P0-1 完整方案 + 数据库迁移**（Alembic 接入 + `0002` 迁移/回填/演练 → 就地更新 + baseline/adjustment/version + `sync_project_dates` 重写 + 删除守卫 + `contract_revisions`/`audit_logs` + diff_hash 二次确认 + 并发 409） | 3-4 天 | 依赖第 1、2 步；涉及结构变更/并发/迁移演练，风险最高、回归最多，最后合入并单独做升级+回滚演练 |
| 4（可并行） | P0-4 全量审计埋点与查询页；P0-5/P0-6 导入导出 | 按各自计划 | 与第 3 步共用 `audit_logs` 表，但业务面独立 |

### 5.3 各步验收标准（DoD）

| 步骤 | 验收 |
| --- | --- |
| 1（P0-3） | M1-M25 全绿；`tests/test_money.py` 全绿；原 e2e 除"预期变更"外全绿；任意路径不再出现 `float` 参与金额计算 |
| 2（P0-2） | 已确认合同再次打开显示项目现值（含增补、比例）；有已发生节点的项目提交 → 422 + 原因列表，DB 金额/状态零变化；无已发生项目提交 → 差异页 + 二次确认后应用 |
| 3（P0-1） | 回改后节点 id 不变、无删除、`due = baseline + adjustment` 恒等；已发生节点 due 锁定；`contract_revisions`/`audit_logs` 完整；并发双击 → 1 次应用 + 1 次 409/幂等；迁移在副本库 upgrade/downgrade 成功且金额不变；无裸 500 |
| 4 | 按 P0-4/P0-5/P0-6 各自验收 |

### 5.4 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| 改造期间测试基线失效 | 第 0 步先留档 + 先写新测试；改造按步合入，每步跑全量 |
| 迁移把金额改错 | `0002` 只回填 `baseline=due`、`adjustment=0`，迁移前后跑一致性脚本对比 `Σdue`；先副本演练 |
| 合同页面交互变复杂 | 第一段预览 + 第二段确认；普通"未发生"项目 diff 为空时直接应用，不增加操作步骤 |
| 业务口径未定（P1-1/尾差/部分付款） | 用配置开关（`CONTRACT_TOTAL_STRICT`、`TAIL_TARGET`、`ALLOW_ZERO_RATIO`）承接，默认取现状口径 + 提示，确认后改开关即可 |

---

## 6. 待业务确认（影响开关默认值）

1. ✅ **已定（2026-10-23）**：增补金额全额参与首付款，所有节点统一按「原金额+增补」计算，节点合计=合同总额；V9 按阻断口径执行，`expected_total` 不再有首付豁免项；
2. 尾差归集到质保金 / 单列「尾差」——决定 `TAIL_TARGET` 默认值；
3. 是否允许比例为 0 的节点（如无质保金合同）——决定 V3/V4 边界与 `ALLOW_ZERO_RATIO`；
4. 增补金额是否允许负数（调减）——决定 V2 是否放行；
5. 已发生节点是否允许走「变更单差额调整」上线即支持，还是本期先只阻断（建议先阻断，二期再差额）；
6. 合同能否绑定多个项目 / 一个项目能否多份合同（影响 D8 唯一性约束，待确认后再加）。

---

## 附：关键金额恒等式与常量

```
due_amount        = baseline_amount + adjustment_amount           （PaymentNode）
baseline_amount   = quantize(base × ratio / 100, 0.01, HALF_UP)  （未发生节点 adjustment = 0）
已发生节点改口径：baseline' = 新口径值；adjustment' = due - baseline'；due 不变
contract_total    = original_amount + additional_amount
target            = Σ(base_i × ratio_i / 100)                    （base：所有节点 = 原金额 + 增补金额；2026-10-23 口径统一）
Σdue              = target（尾差归集后，误差 0）
WARRANTY_DAYS     = 365 * 3 = 1095                                （routes.py:76,82 → 常量）
金额上限          = 999,999,999,999.99（NUMERIC(14,2)）
比例精度          = 0.01 个百分点（NUMERIC(6,4) 存小数，页面按 % 两位）
```

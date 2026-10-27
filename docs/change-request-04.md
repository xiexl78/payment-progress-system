# 变更请求 04 设计说明（付款节点口径 / 申请·开票·付款三操作并列 / 合同变更）

- 版本：**v1.2**（2026-10-24，按 Chief 最终口径修订）
- 适用范围：`payment-system`（Flask + SQLAlchemy + SQLite）
- 性质：**设计文档，不含实现代码**。开发按第 12 节任务拆解执行。
- 上游依据：用户变更请求 1~4；Chief 口径判断（m01067）；Chief 口径（m01116 申请/开票并列）；**Chief 最终口径（m01125：申请付款、登记发票、登记付款三者完全并列，任意顺序，含先付款后开票，互不为前置）**。
- 口径演进：v1.1 曾按「两维并列 + 付款前两者齐备」设计，该版本中一切前置条件自 v1.2 起**全部作废**，不得实现。

---

## 1. 需求转译与已定口径

| # | 用户原意 | 本设计落点 |
|---|---|---|
| 1 | 节点字段名按每个合同约定的付款条件显示百分比，系统不设默认比例 | 比例只来自人工输入（新建项目 / 合同确认页）；删除 `NODE_TEMPLATES` 默认 ratio 与 `contract_engine.DEFAULT_RATIOS` 回退；节点名 = 节点类型 + 实际比例（如「首付款20%」）；OCR 未识别则留空，提交时必填阻断 |
| 2 | 「先申请后开票」写死不对，先到发票后申请也要能走 | **升级为三操作完全并列**：申请、开票、付款各自独立记账、任意顺序、互不为前置（含先付款后开票、付款后再补申请/补开票）；页面按三维度组合展示，不再用串行状态机卡点 |
| 3 | 新建合同不要增补金额字段；变更合同要改全部科目，展示变更前各科目金额，填入变更金额 | 新建项目表单、合同确认页移除增补输入；新建时 `additional_amount` 恒为 0，增补只由变更单产生；变更单逐科目输入「变更后该科目总额」，同页展示变更前原金额/已开票/已付/当前总额 |
| 4 | 变更后已付款节点的差额自动调整到后续支付节点；已付款节点状态不变、不出现「部分/待支付」；备注展示变更前后差异 | 已付款节点 `due_amount / paid_amount / status / ratio / node_name` **全部冻结**；差额 = 科目新总额 − 已付节点应付合计，按**剩余未付款节点原比例**分摊（Chief 口径，非单一节点），尾差只在未付节点内按 warranty→acceptance→progress→first 归集；变更前后金额写入审计并作为节点备注展示；详情页分组不再派生「部分/待支付」 |

### 与用户字面的偏差（已按 Chief 口径执行，记录备查）

1. 用户说「差额自动调整到**下一个**支付节点」；Chief 口径为「按剩余未付节点原比例分摊」。执行后者：单一节点承接会让该节点比例失真、其余节点仍停旧金额，合同减额时还容易把单节点压成负数。
2. 用户第 2 条只说「先到发票后申请」；Chief 最终明确为**三操作完全并列**，故本设计取消所有前置校验，详见 §3。

---

## 2. 目标与非目标

**目标**
- 比例、节点名称 100% 来自合同人工确认，系统中不存在任何「默认比例」。
- 申请、开票、付款三操作各自独立记账，任意顺序、可多次、互不为前置；每一维各自累计不超过节点应付。
- 页面按三维度展示进度（申请进度 / 开票进度 / 付款进度），不再用「未触发→申请→开票→付款」单一状态机做卡点。
- 合同变更按科目录入，已付款节点冻结，差额可解释、可追溯、有备注。
- 变更、报表、导出、审计、测试全部改为并列口径。

**非目标（本次不做）**
- 变更单独立表/变更单列表/审批流/附件（Q6）。
- 发票红冲/作废、付款冲销/退款（Q4）。
- 科目新增、删除、改名，比例单独修改（Q7）。
- 申请→开票→付款之间的审批流或金额匹配（Q3）。

---

## 3. 三操作并列模型（核心设计）

### 3.1 三本独立账

| 维度 | 累计字段（节点） | 明细记录 | 规则 |
|---|---|---|---|
| 申请 | `payment_nodes.applied_amount`（**新增列**） | `payment_applications`（每次申请一行，`apply_amount`、`apply_no`） | 累计申请 ≤ 应付；可多次申请 |
| 开票 | `payment_nodes.invoice_amount`（已有） | `payment_invoices`（**新增表**：`invoice_no`、`amount`、`invoice_date`、`created_by`） | 累计开票 ≤ 应付；可多次开票 |
| 付款 | `payment_nodes.paid_amount`（已有） | `payment_records`（已有，`application_id` 已可空） | 累计付款 ≤ 应付；可多次付款 |

三本账互不读取、互不校验对方是否发生。任何操作只检查：`本次金额 > 0` 且 `该维度累计 + 本次 ≤ 应付` 且 `应付 > 0`。

现状差异：`invoice_amount` 目前是节点上的汇总，明细只存在于申请单的 `invoice_amount/invoice_no` 字段。先付款后开票、无申请开票时没有申请单可挂，故新增 `payment_invoices` 明细表；节点 `invoice_amount` 继续作为累计缓存，保证现有报表/导出取数不变。
`PaymentRecord.application_id` 在 `app/models.py:218` 已是 `nullable=True`，付款可不挂申请，无需改结构。

### 3.2 数据模型变更

```text
payment_nodes（新增一列）
  + applied_amount NUMERIC(14,2) NOT NULL DEFAULT 0     # 申请累计

payment_invoices（新增表，db.create_all() 自动建；无需 ALTER）
  id INTEGER PK
  node_id INTEGER NOT NULL FK payment_nodes.id
  invoice_no VARCHAR(128) NOT NULL
  amount NUMERIC(14,2) NOT NULL
  invoice_date DATE NULL
  remark VARCHAR(255) NULL
  created_at DATETIME, created_by INTEGER
```

迁移（`app/schema.py` 幂等补列，沿用现有机制）：
- `ALTER TABLE payment_nodes ADD COLUMN applied_amount NUMERIC(14,2) NOT NULL DEFAULT 0`；
- 回填一次（可写成 `scripts/migrate_cr04.py`，事务 + 备份）：`applied_amount = COALESCE((SELECT SUM(apply_amount) FROM payment_applications WHERE node_id = payment_nodes.id), 0)`；
- 不重算 `status` 也可运行，派生标签在读取时计算；若保留缓存列，则回填后统一刷新一次（§3.4）。
- 新表由 `db.create_all()` 创建；若生产禁用自动建表，则纳入迁移脚本显式 `CREATE TABLE IF NOT EXISTS`。

### 3.3 三个动作（无前置）

| 动作 | 服务端允许条件（全部满足） | 写入 | 事后 |
|---|---|---|---|
| 发起申请 | 应付>0；申请累计+本次≤应付；本次>0 | 新增 `payment_applications` 一行；`nodes.applied_amount += 本次` | 写审计 `node.apply`；刷新派生标签 |
| 登记发票 | 应付>0；开票累计+本次≤应付；本次>0；发票号必填 | 新增 `payment_invoices` 一行；`nodes.invoice_amount += 本次` | 写审计 `node.invoice`；刷新派生标签 |
| 登记付款 | 应付>0；付款累计+本次≤应付；本次>0 | 新增 `payment_records` 一行（`application_id` 可空，可手工选择关联申请）；`nodes.paid_amount += 本次` | 写审计 `node.pay`；刷新派生标签 |

- **不存在的校验**（必须删除）：`status=='未触发'` 才能申请、`status=='未开票已申请'` 才能开票、`status=='已申请'` 才能付款、付款必须有申请、付款必须有发票。
- 并发：三个动作分别对各自累计字段做原子 CAS（`UPDATE payment_nodes SET 字段=字段+:amt WHERE id=:id AND 字段+:amt<=due_amount`），受影响行数为 0 即回滚并提示「金额已变化，请刷新」；明细行与节点累计在同一事务内提交。
- 任一动作后 `status` 只由派生函数刷新（§3.4），**不参与下一次动作的允许判断**。

### 3.4 状态展示：三维度组合，`status` 降为缓存

页面不再用单一状态表达流程，改为三列进度：

| 维度 | 展示 | 规则 |
|---|---|---|
| 申请 | 未申请 / 部分申请 ¥A / 已足额申请 ¥A | `applied_amount` 与 `due_amount` 比较 |
| 开票 | 未开票 / 部分开票 ¥B / 已足额开票 ¥B | `invoice_amount` 与 `due_amount` 比较 |
| 付款 | 未付款 / 部分付款 ¥C / 已足额付款 ¥C | `paid_amount` 与 `due_amount` 比较 |

节点总体标签（仅展示，不作卡点）：`已完成`（付款足额）/ `进行中`（任一维度有发生但未全付）/ `未开始`（三维全 0）/ `超付待处理`（历史数据，见 §3.6）。

`payment_nodes.status` 保留为**兼容缓存列**（历史报表、筛选、旧测试、导出仍读它），由统一函数 `derive_status()` 按优先级派生并写回：

```python
def derive_status(due, applied, invoice, paid):
    if due <= 0: return '未触发'
    if paid >= due: return '已付款'
    if paid > 0: return '部分付款'
    if applied > 0 and invoice > 0: return '已申请'      # 两维均有发生（不要求足额）
    if applied > 0: return '未开票已申请'
    if invoice > 0: return '已开票未申请'
    return '未触发'
```

`FLOW_STATUS` 扩为：`['未触发','未开票已申请','已开票未申请','已申请','部分付款','已付款','超付待处理']`，另加 `STATUS_LABELS` 做页面标签映射。**任何校验都不得再使用 `status`**；它只用于展示与查询。

### 3.5 页面动作按钮（全部无前置）

| 节点情况 | 可点按钮 |
|---|---|
| 应付>0 且未删除 | 「发起申请」「登记发票」「登记付款」始终可见（无权限者只读） |
| 某一维已足额 | 该维按钮置灰并提示「已达应付金额」 |
| 已付款 | 付款按钮置灰；申请/开票在未足额前仍可补办，允许补登 |
| 应付=0 | 三个按钮均置灰，提示「应付为 0」 |

按钮顺序按页面习惯排列，但不表达流程先后；页面文案避免「先…再…」。

### 3.6 历史数据兼容

- 既有 `status` 保留原值；显示改用 `derive_status` 计算（或在迁移时统一刷新缓存）。
- 历史「已申请」但发票=0 的节点：三维展示为「申请有、开票无」，不再因此拦截付款（并列口径下本就允许）。
- 历史 `超付待处理`（现有变更逻辑写入）：显示红色警示，禁止继续付款；新变更逻辑不再产生该值（改为变更前阻断，§6.2）。
- 历史无 `payment_invoices` 明细：发票汇总仍在 `invoice_amount`，明细表为空；导出/详情页对历史节点展示「历史开票（明细缺失）」；一次性回填可把有 `invoice_no` 的申请行转成发票明细（可选脚本）。

---

## 4. 比例与节点名称：彻底去默认

### 4.1 数据来源

- 新建项目：`project_form.html` 增加一组项目级比例输入（首付款/进度款/验收款/质保金，百分数，必填、无默认值），应用到该项目所有科目。
- 合同确认页：比例输入保留，但**不再回填模板默认值**；OCR/规则识别到就预填并标「来自原文」，没识别到就留空并显示「未识别，请按合同填写」。
- 修改比例的唯一入口：合同确认（重新确认）/新建。详情页不做比例编辑（Q7）。

### 4.2 代码改动

- `app/__init__.py:65-97`：`NODE_TEMPLATES` 去掉 `ratio`、`node_name`（保留 `node_type`、类型标签、触发条件等静态元数据），不再承载任何默认比例。
- `app/contract_engine.py:167`：删除 `DEFAULT_RATIOS`；`extract_ratios()`（:172-189）未识别时返回 `(None, None, False)`，**绝不回退默认值**。
- `app/contract_engine.py:485-502`（`recognize_by_rules`）与 :580 附近、:454 的 LLM prompt：不再用默认比例补位；未识别节点不生成金额，输出中 `ratio: null`。
- 节点名称统一由 `_node_name(node_type, ratio)` 生成（现位于 `app/routes.py:1579-1582`，建议移到 `app/nodes.py` 供路由/引擎/测试共用），规则：类型名 + 百分数，去掉无意义小数（`0.20 → 20%`、`0.205 → 20.5%`）。
- 创建节点时 `node_name` 与 `ratio` 一起写入；比例在任何路径发生变化（合同重新确认）时同步重算名称。
- 校验：`app/validation.py:104-124` 的 `parse_ratio_pct` 保持 0<r≤100（0% 见 Q7 附注，`ALLOW_ZERO_RATIO` 暂不改）；新增 `validate_project_ratios(form)`：4 个比例必填、合计 100%±0.01，错误一次性回显。
- 合同确认提交：`validate_contract_submission` 对 4 个比例逐个必填，缺失即报错；`build_contract_numbers` 仍负责尾差归集与合计校验。
- 展示：`project_detail.html:163` 的「科目总额×{{ meta.ratio }}%」改为按节点实际比例展示；分组表头若同组节点比例不一致，显示「按合同」并在行内展示 `node_name`。

### 4.3 历史数据兼容

- 既有项目节点 `ratio/node_name` 原样保留（30/52/15/3 等），不做数据迁移；它们就是当时合同确认的结果。
- 种子数据（`scripts/init_db.py` 使用 `NODE_TEMPLATES`）改为显式比例创建，演示项目仍可用 30/52/15/3，但属于种子数据而非系统默认。

---

## 5. 新建项目 / 合同确认：去掉增补字段

### 5.1 页面

- `project_form.html:54-69`：删除「增补金额」输入；新增项目级 4 个比例输入（无默认值）；提交时 `additional_amount=0`。
- `contract_confirm.html:140-145`：删除增补输入；提交字段不再包含 `additional_amount`；`values` 中不再显示增补。
- `contract_confirm.html:130-236` 文案同步：节点名称/金额随比例实时计算，比例缺失时行内提示「未识别，请填写」。
- `contract_confirm.html` 的 JS `recompute()` 去掉 `additional_amount` 读取，基数 = 原金额。

### 5.2 服务端

- `validation.validate_project_item_rows`（:161-204）：不再读取 `additional_amount`（忽略旧字段），返回行固定 `additional_amount=ZERO`；新增比例参数校验。
- `validation.validate_contract_submission`（:292-335）：`additional_amount` 不再从表单读取；**新建**时置 0；**回改已存在项目**时保留项目当前增补合计（见 §5.3）。
- `routes.project_new`（:540-577）、`_create_item_nodes`（:511-520）：按表单比例生成节点；`_create_item_nodes` 签名改为接收 `ratios`（`{node_type: Decimal}`）。
- `routes.item_add`（:673-715）：新增科目沿用项目现有节点比例（从同项目任一已有节点读取），不再用模板默认。
- `routes._create_contract_project`（:1388-1414）：`additional_amount=0`，节点名/比例来自 `submission`。
- `routes._apply_contract_values`（:1359-1385）：合同回改时只更新原金额、税率、比例、节点金额与名称；增补保留现值，不因表单缺失被清零。

### 5.3 增补字段语义（不变结构）

- `project_items.additional_amount` 列保留，新建恒 0；变更单写入 `新总额 − 原金额`，**允许为负**（合同减额）。
- 合同总额始终 = Σ(原金额 + 增补) = Σ 变更后科目总额；报表沿用该口径。
- 合同确认页不再展示/编辑增补；若需求方坚持回改时也要看到增补，只在页面上以只读字样展示合计（Q8）。

---

## 6. 合同变更设计

### 6.1 页面与输入

`app/templates/contract_change.html` 重做（保留 `stage=form|confirm` 两段式与二次确认）：

**第一步：录入（stage=form）**
- 顶部：合同当前总额、原金额合计、已申请合计、已开票合计、已付合计。
- 表格逐科目一行，列：科目名称 / 原金额（只读）/ 当前增补（只读，可能为负）/ 已申请合计（只读）/ 已开票合计（只读）/ 已付合计（只读）/ 当前总额（只读）/ **变更后总额**（`<input name="item_total_<item_id>">`，预填当前总额，可编辑，必填，≥0，两位小数）/ 差额（只读）。
- 合计行：变更后合同总额、与当前总额差额。
- 提示：已付款节点的金额与状态冻结；差额按剩余未付款节点比例分摊；比例不变。
- 提交到 `project_contract_change_preview`（现有路由，保留）。

**第二步：确认（stage=confirm）**
- 科目差异表：每科目 原金额、当前增补、变更后总额、差额；已申请/已开票/已付列展示。
- 节点重算明细表：每科目每节点 `原应付 → 新应付 → 差额`，标注「冻结（已有付款）」或「承接差额」，并展示三维已发生额。
- 冻结说明与不可变更清单；若存在阻断项，只显示阻断原因，无确认按钮。
- 备注预览：每节点将写入的备注文本。
- 隐藏字段：`item_total_<id>` 全量 + `confirm_token`；服务端按当前库值重算并校验 token，防旧页面重复提交。

**路由**：沿用 `GET/POST /projects/<id>/contract-change`（:1934）与 `POST /projects/<id>/contract-change/preview`（:1984），不改 URL。

### 6.2 输入校验（`app/validation.py` 新增 `validate_contract_change_items(project, form)`）

1. 每个科目都必须有 `item_total_<id>`（缺项视为错误，不允许静默跳过）。
2. 金额两位小数、非负、≤ `MAX_MONEY`；总和必须 > 0。
3. 与当前总额相同的行允许（等于不变更），但全表都相同则提示「金额未变化」并禁止确认。
4. 维度硬校验（便于给出具体科目/节点）：
   - 新总额 ≥ 该科目**已有付款**节点的应付合计，否则阻断（冻结前提下无法成立）。
   - 未付节点的 `invoice_amount > 新应付` 阻断（已开发票不能超过新应付，Q2）。
   - 未付节点的 `applied_amount > 新应付` 不阻断、只提示「申请金额超过新应付，付款上限按新应付」（申请非资金事实，Q3）。
   - 若科目无任何未付款节点，则新总额必须等于冻结合计。
   - 存在历史 `超付待处理` 节点：先处理后再变更，或按阻断处理（Q2）。

### 6.3 重算算法（替换 `_build_contract_change_plan`，:1686-1826）

对每个科目独立执行：

```
输入：item, new_total（Decimal，两位）
frozen = [n for n in item.nodes if n.paid_amount > 0 or n.status == '已付款']
         # 已有付款即冻结（含部分付款）：资金已发生；已发生节点金额锁定是既有口径
frozen_due = Σ n.due_amount (frozen)
remaining = new_total - frozen_due
if remaining < 0: 阻断（科目「X」新总额 < 已有付款节点应付合计 frozen_due）
unfrozen = [n for n in item.nodes if n not in frozen]
if not unfrozen:
    if remaining != 0: 阻断；否则全部冻结，无分配
elif remaining == 0:
    所有 unfrozen 新应付 = 0；若其 invoice_amount > 0 → 阻断（Q2）；applied_amount > 0 → 提示
else:
    ratio_sum = Σ n.ratio (unfrozen)          # 只在未付节点内归一化
    raw[n] = remaining × (n.ratio / ratio_sum)  # Decimal，逐项 ROUND_HALF_UP
    dues = allocate_tail(raw, remaining,
                         order=('warranty','acceptance','progress','first') 中属于 unfrozen 的子序列)
    # allocate_tail 只接收未付节点字典，天然不会把尾差落到冻结节点
for n in frozen:
    n.due_amount / n.paid_amount / n.status / n.ratio / n.node_name 全部不变
    备注「已有付款冻结（已付 ¥X），差额转后续节点」
for n in unfrozen:
    n.due_amount = dues[n]；ratio / node_name 不变
    n.status = derive_status(新应付, applied, invoice, paid)   # 三维派生，不读旧 status
item.original_amount 不变
item.additional_amount = new_total - original_amount   # 允许为负
```

补充规则：
- 尾差只允许落在未付节点；若未付节点全部原始分摊为 0，`allocate_tail` 抛 `ValueError` → 转业务错误「差额过小无法分摊」。
- 比例不变，因此节点名称不变；若历史数据比例合计 ≠100%，沿用现状报错，要求先修合同。
- 变更不触碰 `applied_amount / invoice_amount / paid_amount / payment_records / payment_invoices / payment_applications`。
- 冻结节点「状态不能变、不能出现部分/待支付」由上述冻结保证：其 status 原样保留；未付节点状态只按三维派生，不引入串行语义。

### 6.4 差额备注与展示

无结构迁移方案（推荐）：
- `_apply_contract_change` 的审计 `after.nodes[node_id]` 增加：
  - `change_note`：如「合同变更 2026-10-24：应付 ¥30,000.00 → ¥31,666.67；承接已付款节点差额 ¥1,666.67」
  - `change_before_due` / `change_after_due` / `change_delta`
  - `frozen: true|false`
- 冻结节点备注：「已有付款节点，应付保持 ¥X；合同差额已转后续未付节点」。
- 详情页读取该项目最新一条 `audit_logs.action='project.contract_change'` 的 `after.nodes`，渲染节点「备注」列；无变更记录显示「—」。
- 详情页展示「最近变更：时间/操作人/合同总额 A→B」，链接审计详情；不做独立表（Q6）。
- 备选（若业务要求备注可搜索/可编辑）：给 `payment_nodes` 加 `change_note TEXT NULL`（`schema.py` 幂等补列），成本低但属于结构变更；默认不采用。

### 6.5 二次确认 token

`_contract_change_token`（:1829-1836）重做：payload 绑定 `project_id + 各科目当前总额 + 各科目提交新总额 + 各节点 applied/invoice/paid`，SHA-256；确认页提交时服务端重算计划并比对 token，不一致则重新出确认页，不写库。

### 6.6 审计

`project.contract_change` 审计（`critical=True`）保留，前后快照扩展为：每科目旧/新原金额、旧/新增补、旧/新总额；每节点旧/新应付、冻结标记、备注、三维已发生额；合同总额变化。操作人、时间沿用 `audit_record`。

---

## 7. 数据模型与迁移汇总

| 项 | 结论 |
|---|---|
| `payment_nodes.applied_amount` | 新增列（NOT NULL DEFAULT 0），`schema.py` 幂等 ALTER；脚本回填历史申请合计 |
| `payment_invoices` | 新增明细表，`db.create_all()` 建；历史申请中的发票号可选回填 |
| `payment_nodes.status` | 保留列，语义降为派生缓存；`String(16)` 足够放「已开票未申请」「部分付款」「超付待处理」（SQLite 不限制实际长度；其他库建议 32） |
| `payment_nodes.invoice_amount / paid_amount` | 保留，作为累计缓存与原子 CAS 依据 |
| `payment_records.application_id` | 已可空，无需变更 |
| `project_items.additional_amount` | 保留；新建 0；变更单可为负 |
| 索引 | 新增 `payment_invoices(node_id)`；其余无新增 |
| 迁移脚本 | `scripts/migrate_cr04.py`：备份 → 加列（幂等）→ 回填 applied_amount → 可选刷新 status → 建新表 → 校验（三维 ≤ 应付、合计一致性）→ 输出报告 |

---

## 8. 路由/接口变更清单

| 路由 | 方法 | 变更 |
|---|---|---|
| `/projects/new` | GET/POST | 去增补字段；新增 4 个比例字段；节点名/比例来自输入 |
| `/projects/<id>/items`（`item_add`） | POST | 新科目沿用项目已有比例；忽略增补字段 |
| `/projects/<id>/nodes/<node_id>`（`node_update`） | POST | **删除全部状态前置**；`apply`/`invoice`/`pay` 各自按维度累计校验；增加原子 CAS；`invoice` 写 `payment_invoices`；`pay` 的 `application_id` 可空；每次动作后刷新派生 status |
| `/contracts/<id>/confirm` | POST | 去增补；比例必填无默认；生成/回改节点名称与比例 |
| `/projects/<id>/contract-change` | GET/POST | 逐科目 `item_total_<id>` 输入；冻结已有付款节点；差额分摊；旧参数（`original_amount`/`additional_amount`）保留兼容分支（Q9） |
| `/projects/<id>/contract-change/preview` | POST | 同上；输出科目差异、节点重算、阻断清单、备注预览 |
| `/projects/<id>` | GET | 三列进度；状态标签用 `STATUS_LABELS`；备注列；按钮矩阵按 §3.5 |
| `/reports` | GET | 台账增加三维列与派生状态；提醒按维度拆分（§9） |
| `/projects/<id>/export.xlsx`、`/reports/export.xlsx` | GET | 导出列增加 申请/开票/付款 三维已发生额与状态；旧列保留 |
| `/admin/audit/export.csv` | GET | 无结构变化；新动作名可在筛选/展示中出现 |

表单字段命名（新增）：
- 新建项目：`ratio_first/ratio_progress/ratio_acceptance/ratio_warranty`（百分数）。
- 节点动作：`apply_amount`、`invoice_amount + invoice_no + invoice_date`、`paid_amount + pay_date + pay_method + voucher_no + application_id?`。
- 合同变更：`item_total_<item_id>`（每科目一个），`confirm_token`。

---

## 9. 页面展示、报表与导出

1. 详情页节点表：应付 / 申请进度 / 开票进度 / 付款进度 / 状态标签 / 备注 / 操作。三维进度统一显示「已发生 ¥x / 应付 ¥y」。
2. 分组表头比例：取该组节点实际 `ratio`；同组唯一值显示百分比，否则显示「按合同」。
3. 分组 badge：不再用 `sum_paid/sum_due` 派生「部分/待支付」；改为「已付 2 · 待付 2」或「全部已付款」。已有付款节点在变更后仍显示「已付款」（若部分付款则显示「部分付款」）。
4. 操作按钮按 §3.5，不表达先后顺序；页面无「必须先…」类提示。
5. 报表台账列：应付、申请累计、开票累计、付款累计、未付、派生状态、下一动作提示（信息性：未申请 / 未开票 / 未付款按维度并列列出，不暗示必须顺序）。
6. 待办提醒（`routes.py:2043-2052`）：改为按维度生成——「有应付未申请」「有应付未开票」「有应付未付款」，各自独立计数；不再用 `('未开票已申请','已申请')` 串行筛选。
7. Excel 导出（`app/exporter.py`、`app/routes.py:2085/2103`、`scripts/export_excel.py`）：项目明细与报表汇总增加三维金额与状态列；导出测试用例同步更新字段断言。
8. 审计日志页：动作名沿用 `node.apply/node.invoice/node.pay`（若现有名字不同，保持原动作名，仅 payload 扩展），展示三维 before/after。

---

## 10. 影响文件清单

**后端**
- `app/__init__.py:65-97`：`NODE_TEMPLATES` 去 ratio/node_name；`FLOW_STATUS` 扩为 7 值；新增 `STATUS_LABELS`；导出调整。
- `app/contract_engine.py:167-189`、`:454`、`:485-502`、`:580` 附近：删默认比例与回退；识别缺比例留空。
- `app/validation.py:161-204`（项目科目行/比例）、`:207-335`（合同提交/增补可选）、`:338-367`（节点动作：改为三维累计校验、删除状态前置）、新增 `validate_project_ratios`、`validate_contract_change_items`。
- `app/routes.py`：
  - `:511-520` `_create_item_nodes`（比例参数、名称）
  - `:540-577` `project_new`（去增补、比例输入）
  - `:582-606` `project_detail`（三维进度/备注/比例展示数据）
  - `:673-715` `item_add`（沿用项目比例）
  - `:763-913` `node_update`（三维动作、CAS、发票明细、可选关联申请、派生 status）
  - `:1198-1264` 合同取值/识别值回填（去默认比例/增补）
  - `:1359-1414` `_apply_contract_values` / `_create_contract_project`
  - `:1579-1582` `_node_name` 抽到 `app/nodes.py`
  - `:1637-2000+` 合同变更区（`_build_contract_change_plan`、token、输入、确认页上下文、`_apply_contract_change`）
  - `:2015` 起 `_report_rows` 与 `:2043-2052` 待办提醒
  - `:2085` 项目导出、`:2103` 报表导出
  - 新增 `app/nodes.py`：`derive_status`、`status_label`、`node_name`、`three_dim_progress`
- `app/models.py:117-183`：`PaymentNode` 加 `applied_amount` 列与三维属性；新增 `PaymentInvoice`（约 :214 前）；`to_dict` 增加三维字段；`has_business` 增加 `applied_amount`。
- `app/schema.py`：幂等补 `applied_amount` 列。
- `app/exporter.py`：项目/报表工作表增加申请、开票、付款三列。
- `app/money.py:240-290`：复用 `allocate_tail`（只传未付节点）；可加 `allocate_by_ratios` 薄封装。
- `app/audit.py`：动作名如缺失则补 `node.apply/node.invoice/node.pay`（或沿用现状）。
- `scripts/init_db.py:43`：种子节点显式比例。
- `scripts/migrate_cr04.py`：新增迁移/回填/校验脚本。

**前端模板**
- `app/templates/project_form.html:54-69`：去增补、加比例输入与说明。
- `app/templates/contract_confirm.html:130-236`：去增补、比例必填、节点名/提示、JS 基数。
- `app/templates/contract_change.html`：整体重做（逐科目录入 / 差异+节点重算 / 阻断 / 备注预览）。
- `app/templates/project_detail.html:142-234`：三维进度列、表头比例、badge、备注、按钮矩阵、去「部分/待支付」。
- `app/templates/reports.html`：台账三维列与提醒文案。

**测试/文档**
- `tests/qa_http_cases.py`：约 41 处涉及变更/状态/比例/增补；状态前置相关用例全部改写；导出用例字段断言更新。
- `tests/README.md:23-25`：基线数字更新。
- `docs/scope.md`：状态维度、增补来源、比例来源、变更口径、三操作并列更新。
- `docs/review-technical.md`：补「变更 04」口径变更说明。
- `README.md`：口径说明更新。

---

## 11. 测试点与验收标准

### 11.1 单元测试
1. `derive_status`：due=0、paid 足额、paid 部分、applied+invoice、仅 applied、仅 invoice、全 0；历史超付值不参与派生。
2. `node_name`：20%、20.5%、3%、100%；无默认比例。
3. `validate_project_ratios`：缺失、合计 99.99/100.01、负数、0、>100。
4. 三维累计校验：每维恰好到应付通过、超 1 分拒绝、重复提交第二次触发 CAS 失败。
5. 变更分配纯函数：冻结节点不参与；比例归一化；尾差归未付质保金；尾差质保金冻结时落到验收款；remaining=0；remaining<0 抛错。
6. 变更后科目节点合计 = 新总额；合同总额 = Σ 新总额；冻结节点 due/paid/status/ratio/name 完全不变。
7. 备注文本生成（冻结节点/承接节点/无差额）。

### 11.2 接口/页面测试
8. 新建项目不填比例 → 400/回显错误，不落库；比例合计 ≠100 → 拒绝。
9. 新建项目不存在增补输入；`additional_amount=0`；节点名含实际比例。
10. 合同确认页无增补字段；OCR 无比例时 4 个比例输入为空且提交被拒。
11. **顺序矩阵（3! = 6 种）**：申请/开票/付款任意顺序都能完成；重点覆盖「付款→开票」「付款→申请」「开票→付款→申请」。
12. 三维独立累计：仅申请、仅开票、仅付款均可单独发生；各维分多次累计，最后一笔恰好到应付，再多 1 分拒绝。
13. 重复付款：同一节点两次付款（第二次为剩余额）成功；第二次若超应付被拒；表单双击不会产生两笔（CAS/防重）。
14. 已付款后补开票/补申请：额度内允许；状态标签保持「已完成」。
15. 变更单只提交部分科目 → 拒绝；提交负总额 → 拒绝；提交后总额 < 已有付款节点合计 → 阻断并给出科目名。
16. 变更后已开发票金额 > 新应付 → 阻断（若 Q2 确认允许，改为标记+提示）。
17. 变更成功：已付款节点不变；未付节点按比例承接差额；详情页备注显示前后金额；审计含 before/after；三维已发生额保留。
18. 二次确认 token：改库后旧确认页提交 → 重新出确认页不落库。
19. 变更后继续付款：按新应付上限校验；冻结节点仍可付款至其旧应付剩余（若部分付款）。
20. 报表提醒包含三个维度的独立提醒；不出现「必须先申请」类文案；导出含三维列且金额两位小数。
21. 回归：基线 235 项中受影响用例更新后全绿；`python tests/run_qa.py` 退出码 0。

### 11.3 验收标准（业务视角）
- 任意合同新建时，比例 100% 由人工填写，节点名称与页面显示百分比与合同一致；系统任何位置不再出现 30/52/15/3 的默认值（种子演示项目除外）。
- 同一节点可按任意顺序完成申请、开票、付款；三维度在页面上分别可见、分别累计；系统不因「缺另一步」拒绝任何操作。
- 合同变更页能对每个科目填写变更后金额，并看到变更前金额、已申请、已开票、已付；确认后已有付款节点状态与金额一字不变，差额落到后续未付节点，备注可读。
- 变更后不存在已付款节点显示「部分/待支付」；报表与导出能分别看到申请、开票、付款三个口径。

---

## 12. 实施任务拆解（给开发）

| 任务 | 内容 | 依赖 | 优先级 | 预估 |
|---|---|---|---|---|
| T1 | 数据层：`applied_amount` 列 + `payment_invoices` 表 + `app/nodes.py`（派生状态/标签/名称/三维进度）+ 迁移脚本 + 单测 | — | P0 | 0.5 天 |
| T2 | 比例去默认：`contract_engine` 提取、新建项目/合同确认页比例输入、节点名/比例、科目新增沿用比例 | T1 | P0 | 1 天 |
| T3 | 三操作并列：`node_update` 三维独立动作、CAS、发票明细、可选关联申请、按钮矩阵、提醒、派生 status | T1 | P0 | 1 天 |
| T4 | 合同变更重做：逐科目输入/校验/冻结/差额分摊/二次确认/token/审计 | T1、T3 | P0 | 1.5 天 |
| T5 | 备注展示：审计 note、详情页备注列、最近变更摘要 | T4 | P1 | 0.5 天 |
| T6 | 报表/导出三维列 + 测试与文档（含 6 种顺序、部分付款、导出字段） | T2-T5 | P0 | 1 天 |
| T7 | 代码评审 + 全量 QA + 手工走查（先付款后开票、部分付款、变更减额、尾差） | T2-T6 | P0 | 0.5 天 |

建议顺序：T1 → T2 与 T3 可并行（不同文件为主，`routes.py` 同一文件需错峰/合并）→ T4 → T5 → T6 → T7。写入范围提示：T2/T3/T4/T6 都会改 `app/routes.py`，必须串行或由同一人合并，避免并发覆盖。

---

## 13. 风险与备选

| 风险 | 影响 | 缓解 |
|---|---|---|
| 三维金额与 status 缓存不一致 | 展示错误、导出错误 | 所有写入走同一 `derive_status`；提供只读校验脚本比对 `derive_status` 与缓存；迁移时统一刷新 |
| 去掉全部前置后，误操作（未审先付）风险上升 | 财务合规 | 权限控制 + 审计 + 页面信息提示，不用状态卡点；若需审批另立需求（Q5） |
| 多次部分付款/开票导致对账复杂度上升 | 台账难核对 | 导出按节点+明细汇总；`payment_invoices` 保留票据要素；页面显示累计与明细链接 |
| 同一发票号/凭证号重复登记 | 重复记账 | 建议唯一约束或强提示（Q1） |
| 变更减额时已有开票/申请超过新应付 | 无法变更 | 阻断并提示先红冲/重新申请；不自动改已发生额（Q2） |
| 历史数据缺发票明细、status 语义旧 | 展示混乱 | 迁移脚本 + 详情页「历史开票（明细缺失）」标记；文档说明 |
| 41 项旧测试语义变化 | 基线红 | T6 集中更新，逐条评审而非改断言凑绿 |

---

## 14. 开放问题（需用户/Chief 确认，重点为并列后的保护）

| # | 问题 | 建议 | 影响 |
|---|---|---|---|
| Q1 | 重复操作保护：同一发票号多次登记、同一付款凭证号重复付款、双击重复提交，各保留哪些保护？ | 金额累计上限保留；同一发票号唯一（DB 唯一索引或强提示）；同凭证号重复付款强提示+二次确认；表单一次性 token（CR-05 起改为签名令牌 + `node_action_tokens` jti 防重放，TTL 2 小时）防双击。是否要求“同一笔业务只能一次”请确认 | 迁移索引/校验/页面提示 |
| Q2 | 变更降额后：已开票金额 > 新应付、或存在历史「超付待处理」节点，如何处理？ | 阻断变更，提示先红冲/冲销；超付节点先清理再变更 | §6.2 阻断清单、测试 16 |
| Q3 | 申请金额与开票金额、付款金额之间是否要有业务匹配（如付款不得超过已申请/已开票）？ | 不建议：按 Chief 口径三者并列，各自只受应付上限约束；金额差异由报表提示 | 若要求匹配，等于恢复前置，需推翻 §3 |
| Q4 | 已付款后是否允许补申请、补开票？发票红冲/作废、付款冲销是否本期需要？ | 补申请/补开票允许（额度内）；红冲/冲销本期不做，仅登记正向金额 | 动作规则与页面 |
| Q5 | 是否需要“付款审批”或操作权限分离（如申请、付款由不同角色）？ | 本期沿用现有角色权限，审批另立需求 | 权限矩阵/页面 |
| Q6 | 是否需要变更单号、变更列表页、附件、审批流？备注是否需要可搜索/可编辑（决定是否加 `change_note` 列或独立表）？ | 本期仅审计+二次确认；备注存审计。若需搜索/编辑再升级 | 数据模型与 T5 范围 |
| Q7 | 比例是否允许后续单独修改？变更单是否允许改比例或增删科目？0% 节点是否允许（如无质保金合同）？ | 比例只在新建/合同确认改；变更单只改金额；0% 维持不允许（`ALLOW_ZERO_RATIO=False`） | 校验与页面入口 |
| Q8 | 已存在项目的合同回改时，原增补金额是否保留（页面不可见）？ | 保留，避免回改把历史增补清零；只读展示合计 | §5.2/5.3 |
| Q9 | 旧合同变更参数（`original_amount`+`additional_amount`）是否保留兼容？ | 保留只读兼容分支一个版本，UI 不再暴露；下版本删除 | 旧测试与外部脚本 |
| Q10 | 新增 `payment_invoices` 表与 `applied_amount` 列是否可接受（与“尽量不动结构”相比）？备选是不加表/列，申请/发票只存节点汇总+审计，但失去明细与并发原子性 | 推荐加列+表：三本账需要独立明细与原子累计；改动小、可回滚 | 迁移方案与工期 |

---

## 15. 算例（用于走查/测试断言）

比例 30/52/15/3，科目 A 原金额 100,000，科目 B 50,000。

| 科目 | 节点 | 原应付 | 已付 |
|---|---|---|---|
| A | 首付款30% | 30,000.00 | 30,000.00（冻结） |
| A | 进度款52% | 52,000.00 | 52,000.00（冻结） |
| A | 验收款15% | 15,000.00 | 0 |
| A | 质保金3% | 3,000.00 | 0 |
| B | 四节点 | 15,000 / 26,000 / 7,500 / 1,500 | 0 |

变更：A 新总额 120,000；B 不变 50,000。

- A 冻结合计 = 82,000；remaining = 38,000。
- 未付比例 15% + 3% = 18%，归一化：验收款 38,000×15/18 = 31,666.666… → 31,666.67；质保金 38,000×3/18 = 6,333.333… → 6,333.33；合计 38,000.00，无尾差。
- 结果：A 首付/进度不变；A 验收 31,666.67、质保 6,333.33；A 合计 120,000；B 全部不变；合同总额 170,000。
- 备注：A 首付「已有付款冻结，应付保持 30,000.00」；A 验收「承接差额 16,666.67」；A 质保「承接差额 3,333.33」。

尾差例：A 新总额 120,000.01，remaining = 38,000.01。
- 验收 = 38,000.01×15/18 = 31,666.675 → 31,666.68（HALF_UP）；质保 = 6,333.335 → 6,333.34；合计 38,000.02，尾差 −0.01；未付集合内按 warranty→acceptance 归集 → 质保 6,333.33；验收 31,666.68；合计 38,000.01。✅

减额阻断例：A 新总额 70,000 < 冻结合计 82,000 → 阻断，提示「科目 A 新总额 70,000.00 小于已有付款节点应付合计 82,000.00，无法在冻结已有付款节点的前提下变更」。

顺序矩阵例（节点应付 10,000）：
- 先付款 6,000 → 后开票 4,000 → 后申请 10,000：付款累计 6,000（部分付款）、开票 4,000、申请 10,000，均 ≤10,000，全部成功；节点总体标签「进行中」。
- 后补付款 4,000：付款累计 10,000 = 应付，标签「已完成」；申请、开票各自已达上限。

---

## 附录 A：节点操作一次性 token 的存储修订（CR-05）

- CR-04 实现曾把节点操作的一次性 token 全量写入 Flask 客户端 session Cookie（`_issue_node_tokens`），导致 Cookie 超过浏览器 4093B 被丢弃，出现「操作已失效且刷新无效」。
- 自 CR-05 起，一次性语义不变，但 token 不再写入 session：改为 `itsdangerous` 签名串（载荷 `{node_id, action, 会话 sid 摘要, jti, 过期时间}`，TTL 2 小时），页面隐藏字段携带；服务端只在消费时把 `jti` 写入 `node_action_tokens(jti PK, node_id, action, session_sid, expires_at, consumed_at, created_at)`，主键冲突即判重复提交。
- 本文 Q1 所称「表单一次性 token」即指该签名令牌；三本账（申请/开票/付款）并列口径不受影响。
- 实现细节见 `docs/change-request-05.md` §2.2 / §8.3。

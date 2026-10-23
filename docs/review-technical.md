# 付款进度管理系统 · 技术评审与完善路线图

- 评审日期：2026-10-22
- 评审对象：`payment-system/`（Flask + SQLAlchemy + SQLite；app 目录约 1,600 行 Python、9 个模板，脚本约 630 行）
- 评审性质：**只读评审**，未修改任何业务代码与业务数据库
- 上线目标：**单位内部真实上线，替换现行《付款进度表》Excel**
- 部署假设：单位内网，Windows 单机或小服务器，可能多人同时使用
- 方法：通读全部 Python 与模板 → 用临时 SQLite 库 + Flask test client 复现关键行为 → 校验代码/文档一致性 → 给出分阶段改造方案

> 说明：文中标注「**实测**」的结论，均在项目仓库之外的临时数据库中通过临时脚本复现，业务库数据未改动（评审结束后已复核：3 个用户 / 1 个项目 / 12 个节点，与评审前一致）。

---

## 0. 结论摘要（5 条）

1. **系统能跑通主流程，但离"可以给真实用户用"还差一层安全与运维地基。** 全站 POST 无 CSRF、`SECRET_KEY` 有硬编码默认值、默认账号密码写在登录页上、无审计日志、无备份机制、无版本控制（目录下无 `.git`）。这些不是功能问题，是上线事故风险。
2. **数据准确性有实测缺陷，且集中在"钱"上。** 节点比例合计不等于 100% 时服务端照单全收（实测 50% 也生成项目，应付合计 5 万 ≠ 合同 10 万）；负数比例也能入库（实测 -10% → 应付 -10,000.00）；合同"修改后重新生成"会物理删除付款节点，项目已有申请/付款记录时直接 500（实测 `FOREIGN KEY constraint failed`）；清空验收日期后质保到期日残留（实测）。种子数据本身已经出现"已付款 85,800 元但没有任何付款记录"的台账矛盾。
3. **多人同时用会踩 SQLite 写锁。** 上传合同接口在 OCR 期间一直持有未提交的写事务，实测另一个写入者等满 5 秒后报 `database is locked`；扫描件 OCR 最长可达数分钟，期间其他人无法保存任何操作。
4. **可维护性不足，接手成本高。** `routes.py` 800 行同时承担 HTTP 解析、权限、业务规则、事务、文件 IO、识别编排；节点生成/金额口径/删除级联在 3~5 处重复；无迁移工具、无单元测试、无日志、无错误处理，README 与实际实现已不一致（说 20MB，实际 16MB）。
5. **迁移 MSSQL 可行但不是改个连接串。** 需解决 URI 硬编码、SQLite PRAGMA、测试脚本直连 sqlite3、`String/Text` 在 MSSQL 上的中文排序规则、大小写唯一性、无 Alembic 迁移等问题；建议阶段 1 不迁，阶段 2 完成数据库抽象与迁移工具后再定。

---

## 1. 现状梳理

### 1.1 代码规模与文件

| 文件 | 行数（含注释/空行） | 职责 | 评价 |
| --- | --- | --- | --- |
| `app/routes.py` | 805 | 全部页面路由 + 全部业务逻辑 + 合同上传编排 | 职责过载，最大风险点 |
| `app/contract_engine.py` | 516 | 文本提取（PDF/Word/TXT/图片 OCR）+ 规则识别 + LLM 适配 | 可拆分：提取器 / 识别器 / 解析器 |
| `app/models.py` | 245 | 7 张表 + 少量领域方法 | 结构基本合理，缺约束/索引/审计 |
| `app/__init__.py` | 78 | 应用工厂、节点模板、SQLite PRAGMA | 可接受，但配置与常量应外置 |
| `app/templates/*` | 9 个 | 服务端渲染页面 | 无 CSRF，逻辑简单可维护 |
| `scripts/e2e_*.py` | 474 | 依赖真实服务与真实库的端到端脚本 | 不能当回归测试（见第 8 节） |
| `scripts/init_db.py` | 112 | 建表 + 种子数据 | 无 schema 版本管理 |
| `scripts/ocr_win.ps1` | 47 | Windows 自带 OCR | 可用，但无并发隔离 |

依赖：Flask 3.x、Flask-SQLAlchemy 3.x、pdfplumber、python-docx、PyMuPDF（注意 **AGPL-3.0 许可**）、requests（实际代码用 `urllib`，该依赖可能冗余）。

### 1.2 数据模型

```
users(id, username, password_hash, display_name, role, created_at)
projects(id, project_no, name, status, trial_date, acceptance_date,
         warranty_due_date, remark, created_by→users, created_at)
  ├─ project_items(id, project_id→projects, item_name, tax_rate,
  │                original_amount NUMERIC(14,2), additional_amount NUMERIC(14,2))
  │    └─ payment_nodes(id, project_id→projects, item_id→project_items, node_type,
  │                     node_name, ratio NUMERIC(6,4), calc_base, due_amount NUMERIC(14,2),
  │                     due_date, trigger_date, invoice_amount NUMERIC(14,2),
  │                     paid_amount NUMERIC(14,2), status)
  │         └─ payment_applications(id, node_id→payment_nodes, apply_no unique,
  │                                apply_amount, invoice_amount, invoice_no,
  │                                status, applicant_id/name, apply_time)
  │              └─ payment_records(id, application_id→payment_applications,
  │                                node_id→payment_nodes, paid_amount, pay_date,
  │                                pay_method, voucher_no, operator_id/name)
  ├─ acceptance_records(id, project_id→projects, acceptance_date, conclusion, acceptor, remark)
  └─ contract_files(id, file_name, stored_path, file_type, file_size, engine, status,
                    project_id→projects(可空), contract_name, result_json, raw_text,
                    error_msg, created_by→users)
```

**注意**：金额列的数据库类型**已经是 `Numeric`，不是 Float**（这是好消息）；风险在 Python 侧计算与 SQLite 的类型亲和性，见第 4.1 节。

### 1.3 业务口径（从代码与 README 还原）

| 口径 | 规则 | 代码位置 |
| --- | --- | --- |
| 付款节点 | 每科目 4 个：首付款 30%、进度款 52%、验收款 15%、质保金 3% | `app/__init__.py:9-38` |
| 计算基数 | 所有节点（含首付款）= （原金额 + 增补金额）× 比例；尾差归集后节点合计=合同总额（**2026-10-23 口径统一**；评审时旧口径为首付按原金额，已作废） | `models.py:120-127`、`routes.py:610-616` |
| 状态机 | 未触发 → 未开票已申请 → 已申请 → 已付款 | `routes.py:294-374`、`__init__.py:41` |
| 试运行 | 登记后 进度款节点 `trigger_date` = 试运行日期 | `routes.py:69-83` |
| 验收 | 登记后 验收款 `trigger_date` = 验收日期；质保金 `due_date` = 验收日期 + 1095 天；项目 `warranty_due_date` 同 | `routes.py:69-83`、`377-400` |
| 质保口径 | 验收日期 + 365×3 天（与原始 Excel 公式一致，比日历 3 年差 1 天） | README 第 66-67 行 |
| 合同生成项目 | 自动建 1 个"合同价款"科目 + 4 节点；所有节点按（原+增补）计算（口径已统一） | `routes.py:557-654` |
| 合同回改 | 更新原项目、不新建；**删除并重建全部节点** | `routes.py:589-621` |
| 权限 | admin 全部；operator 建项目/申请/发票/付款/验收/合同；viewer 只读 | `routes.py:43-55` + 模板 |
| 合同识别 | 本地规则优先，配置环境变量后优先 LLM，失败降级规则 | `contract_engine.py:463-499` |

> **口径变更（2026-10-23，已生效）**：首付款不再只按科目原金额，所有节点统一按「科目原金额+增补金额」× 比例计算，节点合计=合同总额（尾差归集）。本文其余章节中出现的「首付款按原金额」「首付=原金额」等描述均为评审时旧口径，**以现行口径为准**，不再逐条回改。

---

## 2. 架构评审

### 2.1 分层：只有 2.5 层，缺业务服务层

现状：`模板 → 路由（含业务）→ 模型`。没有 service / repository / 表单校验层，导致：

- 路由函数平均 30~60 行，`contract_confirm_submit` 近 100 行，单函数内完成：表单取值、校验、编号生成、删旧节点、建科目、建节点、状态更新、跳转；
- 业务规则散落在路由里，模板里还藏着规则（如"首付按原金额"的旧文案——口径已于 2026-10-23 统一为按原+增补、比例默认值、金额算式在 `contract_confirm.html` 的 JS 里）；
- 同一业务规则在 Python 与 JS 各实现一遍（金额 = 基数 × 比例），改口径必须改两处，且服务端不校验前端结果。

### 2.2 `routes.py` 职责过载

一个 805 行文件里混了：认证、项目 CRUD、节点流转、验收/试运行、合同上传/确认/重识别/原文、报表聚合、用户管理、金额与日期工具函数。具体问题：

| 问题 | 位置 | 影响 |
| --- | --- | --- |
| 合同上传在请求线程内做 OCR，且写事务跨整个识别过程 | `routes.py:510-540`（`flush` 在 515，`commit` 在 538，OCR 在 524） | 写锁被占用数分钟 → 多人并发时他人写入失败（实测） |
| 合同回改用 bulk delete 物理删除节点 | `routes.py:600-602` | 绕过 ORM 级联；有申请/付款时 500（实测）；无记录时节点 ID 重建、审计链断裂 |
| 项目删除手写 5 步级联 | `routes.py:276-289` | 与 ORM `cascade` 两套机制并存，易漏（如 `payment_records.node_id` 之外的外键） |
| 事务边界混乱 | `sync_project_dates` 内部 `commit`（`routes.py:83`），调用方再 `commit` | 一次业务操作拆成两次提交，异常时数据半成品（如只改了日期没改节点） |
| 报表在内存聚合全量数据 | `routes.py:710-737` | 数据量增长后 O(n) 内存与全表扫描 |
| 无分页 | `projects`/`reports`/`contracts` 全部 `.all()` | 项目多了以后页面变慢 |
| 错误无统一处理 | 无 `errorhandler`（全仓库 grep 无） | 唯一约束/外键冲突直接 500；413 显示英文默认页（实测） |
| 无日志 | 全仓库无 `logging` | 线上问题不可查 |

### 2.3 循环 / 重复逻辑清单

| 重复项 | 出现位置 | 建议 |
| --- | --- | --- |
| 节点生成（4 节点循环） | `routes.py:181-187`、`257-262`、`610-617`、`641-648`、`init_db.py:48-62` | 抽 `services/nodes.py::create_nodes_for_item(item)` |
| 金额计算 due = base × ratio | `models.PaymentNode.recalc`、`routes.py:612/643`、`contract_engine.recognize_by_rules:419-432`、`_default_nodes:502-515`、`recognize_contract:468-484` | 唯一 `money.py::calc_due(base, ratio)`，Decimal + 统一舍入 |
| 计算基数常量 | `__init__.py NODE_TEMPLATES`、`contract_engine.CALC_BASES`、`routes.py:443 CALC_BASE_LABELS`、`routes.py:615/646` 字面量 | 收敛为一份 `NODE_SPEC`（类型/名称/比例/基数/触发条件/默认值） |
| 百分比格式化 | `routes._pct_str`、`_node_name`、模板 `(ratio*100)|int` | 收敛为一个格式化函数 |
| 删除/级联 | `models.py` 关系 `cascade` + `routes.py:276-289` + `routes.py:600-602` | 统一：DB 约束 + 服务层显式级联；禁物理删除 |
| 合同项目 remark 拼接 | `routes.py:596-598`、`629-631` 重复两次 | 抽 `build_contract_remark()`；更好的做法是结构化字段 |

### 2.4 建议目标结构（阶段 2 落地）

```
app/
├── __init__.py            # create_app、配置加载、错误处理、蓝图注册
├── config.py              # 环境变量 → 配置（SECRET_KEY/DATA_DIR/DATABASE_URL/上传上限）
├── constants.py           # NODE_SPEC、状态枚举、角色、允许扩展名
├── models.py              # 只保留模型与轻量属性
├── services/
│   ├── projects.py        # 项目/科目 CRUD、级联、软删除
│   ├── nodes.py           # 节点生成、申请、发票、付款、重算
│   ├── contracts.py       # 上传编排、确认/回改、重识别
│   ├── reports.py         # SQL 聚合报表
│   ├── audit.py           # 审计留痕
│   └── money.py           # Decimal 金额工具
├── contracts/             # 合同识别（从 contract_engine 拆）
│   ├── extractors.py      # pdf/docx/txt/image(OCR)
│   ├── rules.py           # 规则识别
│   ├── llm.py             # 大模型适配（默认关闭）
│   └── parser.py          # 结果规范化 + 校验
├── web/                   # 蓝图 + 表单校验
│   ├── auth.py  projects.py  contracts.py  reports.py  admin.py
└── templates/
tests/                     # 见第 8 节
scripts/                   # init_db / import_excel / export_excel / backup / e2e 冒烟
```

### 2.5 其他可维护性问题

- `to_dict()` 7 处定义、`jsonify` 已导入但**全项目无调用**，属死代码；无 JSON API。
- `Project.total_amount()` 是方法，`ProjectItem.total_amount` 是属性，命名不一致。
- `original_amount` 在自行建项目时表示"科目原金额"，在合同生成项目时被当成"合同总金额"，语义混用。
- 配置分散在三个文件（节点模板、状态、允许扩展名、比例默认值），改一处漏一处。
- README 与实现不一致：上传上限 20MB vs 实际 `MAX_CONTENT_LENGTH=16MB`（实测 17MB → 413）；README 声称 93 项检查（脚本中确实有 93 处断言），但 e2e 依赖真实服务与真实库、不幂等（见第 8 节）。

---

## 3. 数据模型评审

### 3.1 金额：不是 Float vs Numeric 的问题，而是"DB 用 Numeric、计算用 float"

| 结论 | 证据 |
| --- | --- |
| 列类型已经是 `Numeric(14,2)` / `Numeric(6,4)` | `models.py:82-83,109,111,114-115` |
| 但 Python 侧 30 处 `float()` 参与计算与汇总 | 全仓库 `float(` 计数 30；`routes.py:130-135,202-211,713-723`、`models.py:120-127`、`templates` 中 `|float` |
| 合同确认页金额联动是 JS 浮点计算，服务端不重算校验 | `contract_confirm.html:159-181`；服务端直接信任 `ratio_*` 与 `original_amount` |
| SQLite 的 NUMERIC 亲和性不强制类型 | 实测库内 `due_amount` 存成 integer/real（`typeof` 为 integer），`ratio` 为 real；迁移到 MSSQL 才暴露精度/溢出 |
| 无金额 CHECK 约束 | 无 `CheckConstraint`；实测可将 `ratio` 写成 -0.1000、`due_amount` 写成 -10,000.00 |
| 舍入规则不统一 | `recalc` 用 `round(float×float, 2)`（银行家舍入），合同路径也用 `round`；未定义尾差归属 |

**建议（P0）**：
1. 统一 `money.py`：`Decimal(str(x)).quantize(Decimal('0.01'), ROUND_HALF_UP)`；输入解析用 `Decimal`，禁止 `float` 参与金额；
2. 比例校验：`0 < ratio ≤ 1`，合计 = 100% ± 0.01（可配置严格/警告）；金额范围 `0 < amount ≤ 99,999,999,999.99`（Numeric(14,2) 上限）；
3. 尾差口径需业务定：四舍五入后 4 节点合计与合同金额差几分的处理（建议记入质保金或单列"尾差"）；
4. DB 加 `CheckConstraint`（SQLite 会实际生效，MSSQL 也一致）。

### 3.2 冗余字段与一致性（最严重的数据问题）

| 冗余/派生 | 现状 | 风险 |
| --- | --- | --- |
| `payment_nodes.paid_amount / invoice_amount / status` ↔ `payment_applications / payment_records` | 双写，无事务内重算，无明细对账 | **实测种子数据已漂移**：进度款节点 `paid_amount=68,120`、`status=已申请`，但 `payment_applications`/`payment_records` 均为 0 → 报表"已付 85,800 元"与实际台账不符；Excel 迁移时同样会遇到 |
| `projects.warranty_due_date` ↔ `acceptance_date` | 派生字段，只在登记/编辑时更新 | 实测：清空验收日期后 `warranty_due_date` 仍为 2029-03-09 |
| `payment_nodes.status` ↔ `payment_applications.status` | 两处状态机 | 付款后两处都改，但撤销/部分付款路径不存在，状态会互相矛盾 |
| `contract_files.contract_name` ↔ `projects.name` | 双份名称，回改时同时改 | 改合同名会静默改项目名 |
| `projects.remark` 存合同金额/税率/付款方式 | 字符串拼接 | 合同结构化字段缺失（合同编号、金额、税率、付款方式、签订日期），回改覆盖后历史丢失 |
| `applicant_name / operator_name` 快照 | 合理（防改名） | 保留 |

**建议（P0/P1）**：
- 节点金额与状态改为**由明细汇总推导**（`paid_amount = SUM(payment_records.paid_amount)`，`invoice_amount = SUM(...)`, `status` 由明细推导），或在每个写操作的事务内统一重算，禁止多处直接赋值；
- 增加一致性自检脚本（阶段 1 交付），迁移/回改后跑一次；
- 质保到期日等派生字段集中在一个 `sync_project_dates` 且**幂等**：验收日期为空时清空对应派生字段；
- 合同元数据改为结构化列，`remark` 不再塞数据。

### 3.3 缺索引（实测：库里只有 3 个唯一索引）

实际索引只有：`users.username`、`projects.project_no`、`payment_applications.apply_no`。所有外键列与列表排序字段都无索引：

- `payment_nodes.project_id`、`payment_nodes.item_id`
- `payment_applications.node_id`、`payment_records.node_id`、`payment_records.application_id`
- `project_items.project_id`、`acceptance_records.project_id`
- `contract_files.project_id`、`contract_files.created_by`
- 各表 `created_at`（列表页 `ORDER BY created_at DESC`）

当前数据量小无感，但报表/详情页每次都全表扫描，项目上几十个后明显变慢。**P1（阶段 1 顺带加，成本低）**。

### 3.4 约束缺失

| 缺什么 | 后果 |
| --- | --- |
| `CHECK (ratio > 0 AND ratio <= 1)` | 负比例/超 100% 入库（实测） |
| `CHECK (due_amount >= 0)` 等 | 负数金额入库 |
| `CHECK (status IN (...))` | 状态字符串随意写入，拼写错误无感知 |
| `CHECK (acceptance_date <= warranty_due_date)` | 日期逻辑矛盾无感知 |
| 外键 `ON DELETE` 策略 | 删除行为依赖应用层，两套机制（见 3.5） |
| `contract_files.project_id` 唯一性/绑定历史 | 一个项目可被多个合同指向，回改/删除后难以追溯 |
| 导入批次/来源字段 | Excel 迁移无法区分"历史数据"与"系统录入" |

### 3.5 级联删除：目前是"两套机制、都不完整"

- `models.py` 有 ORM `cascade='all, delete-orphan'`（项目→科目/节点/验收，科目→节点，节点→申请，申请→付款记录）；
- 但 `routes.py:276-289` 项目删除又手写 bulk delete，`routes.py:600-602` 合同回改也 bulk delete；
- bulk delete **不触发** ORM cascade，而 SQLite 已开 `PRAGMA foreign_keys=ON`，且外键**没有** `ON DELETE CASCADE` → 父行删除时会因存在子行而报 `FOREIGN KEY constraint failed`。

**实测结果**：

| 操作 | 结果 |
| --- | --- |
| 已确认合同回改（项目已有申请+付款记录） | `IntegrityError: FOREIGN KEY constraint failed` → 用户看到 500，事务回滚 |
| 已确认合同回改（项目无任何申请/付款） | 成功，但节点被删除重建（ID 全变），历史引用断链 |
| 管理员删除有业务数据的用户 | `IntegrityError` → 500（`projects.created_by`、`applicant_id`、`operator_id` 引用） |
| 项目删除（有申请/付款） | 正常（手写按依赖顺序删除，临时规避了外键问题） |

**建议（P0）**：财务业务数据**不做物理删除**——项目/节点改软删除（`deleted_at`），删除动作写审计；用户改"停用"（`is_active`）而非删除；合同回改改为**就地更新**节点金额/比例（保留节点 ID 与付款明细），或对已有付款的项目禁止回改并提示走"冲销/变更"流程。

### 3.6 缺失的表

| 表 | 必要性 | 优先级 |
| --- | --- | --- |
| `audit_logs`（操作留痕） | 政务/财务场景基本必需，见第 10 节 | P0 |
| `schema_version` / Alembic `alembic_version` | 无迁移工具，后续改表只能删库重建 | P1（阶段 2） |
| `import_batches` 或 `remark` 来源标记 | Excel 迁移可追溯、可回滚 | P1（迁移时加） |
| 附件表（发票/付款凭证扫描件） | 目前只有凭证号文本，财务审计常需附件 | P2 |

---

## 4. 安全评审

> 定性：现状是"内网单机演示系统"的安全水平，直接上线给真实用户（尤其涉及付款）不达标。

### 4.1 认证与会话

| 项 | 现状（实测） | 风险 | 优先级 |
| --- | --- | --- | --- |
| `SECRET_KEY` | 硬编码默认值 `payment-progress-secret-key-change-in-prod`，仅当环境变量缺失时使用 | 未配置环境变量即可用公开密钥伪造 session cookie（如 `user_id=1`）→ 任意人可成管理员 | **P0** |
| 默认账号 | `admin/admin123`、`jbr/123456`、`viewer/123456`；登录页直接展示默认账号 | 上线后弱口令立即可被利用 | **P0** |
| 会话有效期 / 失效 | 非 permanent session（浏览器关闭即失效），但**无服务端过期**；改密/停用/删除用户后旧会话仍有效 | 离职账号、共享电脑风险 | **P0** |
| Cookie 属性 | `HTTPONLY=True`（好），`SAMESITE=None`（浏览器默认 Lax），`SECURE=False` | HTTPS 下 cookie 未标记 Secure；无 `__Host-` 前缀 | P0（随部署） |
| 登录限速/锁定 | 无 | 暴力破解无成本，无失败审计 | **P0** |
| 改密/重置 | 无任何修改密码入口 | 用户无法更换初始密码，"强制改密"也无从落地 | **P0** |
| 密码存储 | `werkzeug.generate_password_hash`（scrypt 默认），校验正确 | 算法合格；但无强度策略（新建用户可设 1 位密码） | P0（策略） |
| 登出 | `GET /logout` | 可被第三方页面/预加载触发（配合无 CSRF） | P1 |

### 4.2 CSRF

- 全站 POST 表单（建项目、删项目、申请、发票、付款、验收、合同上传/确认/重识别、建删用户）**均无 token**，`grep csrf` 全仓库 0 匹配；
- 后果：内网钓鱼页、恶意邮件、被入侵的同事浏览器，都能以已登录管理员身份提交付款/删除操作；配合 `GET /logout` 还能强制踢下线；
- 内网不是豁免理由（政务内网也有横向移动与钓鱼）。
- **P0**：引入 `Flask-WTF` 的 `CSRFProtect`（或自建 token），所有 POST 校验；登出改 POST；`SECRET_KEY` 必须稳定且保密（否则 token 失效）。

### 4.3 授权与越权

| 项 | 现状 | 风险 | 优先级 |
| --- | --- | --- | --- |
| 角色粒度 | admin / operator / viewer 三个粗粒度角色 | 无"部门/项目"数据范围；任一 operator 可改所有项目 | P1（政务多部门场景） |
| 只读用户可见范围 | **实测** viewer 可访问 `/contracts`（列表）与 `/contracts/<id>/raw`（合同全文/金额/付款条件） | 合同属于敏感商业信息，只读人员是否应看全文需业务确认 | P1（需用户定） |
| 合同确认页 GET | 仅 `login_required`，viewer 可打开确认页（POST 被角色拦截） | 信息暴露 | P2 |
| 金额修改权限 | operator 即可通过合同回改/建项目改金额，无二次确认、无审批 | 内控缺陷（谁申请谁付款谁改价） | P1 |
| 用户删除 | 可删除有业务数据的用户 → **实测 500**；无停用/改密/重置 | 数据完整性 + 运维不可用 | P0（改停用） |
| 权限实现 | 装饰器 + 模板隐藏按钮，无统一策略层 | 漏加装饰器即越权；无自动化权限矩阵测试 | P1 |
| 对象级校验 | 无 | 越权访问他人项目（当前无项目归属概念，可接受内网单部门） | P2 |

### 4.4 上传文件安全

| 项 | 现状 | 评估 |
| --- | --- | --- |
| 扩展名白名单 | `pdf/docx/txt/md/jpg/jpeg/png/bmp/webp` | 合理，但**只校验扩展名**，不校验文件头/内容 |
| 存储命名 | 以记录 ID 命名（`{id}.{ext}`），不用原始文件名 | **好**，天然防路径穿越 |
| 大小限制 | 全局 `MAX_CONTENT_LENGTH=16MB`；README 写 20MB | **不一致（实测 17MB → 413）**；受 413 时是英文默认错误页 |
| 内容校验 | 无 magic bytes、无 ZIP 炸弹防护（docx 是 zip）、无 PDF 页数上限 | 可被构造文件拖垮解析；低风险但应加 |
| 存储位置 | `instance/uploads/`（项目目录内），无访问控制，靠部署不暴露 | 需移到数据目录并限制权限；备份要包含 |
| 文件访问 | 无原文件下载/预览路由，只能看提取文本 | 业务核对不便（P1） |
| 清理 | 上传失败/删除合同/删除项目后文件不清理 | 磁盘只增不减（P2） |
| 并发 | 临时 OCR 图片用固定文件名 `contract_scan_{i}.png`（`contract_engine.py:269`） | **并发上传会互相覆盖，可能串数据**（P1） |

### 4.5 OCR 的命令执行风险

`contract_engine.py:318-338` 用 `subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File', script, '-ImagePath', path])`：

- **当前不存在命令注入**：路径由服务端生成（记录 ID + 白名单扩展名），不拼接用户文件名；list 传参由 Python 负责转义；`ocr_win.ps1` 只做只读 OCR；
- 但有三个待改进点：
  1. `powershell` 依赖 PATH 查找（Windows 会先查当前目录），若进程工作目录可写，存在可执行文件劫持的理论风险（低）；
  2. `-ExecutionPolicy Bypass` + 子进程无资源限制（超时 120s、无页数上限）→ 恶意/超大 PDF 可长时间占 CPU（DoS）；
  3. 临时文件在系统 temp，固定命名，多用户并发互相覆盖。
- **若未来把原始文件名、用户输入拼进命令行，就会变成注入点**——本次评审结论是"当前安全，但要写进编码规范禁止拼接"。

### 4.6 大模型（LLM）外发

- 配置 `CONTRACT_LLM_*` 环境变量后，**合同全文前 8,000 字会发往外部 OpenAI 兼容接口**（`contract_engine.py:365-405`）；
- 政务/采购合同常含敏感信息（甲乙方、金额、付款条件），外发需单位审批；且目前**无开关审计、无脱敏、无出网白名单**；
- 提示注入：合同正文可包含指令，模型输出被当作"识别结果"；流程上仍需人工在确认页确认（**这是好的设计**），但服务端不校验模型返回的比例/金额（实测可入库负比例）→ 必须加范围与合计校验；
- Key 仅存环境变量（好），但无轮换/最小权限说明。
- **建议**：默认关闭 LLM（`CONTRACT_LLM_ENABLED=false` 显式开启），页面上显示"本次识别是否走了外部模型"，并记录审计；仅对非敏感合同启用。

### 4.7 其他安全项

| 项 | 现状 | 优先级 |
| --- | --- | --- |
| 错误页 | 无自定义 `errorhandler`，413/404/500 为 Flask 英文默认页（实测 413），泄露框架信息 | P1 |
| 日志/审计 | 无应用日志、无登录失败记录、无操作留痕 | P0（见第 10 节） |
| XSS | Jinja 自动转义开启，无 `|safe`（grep 0 匹配），`raw_text` 在 `<pre>` 中输出 | 低风险，无需改 |
| SQL 注入 | 全部 ORM 参数化，无裸 SQL | 无风险；但 `contains()` 未转义 `%/_` 通配符，仅搜索语义问题 |
| 安全响应头 | 无 CSP/X-Frame-Options/Referrer-Policy/HSTS | P2（内网低成本可加） |
| 依赖 | `requirements.txt` 只有 `>=`，不可复现；PyMuPDF 为 **AGPL-3.0**（对外分发/SaaS 有开源义务；纯内部使用一般可接受，需法务确认）；无漏洞扫描/SBOM | P1 |
| 数据库/备份加密 | DB 与上传文件明文；Windows 单机可依赖 BitLocker | P1（部署时） |
| HTTPS | 未实现，README 让用户自行反代 | P0（部署方案里给） |

---

## 5. 并发与事务

### 5.1 SQLite WAL 够不够？

**短事务 + 少量并发：够用。** 现状已开 WAL + `busy_timeout=5000` + `foreign_keys=ON`（`__init__.py:61-73`），对"内网 5~20 人、点几下保存一下"的场景基本可行。

**但当前代码有两个结构性障碍：**

1. **长事务持写锁（实测）**：合同上传在 `db.session.flush()` 之后、`commit()` 之前做文本提取/OCR（最长 120s/页，多页 PDF 可达数分钟）。SQLite 同一时刻只允许一个写事务，实测另一个写入者等满 5 秒后报 `database is locked`。→ 多人同时用，一个人传扫描件，其他人保存就报错；且 Flask 开发服务器多线程下错误是 500。
2. **事务边界混乱**：19 处 `commit()`，`sync_project_dates` 内部先提交一次，路由再提交；失败路径可能留下半成品（日期改了、节点没改）。→ 改为每个请求一个事务（UoW），或显式 `with db.session.begin()`，识别等慢操作移出事务。

### 5.2 竞态（多人同时操作）

| 场景 | 机制 | 后果 |
| --- | --- | --- |
| 两人同时发起付款申请 | `make_apply_no()` = 秒 + 微秒%1000，唯一约束 `apply_no` | 碰撞 → `IntegrityError` 500 |
| 两人同时确认合同生成项目 | `_next_contract_project_no()` 先查后插 | 唯一冲突 500；`project_no` 由用户手填时也会撞 |
| 两人同时登记同一节点付款 | `node.paid_amount = float(...) + amount` 读-改-写，无版本/无锁 | SQLite 下可能 `BUSY` 500；换 MSSQL 会静默丢更新（两次付款只记一次） |
| 项目编号手填重复 | 只查一次不捕获唯一冲突 | 500 |
| 删除与编辑并发 | 无软删除/无版本 | 覆盖或 404 |

### 5.3 建议

- **P0**：上传/识别改为"先落记录并提交 → 后台识别（线程或任务表）→ 再更新结果"，请求内不持写事务；长 OCR 加页数上限（如 20 页）与总超时；
- **P0**：金额汇总以明细为准（或在事务内重算），避免读-改-写丢更新；
- **P1**：编号生成用数据库序列/`UUID` 后缀/重试；唯一冲突统一捕获为友好提示；关键表加 `version` 乐观锁；写操作对 `OperationalError: database is locked` 做有限重试（如 3 次指数退避）；
- **P1**：单进程部署（waitress 单进程多线程），禁止 gunicorn 多 worker 打 SQLite；用户数或并发上去后再评估 PG/MSSQL。

---

## 6. 部署评审与建议（内网 Windows 单机/小服务器）

### 6.1 现状问题

| 项 | 现状 | 风险 |
| --- | --- | --- |
| 运行方式 | `python run.py` 用的 **Werkzeug 开发服务器**，无服务化 | 机器重启/注销即停；无自动拉起；性能与稳定性不达生产 |
| `SECRET_KEY` | 默认硬编码 | 见 4.1 |
| 数据库位置 | 写死在**项目目录** `instance/payment_progress.db` | 程序装在 `Program Files` 等只读目录时无法写库；升级代码可能误删数据 |
| 上传目录 | 项目目录 `instance/uploads/` | 同上；备份容易漏 |
| 配置 | 无配置文件/环境变量入口（除 SECRET_KEY 与 LLM） | 换机器要改代码 |
| 日志 | 无 | 故障无法定位 |
| 健康检查 | 无 | 无法做服务监控 |
| 备份 | **完全没有** | 单机故障/误删 = 数据全丢，政务场景不可接受 |
| 版本控制 | 目录下**无 `.git`**（实测） | 无历史、无回滚、多人改代码会冲突 |
| 依赖锁定 | `>=` 范围 | 一年后再装可能装到不兼容版本 |
| HTTPS | 无 | 登录密码/付款数据明文传输 |
| 迁移工具 | 无（`create_all` + `init_db --force` 删库重建） | 后续任何表结构变化都可能丢数据 |

### 6.2 目标部署形态（建议）

```
单位内网
┌───────────── Windows Server 2019/2022（或 Win10/11 单机）─────────────┐
│  IIS（可选，HTTPS 终结，单位 CA 证书）或直接内网 8080                     │
│      ↓                                                                  │
│  Waitress（单进程 8 线程，Windows 服务，NSSM/WinSW 托管）                │
│      ↓                                                                  │
│  C:\Apps\payment-system\          代码（只读，服务账号仅有读权限）        │
│  C:\ProgramData\PaymentSystem\                                         │
│      data\payment_progress.db    数据（仅服务账号+管理员可写）           │
│      uploads\                    合同文件                                │
│      logs\                       应用/访问日志（按天轮转，保留 90 天）    │
│      backups\                    备份（db + uploads，保留策略见 6.4）     │
│  计划任务：每日 2 次在线备份 + 健康检查 + 日志清理                        │
└────────────────────────────────────────────────────────────────────────┘
```

具体建议：

- **应用服务器**：`waitress-serve --listen=127.0.0.1:8080 run:app`（纯 Python、Windows 友好、单进程多线程，避免多 worker 争 SQLite 写锁）；IIS ARR/nginx 反向代理终结 HTTPS；或内网直连 8080（不推荐，密码明文）。
- **服务化**：NSSM 或 WinSW 注册为 Windows 服务，开机自启、崩溃自动重启；服务账号为专用低权限账号，禁止交互登录。
- **目录与权限**：代码与数据分离（上表）；用 `icacls` 收紧数据目录 ACL；防病毒软件排除数据目录（避免扫描锁文件导致 `database is locked`）。
- **配置**：新增 `config.py`，从环境变量读取 `SECRET_KEY`、`DATA_DIR`、`DATABASE_URL`、`MAX_UPLOAD_MB`、`SESSION_LIFETIME_HOURS`、`LLM_ENABLED`；**启动时校验 `SECRET_KEY` 非默认值，否则拒绝启动**。
- **网络**：Windows 防火墙仅放行内网网段到 443/8080；关闭默认账号；限制 RDP 来源。
- **可观测**：`/healthz`（DB 可读 + 磁盘剩余 + 版本号）；应用日志 + waitress 访问日志；每日巡检脚本输出项目数/备份状态/磁盘空间到日志或邮件。
- **升级流程**：git tag + 备份 → 停服务 → 替换代码（不动 data）→ 执行 Alembic 迁移 → 启动 → 冒烟（登录/详情/报表）→ 记录变更。
- **并发容量**：SQLite + 单进程线程，5~20 人、日常写入频率下够用（修完长事务后）；若并发人数 >20、或需要多台/多部门，再评估 PostgreSQL/MSSQL。

### 6.3 备份与恢复（P0，上线前必须有）

| 项 | 方案 |
| --- | --- |
| 备份内容 | `payment_progress.db`（含 WAL 一致性）+ `uploads/` + 配置；**用 SQLite 在线备份 API**（`sqlite3.Connection.backup` 或 `VACUUM INTO`），不要直接复制正在写的 db 文件 |
| 频率 | 每日 2 次（如 12:30、18:30）+ 服务停止前 + 重大操作（导入/批量删改）前手动 |
| 保留 | 7 日报 + 4 周报 + 12 月报；至少一份在另一块盘或另一台机器/NAS |
| 校验 | 备份后 `PRAGMA integrity_check` + 记录项目/节点行数 + 文件大小；失败写事件日志/告警 |
| 恢复 | 停止服务 → 保留现场（改名隔离）→ 用最近备份恢复 db+uploads → 启动 → 核对项目数/付款合计/登录 → 记录恢复时间与结果 |
| 演练 | 每季度一次恢复演练，把"恢复成功率/耗时"写进运维记录 |
| 回滚 | 数据迁移/大版本升级前做全量快照，保留到双轨期结束 |

### 6.4 一次性上线动作清单

1. 初始化 git 仓库并提交现状快照（含代码、README、样例外的一切；`instance/` 入库排除）；
2. 做一次现状全量备份（db + uploads）；
3. 改配置：`SECRET_KEY`、数据目录、上传上限；
4. 建默认管理员（强密码，首次登录强制改密），停用/删除示例账号；
5. 装服务化 + 备份计划任务 + `/healthz` 监控；
6. 用 Excel 导入脚本试导入 → 核对 → 正式导入；
7. 双轨运行 1~2 周（旧 Excel 只读），双方金额核对一致后停表归档；
8. 培训与操作手册（含"停用旧表的时间点"、"付款登记口径"、"回改流程"）。

---

## 7. MSSQL 迁移可行性

结论：**技术可行，ORM 主体可移植；但不是改连接串，工作量约 8~16 人时代码改造 + 4~8 人时联调验证（不含数据迁移）。建议阶段 1 不迁，阶段 2 完成数据库抽象 + Alembic 后再决定。**

### 7.1 阻断点清单

| 位置 | 现状 | 影响 | 改造 |
| --- | --- | --- | --- |
| `app/__init__.py:49` | `'sqlite:///' + 绝对路径` 写死 | 无法切换数据库 | 读 `DATABASE_URL`，缺省用 SQLite |
| `app/__init__.py:61-73` | 无条件执行 `PRAGMA journal_mode/busy_timeout/foreign_keys` | MSSQL 连接时每次抛异常（被 `except: pass` 吞掉） | 按 `engine.dialect.name == 'sqlite'` 注册 |
| `scripts/init_db.py:70` | `URI.replace('sqlite:///','')` 当文件路径 | 换库后误删/路径错 | 用 `engine.url.database`；删库逻辑仅 SQLite 分支 |
| `scripts/e2e_test.py` | 直接 `sqlite3` 连接库做断言 | 迁移后测试全废 | 改为 ORM 断言 / test_client |
| `models.py` 的 `String/Text` | 非 Unicode 类型 | MSSQL 上中文依赖排序规则，可能乱码 | 改 `db.Unicode/db.UnicodeText` 或统一 UTF-8 collation |
| 唯一约束 `username/project_no` | SQLite 区分大小写，MSSQL 默认不区分 | 行为变化：`Admin`/`admin` 可能冲突或登录行为不同 | 入库统一小写/显式 collation |
| SQLite 类型亲和性 | 不校验类型/精度；MSSQL DECIMAL 严格 | 现有"超出 Numeric 上限也不报错"的输入，迁移后报错暴露 | 阶段 1 先加服务端范围校验 + Decimal |
| 日期/时间 | naive date/datetime | 两库一致 | 无需改 |
| 批量删除/关系 | ORM 通用 | 无需改 | — |
| 无 Alembic | `create_all` + 删库重建 | 迁移/后续演进不可重复 | 阶段 2 引入 Alembic（先 SQLite 跑通） |
| 驱动 | 无 | MSSQL 需 `pyodbc` + ODBC Driver 18 | 新增依赖与部署文档 |
| 并发模型 | WAL/busy_timeout 假设 | MSSQL 行锁/隔离级别不同，长事务问题依旧 | 修事务边界后再评估 |

### 7.2 建议

- 阶段 1：**留在 SQLite**（单机/小服务器 + 5~20 人，够用且省运维），但把 `DATABASE_URL` 抽出来、加 Alembic；
- 阶段 2：完成数据库抽象、去掉 `sqlite3` 直连测试，做一次"临时实例迁移演练"；
- 阶段 3：如单位有国产化/集中运维要求（MSSQL、达梦、人大金仓等），再正式选型迁移——注意国产库的 SQLAlchemy 方言成熟度差异较大，需单独验证。

---

## 8. 可测试性

### 8.1 现状问题

| 问题 | 证据 |
| --- | --- |
| 无单元测试、无 pytest、无 CI | 仓库无 `tests/`、无配置 |
| e2e 依赖已启动的真实服务、固定端口 5001、真实数据库 | `scripts/e2e_test.py:14-15` |
| e2e 直接 `sqlite3` 连库断言 | 全文 `sqlite3`；换库即废 |
| e2e 不幂等、会污染数据 | 固定编号 `PJ-E2E-001`；会改种子项目的验收/试运行日期；重复执行必然失败 |
| e2e 断言只看页面文本/DB，不覆盖异常路径 | 无负比例、无权限越权、无 413/500、无并发 |
| 识别引擎无任何测试 | 纯函数、已有样例合同，**最容易测却没测** |
| 无 lint/format/类型检查 | — |
| 无日志，测试失败难定位 | — |

### 8.2 建议（阶段 2 主体，阶段 1 先补关键用例）

```
tests/
├── conftest.py                 # create_app 覆盖临时 SQLite + 种子用户 + 登录 client fixture
├── unit/
│   ├── test_money.py           # Decimal、舍入、尾差、范围校验
│   ├── test_contract_engine.py # 样例 txt/docx、大写金额边界、税率、比例、无金额
│   └── test_node_rules.py      # 节点生成、基数、状态机推导
├── integration/
│   ├── test_auth.py            # 登录/失败限速/会话过期/改密/停用
│   ├── test_csrf.py            # 无 token 的 POST 被拒
│   ├── test_permissions.py     # 角色 × 路由矩阵（含 viewer/operator/admin）
│   ├── test_projects.py        # 建/改/删（软删）/级联
│   ├── test_payment_flow.py    # 申请/发票/付款/部分付款/撤销
│   ├── test_contracts.py       # 上传→确认→回改（有付款时禁止/就地更新）→重识别
│   ├── test_audit.py           # 关键操作留痕
│   └── test_errors.py          # 404/413/500 友好页面
└── smoke/                      # 部署后冒烟（保留脚本，随机编号+自动清理）
```

- `create_app(config)` **已支持传配置**（`__init__.py:53-54`），这是良好的可测试性基础，直接复用；
- 阶段 1 验收门槛：安全/金额/权限/回改 四类用例必须绿；
- 阶段 2 目标：业务服务层覆盖率 ≥60%，CI 跑 `pytest + ruff`；
- e2e 脚本降级为"部署后冒烟"，跑在快照副本或临时库上，不碰生产数据。

---

## 9. Excel 历史数据迁移方案

> 前置：仓库里**没有**那份《付款进度表（新增动设备监测）.xlsx》（README 提到但未入库，仅种子数据）。下表中的字段映射是基于 README 与 `init_db.py` 种子数据反推的**假设**，拿到真实表后要复核。

### 9.1 字段映射（假设版，需按真实表调整）

| Excel 列 | 系统落点 | 转换/校验 |
| --- | --- | --- |
| 项目编号 / 序号 | `projects.project_no` | 缺失则生成 `PJ-IMP-YYYYMMDD-###`；重复报错 |
| 项目名称 | `projects.name` | 必填；去空格 |
| 项目状态 | `projects.status` | 映射到 `进行中/已完成/已归档`，未知值报错 |
| 试运行日期 | `projects.trial_date` + 进度款 `trigger_date` | Excel 序列号/字符串日期均需解析；空允许 |
| 验收日期 | `projects.acceptance_date` + 验收款 `trigger_date` | 同上 |
| 质保到期 | `projects.warranty_due_date` + 质保金 `due_date` | 校验 = 验收 + 1095 天（口径已定，跨闰年差 1 天属正常） |
| 备注 | `projects.remark`（另加 `import_batch` 标记） | — |
| 科目/费用项 | `project_items.item_name` | 同项目内去重 |
| 税率 | `project_items.tax_rate` | 归一为 `13%` 形式 |
| 原金额 | `project_items.original_amount` | Decimal，>0；合计对账 |
| 增补金额 | `project_items.additional_amount` | Decimal，≥0 |
| 首付款金额/比例 | `payment_nodes(first).due_amount / ratio` | 校验 =（原金额+增补）× 比例（现行口径，2026-10-23 统一；旧口径为原金额 × 比例） |
| 进度款 / 验收款 / 质保金 | 对应节点 `due_amount / ratio` | 校验 = (原金额+增补) × 比例 |
| 已开票金额 | `payment_nodes.invoice_amount`（+ 可选申请单） | ≤ 应付（如有超开需警告） |
| 已付金额 | `payment_nodes.paid_amount`（+ 可选付款记录） | ≤ 应付（如有超付需警告） |
| 已付日期/凭证号/付款方式 | `payment_records.pay_date/voucher_no/pay_method` | 若 Excel 有此列；否则留空 + 审计标记"历史导入" |
| 发票号 | `payment_applications.invoice_no` 或独立字段 | 若 Excel 有 |
| 付款状态 | `payment_nodes.status` + 申请单状态 | 枚举校验；与金额一致性校验（如"已付款但已付金额=0"报警） |

### 9.2 导入脚本设计（`scripts/import_excel.py`）

```
python scripts/import_excel.py --file 付款进度表.xlsx --sheet Sheet1 --dry-run
python scripts/import_excel.py --file 付款进度表.xlsx --sheet Sheet1 --commit --batch IMP-20261022
```

- **三阶段**：解析 → 校验（生成 `errors.csv` / `warnings.csv` / `diff.csv`）→ 写库（单事务，失败全回滚）；
- **只读源文件**（openpyxl `read_only=True`），支持合并单元格、多级表头；
- **幂等**：`project_no + item_name` 为业务键，重复则跳过（或 `--update`）；
- **可回滚**：写入 `import_batch` 标记（项目 remark 或独立表），支持 `--rollback IMP-xxx` 删除该批次数据；
- **历史付款的表达**（需用户决策）：
  - 方案 A（推荐）：按 Excel 只回填节点汇总（`paid_amount/invoice_amount/status`），**不造付款记录**——但会重现"已付款但无明细"的台账矛盾，需要报表上标注"历史口径"；
  - 方案 B：为历史已付款生成 `PaymentRecord`（`application_id=NULL`，`pay_date` 留空或取 Excel，`voucher_no` 尽可能补），台账完整但"没有申请单"；
  - 方案 C：同时生成申请单 + 付款记录（状态按 Excel），最完整但对 Excel 信息要求高。
  - **建议**：请用户明确"历史付款是否必须可追溯到凭证"；若必须，选 B/C，并让业务补凭证号。
- **输出**：导入摘要（项目/科目/节点条数、金额合计、错误数、耗时）+ 审计记录（谁、何时、哪个文件、批次号）。

### 9.3 校验规则（至少 15 条）

金额：各科目金额 >0；节点应付 = 基数 × 比例（±0.01）；节点合计 = 项目合同金额（尾差 ≤0.05 或登记为尾差）；已开票/已付 ≤ 应付。
比例：4 节点合计 = 100%（±0.01）；单比例 0~100%。
日期：试运行 ≤ 验收；质保到期 = 验收 + 1095 天；日期可解析。
结构：项目编号唯一；科目名不重复；状态枚举合法；税率格式合法；空项目/空科目报错。

### 9.4 双轨核对与回退

1. 停机窗口（建议 2~4 小时）内：冻结 Excel 编辑 → 全量备份 → `--dry-run` 两轮 → 业务确认差异报告 → `--commit`；
2. 用新增的 `export_excel.py` 导出系统台账（项目/科目/节点/已开票/已付/状态），与旧表逐项目比对金额与状态，双方签字；
3. 双轨 1~2 周：系统为唯一写入源，旧 Excel 只读；发现缺口用"补录 + 审计"修正；
4. 停表归档（只读备份 + 打印/PDF 存档）；
5. 回退预案：导入前快照 + `--rollback` 批次删除；极端情况整体恢复备份。

### 9.5 工作量与风险

- 导入脚本 + 校验：8~12 人时；导出/核对：4~6 人时；数据清洗与两轮试跑：4~8 人时（取决于表质量）；合计约 **16~26 人时**。
- 风险：Excel 口径与系统不一致（尤其"已付款"与发票/凭证）、合并单元格/多 sheet、金额尾差、历史状态无法精确还原、旧表存在手工调整无记录。

---

## 10. 操作留痕（审计）

### 10.1 现状：完全没有

- 无审计表；`payment_records` 有 `operator_id/name`、`payment_applications` 有 `applicant_id/name`，但：
  - 谁**修改**了金额/比例/日期、谁**删除**了项目/节点、谁**回改**了合同 → 无记录；
  - 项目删除是物理删除，删完无法追查；
  - 无登录成功/失败记录，无法排查账号异常；
  - 无字段级 before/after，金额被改多少不可知。
- 政务/财务场景通常要求"操作可追溯、留痕不可篡改、保留期 ≥3 年"，按现状**不满足**。

### 10.2 设计建议（P0 最小版）

```sql
-- 只追加，不更新不删除；页面只读；导出受限
CREATE TABLE audit_logs (
  id            INTEGER PRIMARY KEY,
  occurred_at   DATETIME NOT NULL,
  actor_id      INTEGER,              -- 可为空（登录失败时）
  actor_name    VARCHAR(64),          -- 快照
  actor_role    VARCHAR(16),          -- 快照
  action        VARCHAR(48) NOT NULL, -- login.success / project.create / node.pay ...
  entity_type   VARCHAR(32),          -- project / item / node / contract / user
  entity_id     INTEGER,
  project_id    INTEGER,              -- 便于按项目检索
  before_json   TEXT,                 -- 关注字段快照（金额/状态/日期/比例）
  after_json    TEXT,
  ip            VARCHAR(45),
  user_agent    VARCHAR(256),
  request_id    VARCHAR(36),
  result        VARCHAR(16) DEFAULT 'ok',   -- ok / denied / failed
  error_msg     VARCHAR(512)
);
CREATE INDEX ix_audit_time    ON audit_logs(occurred_at);
CREATE INDEX ix_audit_actor   ON audit_logs(actor_id, occurred_at);
CREATE INDEX ix_audit_project ON audit_logs(project_id, occurred_at);
```

- **留痕字段选择**：金额类必须记 before/after（应付、已开票、已付、比例、计算基数）；状态变更记前后状态；删除记完整快照（JSON）；
- **实现方式**：阶段 1 用统一 helper（`audit.record(...)`）在写操作里调用；阶段 2 服务层重构后收口到 service，避免漏埋；
- **不可篡改**：应用账号无 `DELETE` 权限（SQLite 层面不好做，至少在应用层禁止 + 数据目录 ACL + 备份留存）；建议每日把审计表导出到只追加文件（JSONL）留档；
- **查询页面**：管理员可见，按时间/操作人/项目/动作筛选，可分页，可导出 CSV；保留期 ≥3 年；
- **与业务台账区分**：`payment_records` 是"财务事实"，`audit_logs` 是"操作证据"，两者都要。

### 10.3 最小埋点清单（阶段 1）

登录成功/失败、登出、改密；用户创建/停用/角色变更；项目创建/编辑/状态变更/删除（软删）；科目新增/修改；节点申请/发票/付款/撤销；试运行/验收登记；合同上传/确认/回改/重识别；Excel 导入/导出；报表导出（如有）。

---

## 11. 待用户决策清单

> 标记 **【阻断】** 的项不确认就无法完成阶段 1。

1. **【阻断】历史数据源与口径**：请提供现行《付款进度表》Excel 原件（含所有 sheet 与隐藏列）。历史"已付款"是否需要导入为可追溯的付款记录（含日期/凭证号/发票号）？还是只需回填节点汇总金额？——决定导入方案 A/B/C 与报表口径。
2. **【阻断】合规与部署**：是否有等保/内控/审计留存年限要求？是否要求国产数据库（达梦/人大金仓）或必须 MSSQL？部署机器配置、并发人数、是否按部门隔离数据？合同原文是否属于敏感信息、是否允许发外部大模型？——决定数据库选型、权限模型、LLM 默认开关、审计保留。
3. **金额尾差口径**：4 节点按比例四舍五入后与合同金额差几分钱时，尾差归到质保金、单列"尾差"、还是允许合计不等？
4. **改价与回改权限**：谁有权修改合同金额/比例、回改已生成项目的节点？是否要求"申请—审批—付款"分离（当前同一 operator 可一条龙操作）？
5. **部分付款与撤销**：实际业务是否存在部分付款、跨月付款、付款撤销/冲销？当前节点状态"付款即置已付款"，部分付款语义不完整。
6. **只读角色可见范围**：viewer 是否可以查看合同原文与金额（当前可以）？
7. **历史账号与权限**：上线后保留哪些账号/角色？是否需要 AD/域账号登录？
8. **双轨期长度**：旧 Excel 与新系统并行多久、由谁核对签字？

---

## 12. 分阶段完善路线图

### 12.1 优先级定义

| 级别 | 定义 | 处理阶段 |
| --- | --- | --- |
| **P0** | 不改就不能给真实用户用的项：登录/会话安全、密码与默认账号、CSRF、越权、数据准确性、审计留痕、备份与恢复、部署可重复、异常处理；以及"没有它就无法替换 Excel"的数据迁移能力 | 阶段 0~1 |
| **P1** | 不阻断上线，但会在 1~3 个月内造成维护/性能/合规问题：分层重构、索引与约束、上传与 OCR 稳健性、软删除、权限细化、单测与 CI、报表性能、日志监控、数据库抽象 | 阶段 2 |
| **P2** | 业务增强与体验：撤销/冲销、审批流、附件、导出打印、提醒、合同要素扩展、配置化、SSO | 阶段 3 |

**排序原则**：先"不出事"（安全/数据/备份），再"能迁移"（Excel 导入与双轨核对），最后才是功能与重构。阶段 1 刻意不碰大重构，避免上线前引入新风险。

### 12.2 阶段总览

| 阶段 | 名称 | 优先级 | 估算 | 结束标志 |
| --- | --- | --- | --- | --- |
| 阶段 0 | 接管与基线（1~2 天） | P0 | 8~12 人时 | 有 git 历史、有全量备份、基线跑通 |
| 阶段 1 | **可安全上线最小闭环**（1.5~2.5 周） | P0 | 48~68 人时 | 安全/金额/审计/备份/部署/导入六项验收通过 |
| 阶段 2 | 稳定与可维护（2~2.5 周） | P1 | 74~98 人时 | 分层完成、单测≥60%、并发 10 用户无写锁错误 |
| 阶段 3 | 业务增强（1~1.5 周） | P2 | 40~60 人时 | 业务方 UAT 签字 |

### 12.3 阶段 0：接管与基线（P0，8~12 人时）

| # | 任务 | 改动 | 人时 | 验收 |
| --- | --- | --- | --- | --- |
| 0.1 | 版本控制 | `git init` + `.gitignore`（排除 `instance/`、`*.db`、`uploads/`、`__pycache__`）+ 初始提交 + 打 tag | 2 | 有历史、可回滚 |
| 0.2 | 现状全量备份 | db + uploads + 代码快照，放到项目外目录，记录恢复步骤 | 2 | 恢复演练一次成功 |
| 0.3 | 依赖锁定与基线 | `requirements.txt` 精确版本（当前环境实测：Flask 3.1.3 / SQLAlchemy 2.1.5 / Flask-SQLAlchemy 3.1.1）；记录 PyMuPDF 许可结论（内部使用） | 2 | 干净环境可复现安装 |
| 0.4 | 跑通现状 e2e 并记录基线 | 在副本库/临时端口跑 `e2e_test.py`，记录通过项与已知失败 | 2 | 基线报告入 docs |
| 0.5 | 部署调研 | 确认服务器、账号、网络、备份介质、是否有等保要求 | 2~4 | 部署方案签字 |

### 12.4 阶段 1：可安全上线最小闭环（P0，48~68 人时）

> 目标：**不新增业务功能**，只把"能上线"的六件事做扎实，并用 Excel 导入完成数据切换。

#### 1.1 安全基线（12~16 人时）

| # | 改动 | 人时 |
| --- | --- | --- |
| 1 | `SECRET_KEY` 必须来自环境变量/配置文件，缺失或等于默认值拒绝启动；`SESSION_COOKIE_SECURE`（HTTPS 时）、`SAMESITE=Lax`、会话有效期（如 8 小时）| 3 |
| 2 | 首次登录/使用默认密码强制改密；新增"修改密码"；密码策略（≥8 位、复杂度、不可与用户名相同）；管理员可重置 | 4 |
| 3 | 登录失败限速（如 5 次/5 分钟锁定 15 分钟）+ 成功/失败审计 | 3 |
| 4 | 全站 CSRF（Flask-WTF `CSRFProtect`），登出改 POST，所有表单加 token | 3 |
| 5 | 统一错误处理：404/403/413/500 中文友好页；DEBUG 关闭；启动日志打印版本/配置摘要（不含密钥） | 2 |
| 6 | 关闭示例账号/弱口令；上线前清点账号 | 1 |

#### 1.2 数据准确性（12~16 人时）

| # | 改动 | 人时 |
| --- | --- | --- |
| 1 | `money.py`：全链路 Decimal + ROUND_HALF_UP；去掉金额计算中的 `float()`；模板格式化兼容 Decimal | 4 |
| 2 | 服务端强校验：比例 0<r≤1、合计 100%±0.01、金额范围（Numeric(14,2) 上限）、日期先后；合同确认、项目/科目编辑、节点操作全部走同一校验 | 4 |
| 3 | 合同回改：改为**就地更新**节点（按 node_type/item 匹配）或对已有申请/付款的项目禁止回改（提示走变更/冲销）；不再物理删除节点 | 3 |
| 4 | 派生字段幂等：清空验收日期时清空质保到期/质保金 due_date；试运行清空时清 trigger_date | 1 |
| 5 | 节点汇总与明细一致性：写操作事务内重算 `paid/invoice/status`（或改为查询汇总）；新增 `scripts/check_consistency.py` 自检并修复种子数据 | 3 |
| 6 | 金额尾差：按用户确认口径实现 + 校验测试 | 1 |

#### 1.3 审计与权限最小版（8~12 人时）

| # | 改动 | 人时 |
| --- | --- | --- |
| 1 | `audit_logs` 表 + `audit.record()` helper + 第 10.3 节埋点（P0 子集：登录、项目增删改、节点申请/发票/付款、合同确认/回改、用户管理、导入） | 5 |
| 2 | 管理员"操作日志"页：筛选（时间/人/项目/动作）+ 分页 + CSV 导出；只读 | 3 |
| 3 | 项目/节点软删除（`deleted_at`）+ 删除时写审计；用户改"停用"而非删除；列表默认过滤已删除 | 3 |
| 4 | 合同原文/合同列表访问按角色收敛（按用户确认结论） | 1 |

#### 1.4 部署与备份（8~12 人时）

| # | 改动 | 人时 |
| --- | --- | --- |
| 1 | `config.py` + 环境变量（`DATA_DIR/DATABASE_URL/MAX_UPLOAD_MB/SESSION_LIFETIME/LLM_ENABLED`）；数据目录外置（`C:\ProgramData\...`） | 3 |
| 2 | Waitress + NSSM 服务化；日志轮转；`/healthz`；启动自检（目录可写、密钥已改、DB 可读） | 4 |
| 3 | `scripts/backup.ps1`（SQLite 在线备份 + uploads + 完整性校验 + 保留策略）+ 计划任务 + 失败告警 | 3 |
| 4 | `docs/deploy.md` + `docs/backup-restore.md`（含恢复演练步骤、升级/回滚流程） | 2 |
| 5 | IIS/nginx HTTPS（单位证书）+ 防火墙限制 + 数据目录 ACL | 2 |

#### 1.5 迁移上线（8~12 人时）

| # | 改动 | 人时 |
| --- | --- | --- |
| 1 | `scripts/import_excel.py`（dry-run/commit/批次/回滚/校验报告） | 8 |
| 2 | `scripts/export_excel.py`（导出对账/报送） | 4 |
| 3 | 两轮试导入 + 差异报告 + 业务确认；双轨核对 1~2 周 | 4 |
| 4 | 操作手册 + 培训（1 次）+ 上线检查表 | 2 |

#### 阶段 1 验收清单（缺一不可）

- [ ] 用真实配置启动：无默认密钥、无默认账号、DEBUG 关闭、HTTPS 可访问
- [ ] 未登录/无权限/无 CSRF token 的写请求全部被拒，且有审计记录
- [ ] 比例合计 50%、负比例、超上限金额、日期倒挂 → 全部被拒并给中文提示
- [ ] 合同回改在"已有付款"时不会 500、不丢付款数据；无付款时节点 ID 不变
- [ ] 清空验收日期后质保到期日同步清空；一致性自检脚本 0 异常
- [ ] 审计日志可在页面按项目/人/时间查到金额变更前后值
- [ ] 备份任务连续 3 天成功、恢复演练成功（记录耗时）
- [ ] 服务重启/机器重启自动拉起；`/healthz` 正常
- [ ] Excel 导入后，项目数/科目数/节点金额/已开票/已付 与旧表逐一核对一致（差异有说明）
- [ ] 并发验证：一人上传扫描件期间，另一人能正常保存（不再 `database is locked`）

### 12.5 阶段 2：稳定与可维护（P1，74~98 人时）

| 块 | 内容 | 人时 |
| --- | --- | --- |
| 分层重构 | `routes` 拆 service；节点生成/金额口径/合同生成收敛为单一实现；统一事务边界（请求级 UoW，去掉 `sync_project_dates` 内部 commit）；配置与常量集中 | 16~20 |
| 并发与稳定性 | 上传/识别移出事务（先提交记录再识别，或后台线程/任务表）；OCR 页数上限与总超时；临时文件唯一化；CWD 无关的绝对路径（修 `rerun` 的路径 bug）；写锁重试 | 8~10 |
| 数据层 | 外键索引；CHECK 约束；统一级联策略；Alembic 迁移；合同元数据结构化字段；`Unicode` 类型（为 MSSQL 预留） | 10~14 |
| 上传安全 | magic bytes 校验、单文件大小、失败清理、原文件预览/下载（受权限控制）、存储配额 | 6~8 |
| 测试与 CI | pytest 单测（money/engine/校验）+ 集成测试（test_client）+ 权限矩阵 + e2e 冒烟改造 + CI | 12~16 |
| 报表性能 | SQL 聚合替代内存聚合；分页；按状态筛选；导出 | 6~8 |
| 可观测 | 结构化日志（request_id/user）、慢请求、错误上报、每日巡检脚本 | 4~6 |
| 权限细化 | 部门/项目范围（若需要）、金额变更权限、只读范围 | 6~8 |
| 数据库抽象 | `DATABASE_URL`、去 sqlite3 测试耦合、MSSQL 迁移演练（不正式切换） | 6~8 |

### 12.6 阶段 3：业务增强（P2，40~60 人时）

- 撤销/冲销（替代物理删除）：误登记付款的冲销单、节点状态回退、审计留痕；
- 部分付款与多笔付款语义明确化（当前"付一次即已付款"）；
- 审批流（申请 → 审批 → 付款分离），按金额阈值分级；
- 发票/付款凭证附件上传与预览；合同附件管理；
- 提醒：质保到期、待付款、超期未付，页面 + 导出；可选邮件/企业微信；
- 合同要素扩展（合同编号、甲乙方、签订日期、发票信息）；识别规则增强（数字与大写金额互相校验、多页表格）；
- 报表导出 PDF/Excel、打印模板；数据字典与节点比例模板配置化；
- SSO/AD 域账号登录评估；操作日志长期归档与检索。

### 12.7 风险登记表

| 风险 | 概率 | 影响 | 应对 |
| --- | --- | --- | --- |
| 合同回改丢付款数据/500（已实测） | 高 | 高（财务数据） | 阶段 1 必修；上线前数据快照 + 双轨核对 |
| 无 git/无备份，改造不可逆 | 高 | 高 | 阶段 0 先做，再动代码 |
| Excel 口径与系统不一致（历史已付/发票/尾差） | 高 | 中高 | dry-run + 差异报告 + 业务签字；方案 A/B/C 由用户定 |
| SQLite 多用户写锁 | 中 | 中 | 阶段 1 修长事务 + 重试 + 单进程；>20 并发评估换库 |
| 并发付款丢更新/500 | 中 | 高 | 明细汇总 + 事务重算 + 乐观锁 |
| OCR 语言包缺失/识别率低（扫描件） | 中 | 中 | 保留人工确认；失败提示清晰；可选 LLM（默认关） |
| LLM 外发泄密/提示注入 | 中 | 高 | 默认关闭 + 开关审计 + 输出校验 + 单位审批 |
| PyMuPDF AGPL 合规 | 低 | 中 | 内部使用确认；对外分发前法务评估或换库 |
| 政务国产化/等保要求 | 中 | 高 | 尽早确认；阶段 2 完成 DB 抽象预留 |
| 用户习惯与双轨负担 | 中 | 中 | 培训 + 操作手册 + 双轨期明确责任人 |
| 依赖未锁定导致升级破坏 | 中 | 中 | 阶段 0 锁定 + 阶段 2 CI |

---

## 13. 附录

### 13.1 实测证据清单

| # | 验证内容 | 方法 | 结果 |
| --- | --- | --- | --- |
| 1 | 合同回改（项目已有申请+付款） | test_client POST `/contracts/<id>/confirm` | `IntegrityError: FOREIGN KEY constraint failed`（500），事务回滚 |
| 2 | 比例合计 50% | 同上，ratio 10/10/10/20 | 302 成功生成项目，节点应付合计 50,000 vs 合同 100,000 |
| 3 | 负比例 | ratio_first = -10 | 302 成功，节点 `ratio=-0.1000`、`due_amount=-10,000.00` |
| 4 | 清空验收日期 | POST `/projects/<id>/edit`，acceptance 置空 | `acceptance_date=None`，`warranty_due_date` 仍为 2029-03-09 |
| 5 | 删除有业务数据的用户 | POST `/admin/users/2/delete` | `IntegrityError: FOREIGN KEY constraint failed`（500） |
| 6 | 合同重新识别路径 | 进程 CWD 切到项目外，POST `/contracts/<id>/rerun` | 文件实际存在但提示"合同文件已丢失"；CWD 切回项目根后成功 → 路径依赖 CWD |
| 7 | 上传体积上限 | 上传 17MB txt | HTTP 413（英文默认错误页）；README 写 20MB |
| 8 | SQLite 写阻塞 | 连接 A 未提交写入 + 连接 B `busy_timeout=5000` 写入 | B 等待 5.0s 后 `database is locked` |
| 9 | 会话/密钥配置 | 打印 `create_app()` 默认配置 | `SECRET_KEY` 为硬编码默认值；`SESSION_COOKIE_SAMESITE=None`、`SECURE=False`；`MAX_CONTENT_LENGTH=16MB` |
| 10 | viewer 可见范围 | viewer 登录后 GET | `/contracts` 200、`/contracts/<id>/raw` 200（合同全文） |
| 11 | 索引情况 | 读 `sqlite_master` | 仅 3 个唯一索引，所有外键列无索引 |
| 12 | 种子数据一致性 | 只读查询现有实例库 | 进度款 3 个节点 `paid_amount` 合计 85,800、状态"已申请"，但 `payment_applications`/`payment_records` 均为 0 条 |
| 13 | 金额存储类型 | `typeof(due_amount)` | `integer`/`real`（SQLite 不强制 NUMERIC 精度） |
| 14 | CSRF/日志/错误处理 | 全仓库 grep | 无 csrf、无 logging、无 errorhandler；`jsonify`/`to_dict` 为死代码 |

### 13.2 关键代码位置索引

| 主题 | 位置 |
| --- | --- |
| 应用工厂 / SQLite PRAGMA / 上传上限 | `app/__init__.py:44-78` |
| 默认密钥 | `app/__init__.py:48` |
| 认证与角色装饰器 | `app/routes.py:21-55`、`88-108` |
| 项目/科目/节点创建 | `app/routes.py:140-266` |
| 项目删除（手写级联） | `app/routes.py:269-291` |
| 节点申请/发票/付款 | `app/routes.py:294-374` |
| 验收/试运行联动 | `app/routes.py:69-83`、`377-418` |
| 合同上传（事务跨 OCR） | `app/routes.py:496-540` |
| 合同确认/回改（删节点） | `app/routes.py:557-654` |
| 合同重识别（CWD 路径） | `app/routes.py:662-693` |
| 报表内存聚合 | `app/routes.py:707-737` |
| 用户管理 | `app/routes.py:742-787` |
| 金额模型与 recalc | `app/models.py:75-141` |
| 级联关系 | `app/models.py:47-49,85,118,159` |
| OCR 子进程/临时文件 | `app/contract_engine.py:251-284,318-338` |
| LLM 外发 | `app/contract_engine.py:343-405` |
| 规则识别 | `app/contract_engine.py:408-460` |
| 种子数据不一致 | `scripts/init_db.py:30-62` |

### 13.3 本次评审未做的事

- 未修改任何业务代码、模板、依赖或业务数据库；
- 未做性能压测（仅做了写锁机制验证）；
- 未做安全渗透测试（仅代码审查 + 关键点验证）；
- 未评估浏览器兼容性、打印样式、无障碍；
- 未拿到真实 Excel 原件，迁移字段映射为反推假设；
- 未确认单位等保/国产化/审计规范要求（列入待决策）。

---

**一句话结论**：这是一份"功能完整、口径清楚"的原型，业务逻辑值得保留；但上线前必须补上 **安全基线（密钥/密码/会话/CSRF）、数据准确性（比例与金额校验、回改不丢数据、派生字段一致）、审计留痕、备份恢复、可重复部署** 五块地基，并用 Excel 导入完成历史数据切换。按阶段 0~1 约 **56~80 人时** 即可达到"内部真实可用"的标准，之后再做分层重构与业务增强。

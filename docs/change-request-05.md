# CR-05 设计：截图式合同识别 / 可配置质保期 / 申请单号与多笔付款 / 会话令牌失效修复 / 节点字段改名

- 版本：v1.4（2026-10-27，用户确认 Q1–Q10 全 A（13:27）；方案 B 获 Chief 有条件追认；T2c 已实现）
- 基线：设计起点 `e6f67d5`；最新 HEAD `4ff9fb1`，干净快照回归 **323 项全绿**
- 作者：架构师
- 状态：T1~T7 全部已提交（T2b `61fa95d`、T6 `a8af7ea`）；T2c `6186918` 已实现（TTL 可配默认 30、动态提示、清理 `expires_at < now`）；Q1–Q10 用户已确认全 A；方案 B 已追认；剩余为测试收口与文档对齐
- 涉及需求：CR-05 六条（合同识别截图化、质保期可配置、申请单号必填+去说明、一申请多付款、会话失效 bug、节点字段改名）

---

## 0. 结论速览

| 需求 | 是否动数据结构 | 核心改动 | 难度 |
|---|---|---|---|
| ① 截图式合同识别 | **是**（新增 `contract_screenshots` 表 + `contract_files.source` 列 + 图片读取路由） | 三张截图上传→分类型 OCR→合并识别→确认页带图核对；项目详情可看图 | 中 |
| ② 质保期不写死 | **是**（`projects.warranty_months` 列，回填 36） | 质保期按月输入/识别，日期按「验收日 + N 月」推算 | 小 |
| ③ 申请单号必填、取消说明 | 不改表结构（`apply_no` 列不变，`description` 列保留但停用） | 申请表单加单号必填、删说明；导出/模板/测试同步 | 小 |
| ④ 一申请多付款 | 不改表结构（`payment_records.application_id` 早已存在且可空） | 新增申请单列表 + 「增加付款」路由与累计校验 | 中 |
| ⑤ 「操作已失效」bug | **是**（新增 `node_action_tokens` 消费记录表；令牌本身不落库） | 令牌移出 Cookie 会话，改为 `itsdangerous` 签名串，仅消费时登记 `jti` 防重放；Cookie 体积恒定（实测 226 B） | 中 |
| ⑥ 节点字段改名 | 不改结构（纯派生展示） | 已申请→付款、已开票→发票、删已付款列；`nodes.py` 新增两个派生函数 | 小 |

**第 5 条根因已实测确认**（§8.1），不是页面缓存问题：详情页每次渲染把 3×节点数个一次性 token 追加进浏览器 Cookie 会话，12 节点项目第 4 次刷新 Cookie 就达 4590 字节，超过浏览器 4093 字节上限被静默丢弃；页面 HTML 里的新 token 与浏览器实际保存的旧会话对不上，于是永远「操作已失效」，刷新只会继续追加、永远修不好。修完 Cookie 体积恒定，刷新即可恢复。

> **实现状态（v1.4）**：T1 `d1d3005`、T2 `a2f4a09`、T3 `73f8832`、T4 `68b08a5`、T5 `c7ef62f`、T7 `79c333d`、T2b `61fa95d`（先校验后消费）、T6 `a8af7ea`（三截图上传/OCR/详情展示）、T2c `6186918`（TTL 可配默认 30 分钟、动态提示、清理 `expires_at < now`）均已提交；最新测试基线 `4ff9fb1` 为 **323/323 全绿**。Q1–Q10 用户已于 2026-10-27 13:27 确认全部按 A，无改动。
>
> 第⑤条实际落地为「无状态签名令牌 + jti 消费记录」（下称方案 B），与 Chief m01253 原定方案 A（服务端签发 `token` 行 + `used_at` 原子消费）不同；**Chief 已于 2026-10-27 有条件追认**（TTL 30 分钟可配、提示语动态、清理条件 `expires_at < now`，见 §8.3），无需回退方案 A（Q1–Q10 的 A 结论不受影响）。

---

## 1. 现状核对（需求①、②的判断依据）

| 能力 | 现状（代码位置） | CR-05 结论 |
|---|---|---|
| 合同上传 | `app/routes.py:1183 contract_upload()`，单文件 `request.files['file']`，`ALLOWED_CONTRACT_EXTS` 白名单（pdf/docx/txt/md/img） | 需改多文件三槽位；旧入口是否保留见 Q1 |
| 图片 OCR | `app/contract_engine.py:extract_text()` 图片走 Windows WinRT OCR（`scripts/ocr_win.ps1`，先复制到系统 TEMP 再识别）；扫描 PDF 由 PyMuPDF 渲染后 OCR | **可直接复用，不引新依赖** |
| 识别规则 | `recognize_by_rules(text)` 抽金额/税率/付款方式/4 节点比例；`recognize_with_llm(text)` 走 OpenAI 兼容接口 | 规则/LLM 都按整段文本工作，需加「按截图类型分段」与提示词 |
| 上传件回显 | 全库只有 Excel/CSV 导出用 `send_file`（`routes.py:2118/2134/2157`），**没有任何路由能读回上传的合同文件/图片** | **必须新增鉴权图片路由**，否则「项目详情直接看到图片」无法实现 |
| 质保期 | `routes.py:44 WARRANTY_DAYS = 365*3` 写死；`routes.py:271 sync_project_dates()` 用「验收日 + WARRANTY_DAYS」算 `project.warranty_due_date` 与质保金节点 `due_date`；`PaymentNode.node_type` 含 `warranty` | **必须加可配置质保期字段**，否则不同项目无法不同 |
| 节点账本 | `payment_nodes` 的 `applied_amount` / `invoice_amount` / `paid_amount` 三本账已齐（CR-04），`payment_records.application_id` 可空、`payment_invoices` 表已有 | 需求①②⑥都不需要改 `payment_nodes` |
| 申请单 | `PaymentApplication.apply_no` 唯一、`make_apply_no()` 自动生成（`routes.py:172`）、`description` 说明列 | 改为人工必填单号、说明停用 |
| 申请单列表 | **当前页面没有任何申请单列表**，`PaymentApplication` 只写不展示 | 需求④必须先补列表，否则「增加付款」无处可点 |

---

## 2. 数据模型变更

### 2.1 新增表 `contract_screenshots`（需求①）

一次「截图识别」= 一条 `contract_files` 识别任务（`file_type='img'`、`source='shots'`）+ 2~3 条截图明细。

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `contract_id` | INTEGER NOT NULL FK `contract_files.id` | 所属识别任务 |
| `project_id` | INTEGER NULL FK `projects.id` | 确认生成项目后回填（与 `payment_invoices` 同款做法，便于项目详情直接查） |
| `shot_type` | VARCHAR(16) NOT NULL | `amount` / `payment` / `warranty` |
| `file_name` | VARCHAR(256) NOT NULL | 原始文件名 |
| `stored_path` | VARCHAR(512) NOT NULL | 相对 `UPLOAD_DIR`，如 `uploads/shots/<contract_id>/amount-<uuid>.png`（沿用 `contract_storage_path()` 的相对路径约定） |
| `mime` | VARCHAR(64) NULL | `image/png` 等 |
| `file_size` | INTEGER NULL | 字节 |
| `ocr_text` | TEXT NULL | 该张截图 OCR 文本（便于复核「识别来源」） |
| `ocr_status` | VARCHAR(16) NOT NULL DEFAULT 'pending' | `pending` / `ok` / `failed` / `skipped` |
| `ocr_msg` | VARCHAR(255) NULL | OCR 失败原因 |
| `created_by` | INTEGER NULL | |
| `created_at` | DATETIME | |

- 索引：`(contract_id)`、`(project_id, shot_type)`。
- 一个识别任务内每种 `shot_type` 最多一条（重传即替换）。

### 2.2 新增表 `node_action_tokens`（需求⑤：jti 消费记录，方案 B）

> 方案史：Chief m01253 原定方案 A（服务端签发 `token` 行、`used_at` 原子消费、TTL 30 分钟）；实际落地改为方案 B（无状态签名令牌 + 只登记消费），已由 `a2f4a09` 实现、测试验收（当前干净快照 323 项全绿、Cookie 恒定 226 B）。**Chief 已于 2026-10-27 有条件追认方案 B**，三项条件已由 T2c `6186918` 落地，见 §8.3。

| 列 | 类型 | 说明 |
|---|---|---|
| `jti` | VARCHAR(64) PK | 令牌 payload 里的 `j`（`secrets.token_urlsafe(16)[:22]`）；主键冲突 = 同一令牌已提交过 |
| `node_id` | INTEGER NOT NULL | 绑定的节点 |
| `action` | VARCHAR(16) NOT NULL | `apply` / `invoice` / `pay` |
| `session_sid` | VARCHAR(64) NULL | 消费时的登录会话 sid（仅供排查） |
| `expires_at` | DATETIME NOT NULL | 令牌过期时间（签发时刻 + `NODE_TOKEN_TTL_MINUTES`，默认 30 分钟） |
| `consumed_at` | DATETIME NOT NULL | 消费时间 |
| `created_at` | DATETIME NULL | 写入时间（UTC） |

- 索引：仅 `(expires_at)`；消费时顺手 `DELETE WHERE expires_at < now - 1 天`（`app/routes.py:440-446`），表规模恒定在「近一天消费量」。
- **令牌本身不落库、也不写 session**：`itsdangerous.URLSafeTimedSerializer(SECRET_KEY, salt='payment-progress.node-action')` 签 `{n: node_id, a: action, s: sid 摘要前 8 位, j: jti, e: 过期时间戳}`，模板隐藏字段 `confirm_token` 原样携带。令牌不可伪造、不可枚举，登出/改密后因 `sid` 摘要不匹配而自动失效。
- 本表只是「已用 jti」的登记簿，不承担业务审计职责（审计仍走 `audit_logs`）。

#### 2.2.1 DDL（迁移 0005 使用，SQLite / MSSQL 均可执行）

```sql
CREATE TABLE node_action_tokens (
    jti         VARCHAR(64) NOT NULL,
    node_id     INTEGER     NOT NULL,
    action      VARCHAR(16) NOT NULL,
    session_sid VARCHAR(64) NULL,
    expires_at  DATETIME    NOT NULL,
    consumed_at DATETIME    NOT NULL,
    created_at  DATETIME    NULL,
    CONSTRAINT pk_node_action_tokens PRIMARY KEY (jti)
);
CREATE INDEX ix_node_action_tokens_expires_at ON node_action_tokens (expires_at);
```

- SQLAlchemy 模型：`app/models.py:347 class NodeActionToken`，字段与上表一一对应，`jti` 为主键。
- 不建 `node_id` 外键：行按 TTL 清理，不参与引用完整性；不建 `(node_id)` 索引（无按节点查询的路径）。
- 旧库由 `app/schema.py` 的「缺表按 metadata 补建」自动建表（`e6f67d5` 已修过同类旧库缺表问题）。

#### 2.2.2 签发/消费时序（T2b 已修，`61fa95d`）

1. GET `/projects/<id>` — `_issue_node_tokens(project)`（`app/routes.py:399`）：为每个节点 × 3 动作现签令牌，**渲染零写库**；同时 `session.pop('node_tokens')` 清理 CR-04 遗留在旧会话里的令牌表。TTL 由配置 `NODE_TOKEN_TTL_MINUTES` 决定（默认 **30 分钟**，见下）。
2. POST `/nodes/<id>/update` / `/applications/<id>/payments` — `_consume_node_token()`（`app/routes.py:416`）：`loads(max_age=NODE_TOKEN_TTL)` 验签并验过期 → 比对 `n`/`a` → 比对 `s` 与当前会话 sid 摘要 → 登记 `jti`；`NODE_TOKEN_TTL = NODE_TOKEN_TTL_MINUTES * 60`，禁止再写死 2 小时。
3. ✅ **T2b 已实现（`61fa95d`）**：路由把 `validate_node_action()` 前移到 `_consume_node_token()` 之前（`routes.py:997` 校验、`:1018` 消费），业务校验失败不再烧令牌，同一页面可直接改金额重试，§8.4.2 第 1 条成立。**残留风险**：消费仍由 `_consume_node_token()` 内部单独 `commit()`，若校验通过后业务 CAS 失败（并发/金额被他人改动），令牌仍已被消耗；彻底同事务需再拆 `_verify`/`_record`，暂列后续事项，不影响 Q1–Q10 结论。

4. **TTL 与清理（用户卡片确认④，设计口径）**：新增配置 `NODE_TOKEN_TTL_MINUTES`（`config.py` / 环境变量，默认 **30**，范围 1–1440）；签发时写 `expires_at = now + minutes`，验签用同一值，提示语中的“页面停留超过 N 分钟”由该配置动态渲染，**不得出现硬编码“2 小时”**。`GET` 详情页清理条件为 `DELETE ... WHERE expires_at < now`（只清已过期，不再用 `now - 1 天`）。
5. **T2c 已实现（`6186918`）**：`routes.py:379-398` 读 `current_app.config['NODE_TOKEN_TTL_MINUTES']`，缺省 `NODE_TOKEN_TTL = 30 * 60`；提示语按配置动态渲染；GET 清理条件改为 `NodeActionToken.expires_at < now`。文档口径与实现一致，无需再改代码。

### 2.3 列变更

| 表 | 列 | 变更 | 迁移动作 |
|---|---|---|---|
| `projects` | `warranty_months` | 新增 INTEGER NULL | `ALTER TABLE`；历史行回填 36（保持现有质保到期日不变） |
| `contract_files` | `source` | 新增 VARCHAR(16) NOT NULL DEFAULT `'file'` | `ALTER TABLE`；历史行默认 `file` |
| `contract_files` | `file_type` | 无需改列，新增取值 `img` 已存在；`source` 区分「截图任务」 | — |
| `payment_applications` | `apply_no` | 语义改人工填写（列不变，仍唯一） | 无需 DDL |
| `payment_applications` | `description` | **停用**（保留列与历史值，不写入、不展示、不导出） | 无需 DDL，见 §6.3 |

### 2.4 明确不改的部分

- `payment_nodes` 三维金额与 `status` 缓存：不改（需求⑥只改展示）。
- `payment_records` / `payment_invoices`：不改（需求④靠已有 `application_id` 打通）。
- 任何比例的默认值：继续保持 CR-04 口径「无默认、必填、合计 100%」。

---

## 3. 迁移 0005 与旧库兼容

> 项目尚未引入 Alembic（阶段 2 才做），**0005 是逻辑迁移编号**，不是 Alembic revision；结构升级由 `app/schema.py:upgrade_schema()` 幂等执行，数据回填/备份由 `scripts/migrate_cr05.py` 执行。若后续接入 Alembic，再把本编号落成 `0005_cr05.py`。

### 3.1 迁移 0005 内容

| 步骤 | 操作 | 失败处理 |
|---|---|---|
| 1 | 备份数据库（`scripts/migrate_cr05.py` 复制到 `<数据目录上级>/backups/pre-cr05/` 并做 `PRAGMA integrity_check`） | 备份或校验失败 → 中止，库不动 |
| 2 | `ALTER TABLE projects ADD COLUMN warranty_months INTEGER NULL` | 列已存在则跳过（幂等） |
| 3 | `ALTER TABLE contract_files ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'file'` | 列已存在则跳过；历史行默认 `file` |
| 4 | 建 `contract_screenshots` 表及索引（§2.1） | 表已存在则跳过（`db.metadata.sorted_tables` 建表） |
| 5 | 建 `node_action_tokens` 表及索引（§2.2.1） | 表已存在则跳过 |
| 6 | 回填 `UPDATE projects SET warranty_months = 36 WHERE warranty_months IS NULL` | 事务内执行；不重算 `warranty_due_date` |
| 7 | 自检：`/healthz` 200、`/projects` 200、随机项目详情 200、一笔节点操作成功、旧项目 `warranty_due_date` 与迁移前一致 | 任一项失败 → 回滚备份并记录 |

### 3.2 幂等与验证 SQL

```sql
-- 结构核对（SQLite；MSSQL 查 INFORMATION_SCHEMA）
SELECT name FROM sqlite_master WHERE type='table'
  AND name IN ('contract_screenshots','node_action_tokens');
PRAGMA table_info(projects);          -- 含 warranty_months
PRAGMA table_info(contract_files);    -- 含 source
-- 回填核对
SELECT COUNT(*) FROM projects WHERE warranty_months IS NULL;  -- 期望 0
-- 旧项目质保到期日不应被迁移改写
SELECT id, acceptance_date, warranty_due_date, warranty_months FROM projects
 WHERE acceptance_date IS NOT NULL LIMIT 20;
```

### 3.3 回滚

- 全部为**增量列/增量表**，旧代码不读新列即可回滚：先停服务，用 `backups/pre-cr05/*.bak` 覆盖数据库（或仅 `DROP TABLE` 两张新表并保留新列，旧代码不受影响）；
- 迁移脚本不删任何旧表/旧列，回滚不需要反向 DDL；
- 回滚后必须重跑 `/healthz` 与冒烟，且确认旧项目质保到期日未变。

### 3.4 兼容注意

- `node_action_tokens` 的 `jti` 是 22 字符随机串，令牌由 `SECRET_KEY` 签名、不入库；旧代码不识别本表，不影响登录态（`SECRET_KEY` 轮换会使所有未用令牌失效，属预期）；
- `warranty_months` 为空的历史项目（若未回填）按“不推算”处理，不会写死 36；
- 备份/恢复流程（`scripts/backup.ps1`）无需改动，但备份会包含两张新表，`docs/deploy-drill.md` 需补一句“恢复后抽查新表存在”。

---

## 4. 需求①：按模板上传关键信息截图

### 4.1 上传页（`app/templates/contracts.html`）

上传卡片由单文件 input 改为三个槽位（可预览缩略图后再提交）：

| 槽位 | 字段名 | 必传 | 用途 |
|---|---|---|---|
| ① 合同金额截图 | `shot_amount` | **是** | 识别合同总额、税率 |
| ② 付款条件/付款方式截图 | `shot_payment` | **是** | 识别付款方式、4 节点比例 |
| ③ 维保/质保金条款截图 | `shot_warranty` | 否 | 识别质保期、质保金比例 |

- `accept=".jpg,.jpeg,.png,.bmp,.webp"`，单张 ≤ 10MB（`MAX_SHOT_BYTES`），服务端再校验一次。
- ①② 缺失 → 表单错误提示，不建识别任务、不落库。
- **Q1=A 兜底**：保留原「上传整份合同文件（PDF/Word/txt/Markdown/图片）」入口，折叠在页面下方次要卡片（`<details>` 或「兜底」区），提交仍走 `contract_upload()` 单文件流程；主流程只走三截图，不删除旧数据与旧路由（实现 `a8af7ea`，`contracts.html:35-38`）。

### 4.2 存储

- 目录：`<UPLOAD_DIR>/shots/<contract_id>/<shot_type>-<uuid8>.<ext>`（先建 `ContractFile` 拿 id 再存，与现有「先建行再落盘」一致）。
- DB 存相对路径，读取统一走 `contract_storage_path()` 同类函数 `screenshot_storage_path(shot)`，兼容历史绝对路径。

### 4.3 识别流程（`app/contract_engine.py` + `app/routes.py`）

1. 逐张 `extract_text(path, 'img')` → 写 `ocr_text` / `ocr_status`；
2. 合并文本，加分隔标记后交给识别引擎（规则引擎按现有正则；LLM 提示词明确「三张截图分别是什么」）：
   ```
   === 截图①：合同金额 ===
   ...
   === 截图②：付款条件/付款方式 ===
   ...
   === 截图③：维保/质保金条款 ===
   ```
3. **分类型定向抽取**（新增函数，规则引擎优先、LLM 兜底，识别不到就留空）：

   | 截图 | 抽取内容 | 落地函数 |
   |---|---|---|
   | ① amount | 合同总额、税率 | 复用 `extract_amount` / `extract_tax_rate` |
   | ② payment | 付款方式、4 节点比例 | 复用 `extract_payment_method` / `extract_ratios` |
   | ③ warranty | **质保期（月）**、质保金比例 | 新增 `extract_warranty_months()`（见 §5.2） |

4. `result_json` 扩展：`shots[]`（type/ocr_status/摘要）、`warranty_months`、逐张 `warnings`；
5. `contract_confirm()` 照旧跳转，确认页顶部展示三张截图缩略图 + 每张 OCR 摘要，方便与填写值对照。

### 4.4 映射规则（冲突处理，对应 Q4）

- 三张截图各管一段：金额只认①，比例/付款方式只认②，质保期/质保金比例认③（③缺失时质保金比例可手填）；
- ② 识别出的比例合计 ≠ 100% → 不套默认值、确认页标红并阻断提交（沿用 CR-04 口径）；
- 识别为空 → 对应字段留空、必填项提示手填，不写任何系统默认比例。

### 4.5 项目详情展示（`app/templates/project_detail.html`）

- 新增「合同截图」卡片（放在项目信息卡下方）：
  - 3 个分组（合同金额 / 付款条件 / 维保条款），每组横排缩略图（`<img>` 高 160px），点击新窗口看原图；
  - 空态：「未上传该类型截图」；
  - 只显示**已确认生成项目**的截图（`contract_screenshots.project_id = project.id`）。
- 新路由：`GET /contracts/shots/<int:shot_id>/image` → `send_file(..., mimetype=shot.mime)`（`inline`）。
  - 权限：`login_required` + `role_required('admin','operator')`；viewer 按 D5「不可查看合同原文」同样不可看图（Q9）。
  - 缺失文件 → 404 + 中文提示（不抛 500）。

### 4.6 修改/重新识别

- 「重新识别」沿用 `contract_rerun`，改为对已存截图重跑 OCR；支持重传单张（替换该 `shot_type` 的旧图）。
- 合同修改（`contract_confirm` 的 update 模式）不改动截图；截图归属跟随 `ContractFile`。

### 4.7 识别失败 / 低置信度：人工兜底与审计埋点

#### 4.7.1 失败分级与页面行为

| 场景 | 系统行为 | 用户可做的操作 |
|---|---|---|
| 单张 OCR 失败（图片损坏/格式不支持/WinRT 不可用） | 识别任务仍创建（`ContractFile.status='recognized'`），该 `contract_screenshots.ocr_status='failed'`，`ocr_msg` 存原因；不阻断确认页 | 重传该类型截图 / 重跑 OCR / 直接手工填写字段 |
| 字段未识别或低置信度 | 字段值**不自动填入**（输入框留空 + 提示“识别建议 X，低置信度请核对”）；确认页可编辑 | 手工填写；比例合计校验在提交时执行 |
| ①② 截图 OCR 均失败 | 仍允许确认，但必须勾选「以上内容为人工录入，已对照原图核对」`manual_confirm=1` | 勾选后提交，审计记录人工兜底 |
| ③ 未上传或识别失败 | 质保期/质保金比例留空，按 Q2/Q4 由用户手填；不写默认值 | 手填或不填（质保期空=不推算） |
| 三张截图全部失败 | 同“①② 均失败”，确认页显示三张失败缩略图与原因 | 手工完整录入 + 勾选人工确认 |

#### 4.7.2 置信度判定（写入 `result_json`，不引入新依赖）

| 等级 | 判定条件（任一满足；按低→高优先级取最差） | 处理 |
|---|---|---|
| `high` | 规则正则精确命中 + 通过一致性校验（金额与合计、比例合计≈100%、日期可解析） | 自动预填 |
| `medium` | 仅单一引擎命中（规则或 LLM），格式可解析，但未通过交叉校验 | 自动预填 + “请核对”黄色标记 |
| `low` | 未命中 / 多引擎冲突 / 比例合计明显≠100% / 数值越界 | 不预填，只给建议；必须人工输入 |
| `failed` | OCR 失败或无文本 | 同 `low`，并增加失败原因提示 |

`result_json` 增加结构（示例）：

```json
{
  "engine": "rules",
  "fields": {
    "contract_amount": {"value": "120000.00", "confidence": "high", "source": "rules"},
    "tax_rate": {"value": "9", "confidence": "medium", "source": "rules"},
    "payment_method": {"value": "银行转账", "confidence": "high", "source": "rules"},
    "warranty_months": {"value": null, "confidence": "low", "source": ""}
  },
  "ratios": {
    "first": {"value": 0.30, "confidence": "high", "source": "rules"},
    "progress": {"value": 0.52, "confidence": "medium", "source": "llm"}
  },
  "shots": [{"shot_type": "amount", "ocr_status": "ok", "ocr_engine": "winrt", "text_len": 128}],
  "warnings": ["比例合计 97%，请人工核对"],
  "manual_fallback": false
}
```

#### 4.7.3 重试与重传

- `contract_rerun` 只重跑 `ocr_status in ('failed','pending')` 的截图；已成功的不重复 OCR；
- 单张重传：替换同 `shot_type` 的旧行与文件，保留其他两张；
- 重跑/重传后回到确认页，**已手工填写的字段优先保留**（会话/表单暂存），识别值只在字段为空时填入，避免覆盖人工输入；
- 所有重跑/重传动作写审计（见 4.7.4）。

#### 4.7.4 审计埋点（沿用 `app/audit.py:audit_record`，与业务同事务）

| 事件 | 触发 | 关键字段 |
|---|---|---|
| `contract.shots.upload` | 三截图上传/单张重传后 | `contract_id`、`shot_types`、各张 `file_size`、`mime` |
| `contract.shot.ocr` | 每张 OCR 结束 | `contract_id`、`shot_type`、`ocr_status`、`engine`、`text_len`、`error_code`（不存原文） |
| `contract.recognize.low_confidence` | 识别完成且存在 low/failed 字段 | `contract_id`、`fields`（字段名/confidence/source）、`warnings` |
| `contract.recognize.manual_override` | 确认页提交时检测到人工值与识别值不同 | `contract_id`、`changed_fields`、`before`/`after`（只存金额/比例等结构化值，不存截图内容） |
| `contract.confirm`（已有，扩展） | 确认生成/回改项目 | 增加 `manual_fallback`、`low_confidence_fields`、`shot_types` |
| `contract.shot.image_access` | 受保护图片路由被访问 | `shot_id`、`user_id`、`result`（200/403/404）；仅记录异常访问（403/404）以避免日志量过大 |

- 审计 `actor` 一律取当前登录用户，禁止从表单接收；
- 不记录完整 OCR 文本、图片二进制、文件绝对路径；`text_len` 与 `error_code` 足够排障；
- 确认页提交成功后 `manual_fallback=1` 会进入审计与项目备注（`project.remark` 追加“合同识别为人工兜底，已核对原图”），便于日后追溯。

#### 4.7.5 验收（测试）

1. 上传损坏的 ③ 图：任务创建成功、`ocr_status='failed'`、确认页无 500、可手工填质保期并确认；
2. ①② 均 OCR 失败：未勾选 `manual_confirm` 时提交被拒；勾选后可提交，审计出现 `contract.confirm.manual_fallback=true`；
3. 低置信度比例：字段不预填、提示建议值；手工填 4 个比例后提交成功，审计含 `low_confidence` 与 `manual_override`；
4. 重跑 OCR 不覆盖已手工填写的字段；
5. 图片路由：viewer 403、未登录 302、不存在 404、正常 200 且 `Content-Type` 正确；
6. 审计事件中不出现 OCR 原文/绝对路径/完整令牌。

## 5. 需求②：质保期可配置

### 5.1 字段与表单

- `projects.warranty_months`：整数，1–600（月）。
- 输入位置：`project_form.html`（新建/编辑项目）与 `contract_confirm.html`（确认识别结果）各加一个「质保期（月）」输入框。
- 显示：项目信息卡「质保到期」旁加「质保期 36 个月」。
- 校验：>0 且 ≤600 的整数；允许空（空 = 不推算，不写死默认），见 Q2。

### 5.2 识别（`extract_warranty_months()`，`contract_engine.py`）

支持写法：`质保期3年`、`质保 36 个月`、`保修期两年`、`质量保证期为24个月`、`质保金3%`（比例另走 `extract_ratios`）。
- 年 → ×12；中文数字（一/二/两/三…十二）做小映射；
- 取第一个命中值，写入 `result_json.warranty_months` 并预填确认页；
- 识别不到 → 留空 + 确认页提示「未识别到质保期，请手工填写」（不套 36）。

### 5.3 日期推算

- `routes.py:271 sync_project_dates()` 改为：`warranty_due = acceptance_date + warranty_months 个月`（新增 `timeutil.add_months()`，无 `dateutil` 依赖：按目标月最后一天对齐，如 1/31 + 1 月 = 2/28）。
- 质保金节点 `due_date` 同源；
- `WARRANTY_DAYS = 365*3` 常量删除（或保留为历史备注常量不再使用）；
- 已登记验收但未填质保期的项目：`warranty_due_date` 保持空并提示补填（不静默写 36）。

### 5.4 与 CR-04 合同变更的联动（必须实现，含测试）

CR-04 的「合同变更」只改金额，CR-05 的质保期是**日期口径**，两者通过 `warranty_months` 与 `sync_project_dates()` 解耦：

| 场景 | 行为 |
|---|---|
| 新建项目 / 合同首次确认 | `_create_contract_project()` 写入 `project.warranty_months = submission.warranty_months`，再 `sync_project_dates()`；质保到期日 = 验收日 + N 月（验收日为空则不填） |
| 合同重新确认/回改（`_apply_contract_values()`） | 先把 `project.warranty_months` 更新为本次确认值，再 `sync_project_dates()`；若改小/改大，项目级 `warranty_due_date` 与未发生业务的质保金节点 `due_date` 跟着变，已发生业务的节点日期按现有 `has_business` 规则冻结并回显差异 |
| CR-04 合同变更（`_apply_contract_change()`） | **不修改** `warranty_months`、`warranty_due_date`、任何节点 `due_date`；只改科目金额与未付节点 `due_amount`。质保金节点的到期日仍由验收日 + 质保期决定 |
| 合同变更后再登记验收 | `sync_project_dates()` 按新 `warranty_months` 重算项目质保到期与质保金节点日期；已付款的质保金节点日期冻结，模板提示“日期已冻结，金额已变更” |
| 项目编辑页修改质保期 | 保存后调用 `sync_project_dates()`；若验收日为空，清空 `warranty_due_date`（不写死 36） |
| 历史项目（迁移 0005） | 回填 `warranty_months=36`，但**不重算** `warranty_due_date`，保持旧日期；之后编辑验收日/质保期才按新口径联动 |

实现要点：
- `sync_project_dates()` 中 `warranty_due = add_months(acceptance, project.warranty_months) if acceptance and project.warranty_months else None`，删除 `WARRANTY_DAYS`；
- `_PROJECT_AUDIT_FIELDS` 增加 `warranty_months`，项目编辑/合同确认的审计能体现质保期变化；
- 合同变更审计 `project.contract_change` 增加 `before/after.warranty_months`（未变则相同），并明确 `due_dates_untouched: true`；
- 报表/导出增加「质保期（月）」列（项目 sheet），导出不重新推算日期。

测试点：
1. 验收日 2026-01-31、质保期 1 个月 → 到期 2026-02-28（闰年 2028-02-29）；
2. 合同变更改金额后，`warranty_months`、`warranty_due_date`、质保金节点 `due_date` 均不变；
3. 合同确认回改质保期 36→24，未发生业务的质保金节点日期随验收日重算；已付款质保金节点日期冻结且回显差异；
4. 质保期为空 + 验收日已填 → `warranty_due_date` 为空、页面提示补填，不出现 36 默认。

---

## 6. 需求③：付款申请单号必填、取消说明

### 6.1 表单与路由

- `project_detail.html` 申请块改为：`[单号 input name="apply_no" required] [金额] [提交]`，删掉 `name="description"` 输入；
- `_commit_application(node, amount, apply_no, actor, before_snapshot)`：不再调用 `make_apply_no()`；
- 校验（`validation.validate_node_action` 或路由层）：
  - 必填、`strip()` 后非空、长度 ≤64、允许字母数字与 `-`/`_`/`/`（Q5：是否限制字符集）；
  - 全局唯一：写入前 `PaymentApplication.query.filter_by(apply_no=...)` 查一次，重复直接提示「申请单号 X 已存在」；`IntegrityError` 兜底同样转中文提示；
- 审计 `node.apply` 的 `after` 里已有 `apply_no`，保持。

### 6.2 说明字段的移除范围（已核对全库引用）

| 位置 | 处理 |
|---|---|
| `routes.py:179,199`（`_commit_application`） | 去掉 `description` 参数与赋值 |
| `routes.py:896-897`（`node_update` apply 分支） | 不再读 `description` |
| `templates/project_detail.html:196` | 删除说明输入框，替换为单号输入框 |
| `exporter.py:42`（`APPLICATION_HEADERS` 含 '说明'） | 删除该列 |
| `exporter.py:251`（`app.description`） | 该行删除 |
| `models.py` `PaymentApplication.description` | **保留列**（历史值不丢，回滚安全），注释标 `deprecated`，代码不再读写 |
| `tests/qa_http_cases.py` | 相关提交表单去掉 description；新增「缺单号被拒」「重复单号被拒」用例 |

### 6.3 历史数据

- 历史申请单的自动生成单号（`AP...`）原样保留，可正常展示/导出；
- 历史 `description` 值保留在库中，不再出现在页面/导出/报表。

---

## 7. 需求④：一张申请对应多笔付款

### 7.1 数据

- 无 DDL：`payment_records.application_id`（可空）已在 CR-04 预留；
- 申请单的「已付金额」= `SUM(payment_records.paid_amount WHERE application_id = 申请id)`（动态算，不新增缓存列，避免再引入一致性问题）。

### 7.2 新路由 `POST /applications/<int:app_id>/payments`

- 权限：`admin` / `operator`，且项目可写（复用 `_guard_writable_project`）；
- 表单：`paid_amount`（必填 >0）、`pay_date`、`pay_method`、`voucher_no`、`confirm_token`（复用该节点 `action='pay'` 的签名令牌，与节点付款按钮共用；一次页面刷新内任一付款路径成功即消耗，防双击）；
- 校验（原子 CAS，先更新节点 `paid_amount` 再插明细，失败回滚）：
  1. 本笔 >0；
  2. **该申请关联付款累计 + 本笔 ≤ 申请金额**（建议开启，Q6）；
  3. 节点 `paid_amount + 本笔 ≤ due_amount`（**必须**，沿用现有口径）；
  4. 凭证号重复 → 强提示不阻断（沿用现有做法）；
- 成功后写审计 `application.pay`，`node.status` 用 `derive_status()` 刷新；
- 「无申请直接付款」的节点付款入口**保留**（CR-04 口径），两条路径都受节点应付上限约束。

### 7.3 页面（项目详情新增「付款申请单」卡）

表格列：`申请单号 | 科目 | 节点 | 申请金额 | 已付金额 | 剩余可付 | 付款笔数 | 申请时间 | 操作`。

- 操作：`[增加付款]`（打开行内表单，金额预填「剩余可付」）、`[付款明细]`（展开 `payment_records` 列表）；
- 已付满 → 按钮置灰，提示「已达申请金额」；
- 只读角色 / 已归档项目 → 只展示、无按钮（与现有权限一致）。

### 7.4 报表与导出

- 申请单 sheet（`exporter.py APPLICATION_HEADERS`）：在删除「说明」的同时新增「已付金额」「付款笔数」两列；
- 节点报表不变（仍按节点三维汇总，允许「付款 > 申请」，CR-04 已允许无申请付款）。

---

## 8. 需求⑤：「操作已失效……刷新后没反应」根因与修复

### 8.1 根因（已实测，非推测）

用测试框架的 `LiveServer` + `requests` 造 3 科目 = 12 节点的项目，登录后连续 GET `/projects/<id>`，记录 `session` Cookie 长度：

| 第 N 次打开详情页 | Cookie 长度 | 浏览器行为 |
|---|---|---|
| 1 | 1384 B | 正常 |
| 2 | 2420 B | 正常 |
| 3 | 3471 B | 正常 |
| 4 | 4511 B | **静默丢弃**（Set-Cookie 4590 B > 4093 B 上限） |
| 5 | 5562 B | 丢弃 |
| 6 | 6634 B | 丢弃 |

Werkzeug 同步告警原文：

```
The 'session' cookie is too large: the value was 4511 bytes but the header required 79 extra bytes.
The final size was 4590 bytes but the limit is 4093 bytes. Browsers may silently ignore cookies larger than this.
```

### 8.2 失效链条（为什么刷新修不好）

1. `_issue_node_tokens(project)`（`routes.py:368-395`）每次渲染详情页，为**每个节点的每个操作**新生成 `secrets.token_urlsafe(18)`，追加进 `session['node_tokens']`，只增不减（>600 才裁剪）→ 每刷一次 +约 1.1KB（12 节点）；
2. Flask 默认**客户端签名 Cookie 会话**：整个 session 全量写进 `Set-Cookie`；
3. 第 4 次渲染起 Cookie 超 4093 B → 浏览器静默保留旧 Cookie；
4. 此时页面 HTML 里是服务器刚签发的新 token（T4），浏览器 Cookie 里是旧会话（没有 T4）→ 提交时 `_consume_node_token` 找不到 → 提示「操作已失效」（`routes.py:887`）；
5. 刷新 = 再签发 T5 并继续追加，Set-Cookie 依旧超限被丢弃 → **永远对不上，刷新无效**；
6. 回归 266 项全绿是因为 `requests` 的 cookiejar 不限制单个 Cookie 大小，只有真实浏览器才触发。

**次生问题（T2b 已部分修复）**：原 `routes.py:985` 先 `_consume_node_token()` 再 `validate_node_action()`，业务校验失败也会烧令牌；`61fa95d` 已把校验前移（先校验后消费），校验失败不再烧。残留：消费仍在 `_consume_node_token()` 内单独 commit，校验通过后若业务 CAS 失败（并发/金额被改），令牌仍被消耗，需重开详情页。

### 8.3 修复方案（实际落地：方案 B，无状态签名令牌 + jti 消费记录）

> **Chief 已追认方案 B（2026-10-27）**，附条件：① TTL 改为可配置 `NODE_TOKEN_TTL_MINUTES`，默认 **30 分钟**，不得写死 2 小时；② 提示语按实际配置值动态生成；③ `jti` 清理条件为 `expires_at < now`。三项已由 T2c `6186918` 落地，本次只改文档，不改代码；方案 A 不再要求回退。

| 项 | 方案 B（现状实现） |
|---|---|
| 令牌存储 | 从 session 移除 `node_tokens`；令牌为 `itsdangerous` 签名串，**不入库、不写 session**；`node_action_tokens` 只记已使用的 `jti`。Cookie 只剩用户/CSRF，实测恒定 **226 B**（12 节点项目连续刷新不增长） |
| 签发 | `_issue_node_tokens(project)`（`routes.py:399`）返回 `{node_id: {action: token}}`，渲染零写库；payload `{n,a,s,j,e}`，TTL 由 `NODE_TOKEN_TTL_MINUTES`（默认 30 分钟，`6186918` 已配）计算 |
| 消费 | `_consume_node_token()`（`routes.py:416`）验签 + 验过期 + 比对 node/action + 比对会话 sid 摘要 → `INSERT node_action_tokens(jti)`；主键冲突 = 已提交 |
| 顺序 | T2b（`61fa95d`）已把 `validate_node_action()` 前移到消费之前，校验失败不烧令牌；消费仍单独 commit，业务 CAS 失败时令牌已被消耗（残留，见 §2.2.2） |
| 错误提示 | `_flash_node_token_failure()`（`routes.py:958`）：已提交「该操作已提交（请不要重复点击），请刷新页面查看最新状态」；过期「操作令牌已过期（页面停留超过 N 分钟），请刷新页面后重试」，N 由当前 `NODE_TOKEN_TTL_MINUTES` 动态渲染（`6186918` 已实现）；无效「操作已失效（页面已过期或链接无效），请刷新页面后重试」 |
| 日志 | 现状**没有**分场景失败日志（只 flash）；可选补：记录 node_id/action/jti 前 6 位，不打印完整令牌 |
| 兼容 | 详情页渲染时 `session.pop('node_tokens', None)` 清理 CR-04 遗留令牌表（`routes.py:401`）；CR-04 的 600 条截断逻辑随旧实现一并删除 |
| CSRF | 业务成功后不再轮换 CSRF；只在登录成功后轮换（`routes.py:525-530`），多标签页表单不再互相失效 |
| 为何不用 A | ① 渲染零写库：12 节点项目每次打开详情页少 36 行 INSERT + 1 行 DELETE，GET 不产生写锁；② 令牌表只存已消费 jti，规模恒定、无「按 TTL 堆积」风险；③ 令牌自带过期与 sid 绑定，会话失效即连带失效。代价：依赖 `SECRET_KEY`（轮换会使未用令牌失效）、无服务端主动吊销（靠默认 30 分钟可配 TTL + 会话绑定） |

### 8.4 验收用例（交给测试）

#### 8.4.1 六条必过（Chief 指定，缺一不可上线）

| # | 用例 | 断言 |
|---|---|---|
| T-8.4-1 | 24 节点项目首次打开详情页 | 响应 `Set-Cookie` 中 session 值 < 4000 B（用 LiveServer 真实 Cookie，不用 requests 无限制 cookiejar） |
| T-8.4-2 | 连续打开详情页 10 次 | 第 10 次 session Cookie 长度 ≤ 第 1 次 + 200 B（不随节点数累积）；无 Werkzeug “cookie is too large” 告警 |
| T-8.4-3 | 同一 token 第二次提交 | 第一次成功；第二次被拒（提示“已使用”），节点 `applied_amount/invoice_amount/paid_amount` 不变，申请/发票/付款明细行数不增 |
| T-8.4-4 | 二次提交的 DB 副作用 | 同一 token 并发/顺序提交两次，业务表只产生一次副作用（唯一申请单/发票/付款各 1 行，金额只加一次），`node_action_tokens` 只多出 1 行 `jti` |
| T-8.4-5 | 过期令牌（注入 1 秒 TTL） | 测试用 `NODE_TOKEN_TTL_MINUTES` 可覆盖为 1 秒（或直接构造 `e` 为过去的签名令牌）；等待 >1 秒后提交 → 被拒并提示“已过期”，业务表无副作用 |
| T-8.4-6 | jti 清理条件 | 详情页 GET 只删除 `expires_at < now` 的 `node_action_tokens` 行；未过期（含 1 秒前刚签发）的行必须保留，清理后总行数=本页节点数×3（若同页已消费则加上历史未过期行） |

#### 8.4.2 补充用例

1. 业务校验失败（超应付）后，同一 token 仍可提交合法金额（令牌未被烧）——**T2b（`61fa95d`）已实现**；注意消费仍单独提交，CAS 失败需重开详情页；
2. 过期 token、错 node/action、跨会话（他人）token：均被拒；`jti` 已存在时报「已提交」，提示语与场景对应；
3. 并发双击：一只成功、一只失败，无重复副作用；
4. **bug 主诉场景**：反复刷新详情页 10 次后再提交申请/开票/付款 → 全部成功；
5. 原有回归全绿（当前 323 项；含 token 语义用例 `qa_http_cases.py:1575-1588` 已按方案 B 重写）；
6. 迁移后旧会话里的 `node_tokens` 被清理：首次请求 Cookie 体积回落，旧 token 无法再使用。

---

## 9. 需求⑥：节点字段改名与派生值

### 9.1 页面字段对照

| 现状（`project_detail.html:169-171`） | 目标 |
|---|---|
| 列「已申请」= `d.apply_label` | 列「**付款**」= 派生值 + 金额小字 |
| 列「已开票」= `d.invoice_label` | 列「**发票**」= 派生值 + 金额小字 |
| 列「已付款」= `d.pay_label` | **删除该列**（并入「付款」列） |

「付款」列取值（按优先级自动判断）：`已付款`（paid≥due） / `部分付款`（paid>0） / `已申请`（applied>0） / `未申请`（其余）。
「发票」列取值：`已开票`（invoice>0） / `未开票`（否则）；部分开票金额在列内小字展示「¥A / ¥B」，标签仍按用户口径只给两值（Q8）。

### 9.2 `app/nodes.py` 新增派生函数

```python
def payment_state(due, applied, paid):
    """付款列：未申请 / 已申请 / 部分付款 / 已付款（展示用，永不参与校验）"""
def invoice_state(due, invoice):
    """发票列：未开票 / 已开票"""
```

- 保留 `derive_status()` / `FLOW_STATUS` 不变（报表、待办提醒、旧筛选仍读它，避免连锁改动）；
- `three_dim()` 增加返回 `payment_state` / `invoice_state` 两个字段，模板直接用。

### 9.3 其它同步位置

- `project_detail.html:161` 分组汇总文案「已申请/已开票/已支付」→「申请/开票/付款」；
- `reports.html:38` 表头「已申请/已开票/已付金额」→「申请金额/开票金额/付款金额」，并新增「付款状态」「发票状态」两列（Q8=A，报表直接给状态列，不再用“或”）；
- `exporter.py`：`NODE_HEADERS` 金额列保留（`已申请金额/已开票金额/已支付金额/未付金额`），新增「付款状态」「发票状态」两列；`REPORT_HEADERS` 同步；
- 待办提醒（`routes.py:2061-2070`）仍按 `n.status in ('未开票已申请','已申请')`，本次不改（后续可切到「申请>0 且 付款<应付」）。

---

## 10. 接口清单

| 方法 | 路径 | 变更 | 权限 |
|---|---|---|---|
| POST | `/contracts/shots` | 新增：三截图上传 + 识别 | admin/operator |
| GET | `/contracts/shots/<int:shot_id>/image` | 新增：按权限回显图片（inline） | admin/operator |
| POST | `/contracts/<int:contract_id>/rerun` | 改：重跑已存截图 OCR | admin/operator |
| POST | `/applications/<int:app_id>/payments` | 新增：申请单增加付款 | admin/operator |
| POST | `/nodes/<int:node_id>/update` | 改：apply 分支要求 `apply_no`、去 `description`（已实现 `73f8832`）；令牌改签名令牌 + `jti` 防重放（已实现 `a2f4a09`，T2b 调顺序） | admin/operator |
| GET | `/projects/<int:project_id>` | 改：合同截图卡 + 付款申请单卡 + 付款/发票列 | 登录用户 |
| GET/POST | `/projects/new`、`/projects/<int:project_id>/edit` | 改：质保期（月） | admin/operator |
| GET/POST | `/contracts`、`/contracts/<int:contract_id>/confirm` | 改：三截图表单、确认页带图 + 质保期 | admin/operator（viewer 只读） |
| GET | `/reports`、`/exports/*` | 改：字段名/新增列 | 登录用户 |

---

## 11. 实施顺序与任务拆分（@开发）

| 任务 | 内容 | 状态（commit） | 主要写区 |
|---|---|---|---|
| T1 | 数据模型 + 迁移（`models.py` / `schema.py` / `timeutil.add_months` / `scripts/migrate_cr05.py`） | **已完成** `d1d3005` | `app/models.py`、`app/schema.py`、`app/timeutil.py`、`scripts/` |
| T2 | **需求⑤ token 修复**（方案 B：签名令牌 + `jti` 防重放 + 提示 + 清理） | **已完成** `a2f4a09`（测试 `3c05dc4` 验收） | `app/routes.py` |
| T2b | 修「校验失败烧令牌」：拆 `_verify_node_token()` / `_record_node_token()`，路由改「验签→业务校验→登记 jti + 写明细→一次 commit」 | **已完成** `61fa95d` | `app/routes.py`、`tests/qa_http_cases.py` |
| T3 | 需求③ 单号必填 + 说明移除 | **已完成** `73f8832` | `app/routes.py`、`app/validation.py`、`templates/project_detail.html`、`app/exporter.py` |
| T4 | 需求④ 申请单列表 + 增加付款 | **已完成** `68b08a5` | `app/routes.py`、`templates/project_detail.html`、`app/validation.py` |
| T5 | 需求⑥ 派生字段改名（`payment_state` / `invoice_state`、删已付款列） | **已完成** `c7ef62f` | `app/nodes.py`、`templates/project_detail.html`、`app/exporter.py`、`templates/reports.html` |
| T6 | 需求① 三截图上传/分类型 OCR/确认页带图/项目详情看图/图片鉴权路由 | **已完成** `a8af7ea`；按 Q1=A（旧入口保留兜底）、Q4=A（分图取数、合计≠100% 阻断）、Q9=A（viewer 不可看）、Q10=A（`UPLOAD_DIR/shots/`、≤10MB 原图）实现 | `app/models.py`、`app/contract_engine.py`、`app/routes.py`、`app/security.py`、`templates/contracts.html`、`templates/contract_confirm.html`、`templates/project_detail.html` |
| T7 | 需求② 质保期字段与推算 | **已完成** `79c333d` | `app/routes.py`、`app/validation.py`、`templates/project_form.html`、`templates/contract_confirm.html`、`templates/project_detail.html` |

建议顺序：**T2b → T6 均已完成**；剩余为测试收口（截图用例并入后 323→最终基线）、`docs/qa-baseline.md` 更新与仓库清理。

---

## 12. 对测试基线的影响（@测试）

- 已改（随 `3c05dc4` 及 CR-05 补测）：
  - `qa_http_cases.py:1575-1588` token 语义 → 已按方案 B 重写（同一 token 二次提交 / 空 token / 刷新后可操作 / 防回退）；
  - `qa_http_cases.py:610`、`621`（`apply_no` 自动生成/唯一性）→ 已改人工单号（必填/字符集/重复阻断）；
  - `qa_http_cases.py:1378`（导出含三本账列）→ 已按「付款 / 发票」表头；
  - `qa_http_cases.py:427`、`492-497`（质保到期 = 验收 + 3 年）→ 已改按 `warranty_months` 推算；
  - `qa_common.py:171-204`（`NODE_FORM_RE` / `node_tokens` / `post` 自动补 token）→ 已适配新表单（单号输入框、无说明框）；
- 待补：截图上传（①②必传、分图识别映射、图片鉴权 403/200、viewer 403）、一申请多付款两道上限（申请单/节点）、质保月末对齐（如 1/31+1 月）的剩余用例；跨标签页（登录后不再轮换 CSRF）另立；
- 基线数量更新 `docs/qa-baseline.md`：CR-05 前 266 → 最新干净快照 **323**（`4ff9fb1`）；T6 截图用例已并入，后续以实际全绿数为准。

---

## 13. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| WinRT OCR 在无桌面/服务账号环境不可用 | 截图识别失败 | 与现有图片识别同一依赖；识别失败降级为手填，确认页保留全部可编辑字段并提示原因 |
| 截图只覆盖合同局部，可能识别不出质保金比例 | 比例不全 | 确认页手填（CR-04 已无默认值）+ 标红必填 |
| 需求④放开了「一张申请多笔付款」，会出现「付款 > 申请」 | 报表口径 | 节点上限仍强制；申请单列表显示「已付 / 申请」，超出时标红 |
| 令牌机制改动涉及安全面 | 越权/重放风险 | 现状：`SECRET_KEY` 签名 + 绑定 node/action + 会话 sid 摘要 + TTL 默认 30 分钟可配（T2c `6186918` 已配、清理 `expires_at < now`）+ `jti` 主键防重放；T2b 后「校验失败不烧令牌」；操作审计仍记录操作人 |
| 方案 B 偏离 Chief 方案 A | 决策不一致 | **已追认（2026-10-27）**，附条件 TTL 30 分钟可配、提示语动态、清理条件 `expires_at < now`（T2c 落地）；无需回退 |
| 模板结构大改 | 现有测试选择器失效 | 测试同步先行小改（`qa_common.py`），开发与测试约定表单 `name` 不随意改 |

---

## 14. 开放问题与拍板记录（用户已确认）

> 状态（v1.4）：**用户已于 2026-10-27 13:27 一次性确认 Q1–Q10 全部按建议 A，无改动**。下表“落地结论”记录实现/开工情况；“用户确认”为最终拍板记录，后续实施不得偏离。

| # | 问题 | 建议选项（推荐 A） | 落地结论 | 用户确认 |
|---|---|---|---|---|
| Q1 | 旧「上传合同文件（PDF/Word/txt）」入口是否保留？ | A 保留、折叠为兜底（推荐）；B 删除，只留三截图 | **A 已实现**（`a8af7ea`；`contracts.html:35-38` 兜底卡片） | **A**（2026-10-27 13:27） |
| Q2 | 质保期为空 / 历史回填？ | A 允许空不推算、历史回填 36 且不改日期（推荐）；B 禁止空；C 历史不回填 | **A 已实现**（`79c333d`） | **A**（2026-10-27 13:27） |
| Q3 | 质保期起点？ | A 验收日期（推荐）；B 合同固定到期日（需新增字段） | **A 已实现**（验收日 + N 月） | **A**（2026-10-27 13:27） |
| Q4 | 三图识别冲突 / 比例合计≠100%？ | A 分图取数、合计≠100% 阻断（推荐）；B 以金额图整体重算、容忍差额 | **A 已实现**（`a8af7ea`） | **A**（2026-10-27 13:27） |
| Q5 | 申请单号格式与唯一性？ | A 仅必填+全局唯一+≤64，不强制格式（推荐）；B 强制 `PAY-YYYY-NNNN` | **A 已实现 + 字符集**：必填、strip 后 ≤64、`^[A-Za-z0-9_/\-]{1,64}$`、全局唯一（`73f8832`） | **A**（2026-10-27 13:27） |
| Q6 | 一申请多付款上限？ | A 申请累计≤申请金额，保留无申请付款（推荐）；B 不限制申请累计 | **A 已实现且阻断**：申请单「已付+本笔≤申请金额」、节点「已付+本笔≤应付」；凭证号重复只提示（`68b08a5`） | **A**（2026-10-27 13:27） |
| Q7 | 令牌签发粒度？ | A 每节点×3 动作各一条（推荐）；B 每节点一条三动作共用；C 只按实际渲染表单签发 | **A 已实现**（每节点×3 签名令牌，渲染零写库） | **A**（2026-10-27 13:27） |
| Q8 | 节点展示与报表？ | A 付款四值+金额小字、发票两值+金额、报表加状态列（推荐）；B 只显示金额 | **A 已实现**（`c7ef62f`） | **A**（2026-10-27 13:27） |
| Q9 | viewer 能否看合同截图？ | A 不可看，与 D5 一致（推荐）；B 可看 | **A 已实现**（图片路由仅 admin/operator，viewer 403） | **A**（2026-10-27 13:27） |
| Q10 | 截图存储与大小？ | A `UPLOAD_DIR/shots/`、单张≤10MB、保留原图（推荐）；B 压缩后存储 | **A 已实现**：`UPLOAD_DIR/shots/<contract_id>/`、单张 ≤10MB、保留原图 | **A**（2026-10-27 13:27） |

结论已落到：§2 数据模型、§4 上传与展示、§5 质保期、§8 token、§9 展示、§11 任务依赖。

---

## 15. 实现对照与剩余工作

### 15.1 已提交实现（干净树可复跑）

| 任务 | commit | 内容 | 验证 |
|---|---|---|---|
| T1 | `d1d3005` | `ContractScreenshot`、`NodeActionToken`、`ContractFile.source`、`projects.warranty_months`、`timeutil.add_months`、`schema.py` 补列、`scripts/migrate_cr05.py` | 迁移幂等/旧库自检通过 |
| T2 | `a2f4a09` | 方案 B：签名令牌 + `jti` 消费记录；详情页渲染零写库；业务动作不再轮换 CSRF | Cookie 恒定 226 B，连续刷新不增长 |
| T3 | `73f8832` | 单号人工必填（校验/唯一/重复提示）、说明字段停用（模板/导出/校验） | 自测通过 |
| T4 | `68b08a5` | 项目详情「付款申请单」卡 + `POST /applications/<id>/payments` 增加付款；两道上限（申请单/节点） | 自测通过 |
| T5 | `c7ef62f` | `nodes.payment_state()` / `invoice_state()`；详情列改「付款/发票」、删「已付款」列；导出同步 | 自测通过 |
| T7 | `79c333d` | 质保期按月可配置（字段/校验/日期推算/确认页/导出）；空值不推算 | 自测 25/25 |
| T2b | `61fa95d` | 路由改为「先 `validate_node_action()` 后 `_consume_node_token()`」，校验失败不烧令牌（消费仍单独提交，CAS 失败残留见 §2.2.2） | 自测通过 |
| T6 | `a8af7ea` | 需求① 三截图上传、分类型 OCR、确认页带图、项目详情看图、鉴权图片路由；旧整文件入口保留为兜底 | 自测通过 |
| T2c | `6186918` | `NODE_TOKEN_TTL_MINUTES` 配置化（默认 30、范围 1–1440）、提示语动态、清理 `expires_at < now` | 测试基线 323 全绿 |
| 测试 | `3c05dc4`→`4ff9fb1` | 需求⑤ 用例转正 + CR-05 需求②③④⑥补测 + T6 截图用例 + T2c TTL 用例；干净快照双跑 **323 / 323 PASS / 0 FAIL / 0 XFAIL / 0 XPASS** | 退出码 0 |

### 15.2 剩余工作

- **T6 截图用例待并入**：①②必传、③可选、比例合计≠100% 阻断、识别失败手填、图片 403/200、viewer 403；与工作区未提交的 `tests/qa_db_cases.py` / `tests/qa_http_cases.py` 一起收口后跑最终基线。
- **文档收口**：`docs/qa-baseline.md` 由 266/270 更新到最终数；`docs/change-request-04.md` 附录 A 与本文 §2.2 再对齐一次（当前无未提交 diff）。
- **工作区清理**：`.qa-base2/`（临时目录，不入库）、`启动开发系统.cmd`（本地脚本，不入库）、`scripts/migrate_cr05.py`（未跟踪，需随 T6 入库）。
- **T2c 已完成**（`6186918`）：`NODE_TOKEN_TTL_MINUTES` 配置化（默认 30、范围 1–1440）；提示语按配置动态渲染；GET 清理条件 `expires_at < now`；§8.4.1 T-8.4-5/6 已可执行。
- **决策遗留**：无。第⑤条方案 B 已追认（2026-10-27 附三项条件），Q1–Q10 的 A 结论已回填 §14。

### 15.3 若方案 B 被撤回（回方案 A 清单，备查，当前不触发）

1. `app/models.py`：`NodeActionToken` 字段改为 `token PK / node_id / action / user_id / created_at / expires_at / used_at`，删 `jti / session_sid / consumed_at`；
2. `app/routes.py`：删 `itsdangerous` 导入与 `NODE_TOKEN_TTL / NODE_TOKEN_SALT / _node_token_serializer() / _session_digest()`；`_issue_node_tokens()` 改为「清过期 → 每节点×3 插行」；`_consume_node_token()` 改为 `UPDATE ... WHERE used_at IS NULL AND expires_at > now`，并保持「先校验后消费」；
3. 详情页 GET 变为写库路径（12 节点 36 行 INSERT + 1 DELETE），Cookie 用例改为「体积恒定」断言；
4. 测试改 4 条 token 语义用例与 TTL（30 分钟）；
5. 估算：开发 0.5 个任务 + 测试 0.5 个任务，需重跑 323 基线。

# CR-07 设计：新建流程节点勾选 / 识别并入新建项目 / 节点科目列修复 / 回收站彻底删除

| 项 | 内容 |
| --- | --- |
| 版本 | v1.1（2026-11-14，按 Chief 拍板口径修订 ②④） |
| 作者 | 架构师 |
| 依据 | Chief 群派单 CR-07 + 后续拍板（② 勾选固定节点类型、不做自定义名称；④ 输入项目名、有付款业务只允许归档） |
| 关联文档 | `docs/change-request-04.md`（付款口径/冻结）、`docs/change-request-05.md`（截图识别/令牌/质保期）、`docs/change-request-06.md`、`docs/qa-baseline.md`、`docs/qa-cr07-plan.md` |
| 代码基线 | `main` @ `1bebfb5`，回归 405 项全绿 |
| 状态 | 设计稿，待 Chief 对 §12 问题清单确认后进入开发 |

**v1.1 变更**：② 从「比例 0 = 不存在」升级为「新建时勾选固定节点类型」的交互口径（模型仍 0 迁移）；④ 二次确认改为**输入项目名称**，并增加「有申请/发票/付款任一 → 禁止彻底删除、只允许归档」；新增 Q1（同类型多节点/自定义名称）交用户确认。

---

## 0 结论速览

| 需求 | 设计结论 | 是否改数据模型 | 主要改动文件 |
| --- | --- | --- | --- |
| ② 节点数量按项目定义 | **新建时勾选固定 4 类节点**（首付/进度/验收/质保），未勾选的不生成、不展示；勾选项比例合计 100%；本期不做自定义名称/同类型多节点（Q1） | 否（0 迁移；沿用 `ratio`，不新增 `node_type` 取值） | `app/routes.py`、`app/validation.py`、`app/templates/project_form.html`、`app/templates/project_detail.html`、`app/exporter.py` |
| ① 识别并入新建项目 | 新建页内嵌「三截图上传→识别→回填」，**复用 `contract_files.source='shots'` + `project_id IS NULL` 作草稿**，保存成功后再绑定 | 否 | `app/routes.py`、`app/templates/project_form.html`、`app/templates/contracts.html`、`app/templates/base.html` |
| ③ 节点科目为空 | 已复现：模板引用 `PaymentNode` 上不存在的 `item_name`，Jinja 静默渲染空串；修 1 行（空则显示「—」）+ 4 条验收 | 否 | `app/templates/project_detail.html`（1 行） |
| ④ 回收站彻底删除 | `POST /projects/<id>/purge`：仅 admin、仅回收站项目、**输入项目名称**+勾选不可恢复、有申请/发票/付款任一即拒绝（只允许归档）、级联删 9 类数据+附件、审计 `project.purge` | 否 | `app/routes.py`、`app/templates/projects.html` |

- 数据影响：**0 个 schema 迁移**；新增 1 个脚本（草稿清理）+ 1 个可选配置（草稿保留天数）。
- 开发顺序：`T1`（③ 修复 + 空组不渲染）→ `T2`（② 勾选式节点）→ `T3`（① 新建页识别）→ `T4`（① 旧入口收敛 + 清理脚本）→ `T5`（④ 彻底删除）→ `T6/T7` 测试与文档。
- 基线预计：405 → **约 450–470**（① 约 20、② 约 14、③ 4、④ 约 18）。

---

## 1 现状核对

| 事实 | 位置 | 对本设计的影响 |
| --- | --- | --- |
| **节点类型固定 4 类**（`NODE_TYPES`/`NODE_TEMPLATES`），routes/validation/contract_engine/模板按类型硬编码 | `app/routes.py`（18 处引用）、`app/validation.py`（9 处）、`app/contract_engine.py`（9 处）、`app/templates/project_detail.html`（12 处）/`project_form.html`（9 处） | ② 本期**不动** `node_type` 语义，只做「勾选 + 过滤」；自定义名称/同类型多节点列入 Q1 |
| 新建项目页有 4 个必填比例输入（`ratio_*`，min 0） | `app/templates/project_form.html:64-67` | ② 改为「复选框 + 比例」；勾选才启用比例输入 |
| 新建项目按 4 类全量生成节点，尾差归质保金 | `app/routes.py:722 project_new`、`:695 _create_item_nodes` | ② 生成端只生成勾选集合；尾差在勾选节点间分摊 |
| 加科目也会按 4 类全量生成节点 | `app/routes.py:932 item_add` | ② 加科目必须沿用项目已有节点类型集合，不能新增类型 |
| 校验：比例允许 0、四项合计 100±0.01 | `app/validation.py:56/110/167` | ② 合计校验改为「勾选项内合计 100」；新增勾选校验 |
| 合同确认页固定 4 行比例输入 | `app/templates/contract_confirm.html:226` | ② 保留（0 = 不生成该节点），新建页与确认页口径一致 |
| 详情页节点表科目列引用不存在的属性 | `app/templates/project_detail.html:212` `{{ n.item_name }}`；`PaymentNode` 无该属性（`app/models.py:141`），只有 `node.item`（`:124`） | ③ 修复 + 测试断言 |
| 详情页节点按 4 组固定遍历，空组回退英文类型名 | `app/routes.py:798-810`、`project_detail.html:180/185` | ② 只渲染实际存在的组 |
| 导出按项目全部节点输出 | `app/exporter.py:213 _ordered_nodes` | ② 导出需过滤（节点集合已由生成端保证，过滤作兜底） |
| 截图识别已完整实现（必传①②、OCR、`recognize_shots`、`contract_shot_image` 鉴权） | `app/routes.py:1536/1554/1583/1673`、`app/contract_engine.py:606` | ① 复用识别能力，只改入口与落点 |
| 草稿可复用现成字段：`ContractFile.project_id` 可空、`source` 已有 `'shots'`、`ContractScreenshot.project_id` 可空 | `app/models.py:340/350/360/375` | ① 无需建模改动 |
| 回收站只有恢复，无物理删除；审计表无外键 | `app/templates/projects.html:99-109`、`app/routes.py:973/997`、`app/models.py:551` | ④ 新增 purge；审计快照可留痕 |

---

## 2 需求②：新建流程按项目勾选节点（本期口径）

### 2.1 口径与边界

- 节点类型保持固定 4 类：**首付款 / 进度款 / 验收款 / 质保金**（代码与历史数据都按这 4 类）。
- **新建项目时勾选本项目要用的类型**：勾选的生成节点，未勾选的**不存在**（不生成、详情不展示、报表/导出/提醒不出现）。
- 每类最多 1 个节点；同一类型多个「进度款」、自定义节点名称 → **本期不做**，列入 Q1 交用户确认（需改 `node_type` 语义 + 排序字段 + 历史兼容，属另一个量级）。
- 勾选类型的比例合计必须 100%（±0.01）；至少勾选 1 类；勾选类型的比例必须 > 0。
- 内部表达不变：未勾选类型不建节点；历史遗留的 0 比例节点视作「未勾选」，只隐藏不删。**0 schema 迁移**。
- 排序固定为 首付款 → 进度款 → 验收款 → 质保金（与历史一致，不新增 `seq` 字段）。

### 2.2 新建页交互（`project_form.html`）

```
付款节点：  [x] 首付款  比例[ 30 ]%      [x] 进度款  比例[ 70 ]%
            [ ] 验收款  比例[    ]%      [ ] 质保金  比例[    ]%
            合计 100%  |  节点金额预览：首付 ￥…  进度 ￥…（按科目原金额合计 × 比例）
提交字段： node_types=first&node_types=progress&ratio_first=30&ratio_progress=70
```

- 勾选后才启用对应比例输入；未勾选时比例输入禁用（不参与校验、不提交）。
- 前端实时显示勾选项合计；≠100% 行内红字提示，保存时服务端再校验。
- 勾选变化/比例变化时，节点金额预览同步（算法与合同确认页一致；服务端保存时以服务端结果为准）。
- 合同识别回填（§2.5）：识别到比例 > 0 的类型自动勾选并填比例，识别为空的类型不勾选（用户可手动勾）。
- 编辑项目（`project_edit`）不改节点配置；节点集合的后续变化只走合同回改/合同变更（§2.6）。

### 2.3 服务端校验与生成

| 位置 | 改动 |
| --- | --- |
| `app/validation.py` | 新增 `validate_node_selection(form)`：勾选集合非空；勾选类型比例齐全且均 > 0；勾选类型合计 100±0.01；未勾选类型忽略（即使误传值也不报错，避免旧表单兼容问题）。返回 `(node_types, ratios_pct, errors)` |
| `app/routes.py:722 project_new` | 用 `validate_node_selection` 取代「4 项固定合计」；把 `node_types` 传给 `_create_item_nodes` |
| `app/routes.py:695 _create_item_nodes(item, ratios, operator_id=None, node_types=None)` | 只生成 `node_types` 中的类型；`node_types=None` 时按全量（兼容内部调用/测试）；尾差 `allocate_tail` 只在已生成节点间分摊，保证「节点合计 = 科目总额」 |
| `app/routes.py:932 item_add` | 节点集合取**项目已有节点类型集合**（`{n.node_type for n in project.nodes if n.ratio > 0}`），不允许通过加科目新增类型；比例校验同样只对勾选集合 |
| `app/routes.py:1930 _create_contract_project` | 保持固定 4 行输入口径：金额/比例为 0 的类型不建节点（等价未勾选） |

### 2.4 展示 / 导出 / 报表（统一口径）

| 位置 | 改法 |
| --- | --- |
| `app/routes.py:798-810 project_detail` 的 `node_groups` | 只聚合项目实际存在且 `ratio > 0` 的节点；空组不进入 `node_groups` |
| `app/templates/project_detail.html:180/185` | 只渲染有节点的组；组头用 `node_name`（含比例），不再回退英文 `node_type` |
| `app/templates/contract_confirm.html:226` | 保留 4 行输入，行内提示「填 0 = 该项目没有此节点」 |
| `app/exporter.py:213 _ordered_nodes` | 过滤 `ratio <= 0`（兜底历史脏数据）；申请/付款明细随节点自然减少 |
| `app/routes.py:2619` 报表提醒 | 同一 helper 过滤（0 节点本无未付，统一口径） |
| `app/routes.py:2562 _reports_data` 汇总 | 不改（缺失节点金额为 0，不影响项目合计） |

- 过滤逻辑集中到 `app/nodes.py` 单一 helper（`node_exists(node)` / `active_nodes(nodes)`），所有展示点走它，避免「改一处漏三处」。

### 2.5 合同识别回填与节点勾选

- ② 识别出的比例 > 0 → 自动勾选对应类型并填比例；识别不到的类型不勾选、留空。
- 识别到比例合计 ≠ 100 时，表单高亮提示（保存时阻断），由用户补/改。
- 识别只影响**新建**；已有项目的合同回改按 §2.6 边界处理。

### 2.6 节点集合后续变化（合同回改 / 变更）

| 变化 | 处理 |
| --- | --- |
| 合同回改把某类型从「未勾选/0」改为 >0 | 补建该节点（金额取 submission 计算结果），`sync_project_dates` 重算日期，审计 `change_note` 记录「补建节点」 |
| 合同回改把某类型从 >0 改为 0，节点**无业务** | 删除该节点（连带清理该 node 的 `node_action_tokens`），金额在剩余节点间重算尾差，审计留痕 |
| 同上但节点**已有申请/开票/付款** | **阻断**，扩展 `_contract_blockers`（`app/routes.py:1869`）：「节点已有业务，金额已冻结，需走变更单」 |
| 合同变更（`_build_contract_change_plan` / `project_contract_change`） | 分摊只按**现存节点**比例计算，缺失/0 节点不参与尾差 |

### 2.7 历史数据兼容（不做破坏性改动）

- 历史 4 节点项目：数据不动、展示不变（4 类比例均 > 0，等价「全勾选」）。
- 历史 0 比例节点：等价「未勾选」，展示层隐藏、数据保留（Q5：是否要一次性清理脚本）。
- 历史项目的比例校验、导出、报表口径不变；过滤后与页面一致。
- 新增项目与历史项目在详情/导出/报表使用同一套 helper，避免两套逻辑。

### 2.8 ② 的验收点

- A-07：新建时只勾「首付款 + 验收款」→ 项目只有 2 个节点；详情/项目列表/报表/导出/待办提醒五处一致，无空组、无英文组头、无 0 金额行。
- A-08：勾选 1 类、3 类、4 类均正确；0 勾选、勾选类型合计≠100、勾选但比例=0 均被拒并定位到字段。
- A-09：历史 4 节点项目（含 `1bebfb5` 前数据）展示/导出/报表不变；历史 0 比例节点只隐藏不删。
- A-10：合同回改 0→>0 补建节点、>0→0 且无业务删除、有业务阻断；合同变更分摊只落在现存节点。
- A-11：加科目（`item_add`）沿用项目节点集合，不会凭空多出未勾选类型。

---

## 3 需求①：合同识别并入「新建项目」

### 3.1 目标与口径

- 「新建项目」页即为**唯一**合同识别入口：上传三张关键信息截图（①合同金额、②付款条件/付款方式 **必传**，③维保/质保金条款选传）→ 识别 → 结果直接回填到同一张表的金额/比例/节点勾选/质保期等字段 → 用户核对修改后一次保存生成项目与节点。
- 不再经过 `contract_confirm` 二次确认页（新建场景没有「改价」语义，一次核对即可）；`contract_confirm` 仅保留给历史草稿。
- 识别是**辅助**：所有字段都可手工修改；①② 全部识别失败时需显式勾选人工核对（§3.5）。

### 3.2 页面结构（`project_form.html`，仅新建态）

```
┌ 合同截图识别（可选，推荐） ────────────────────────────────┐
│ ① 合同金额截图 [必传]   ② 付款条件截图 [必传]   ③ 质保条款截图 [选传] │
│ [上传并识别]   支持 JPG/PNG/BMP/WEBP，单张≤10MB                      │
│ ── 已识别：草稿 #123 ──                                            │
│ [缩略图1][缩略图2][缩略图3]  识别来源：规则/LLM   [重新识别] [放弃识别结果] │
│ 提示：识别结果已填入下方表单，请对照原图核对；识别不到请手填            │
└──────────────────────────────────────────────────────────────┘
项目编号* [____]   项目名称* [____]   质保期(月)[__]   备注[____]
付款节点：☑首付款[30]% ☑进度款[52]% ☐验收款[ ]% ☐质保金[ ]%   合计 82% ⚠
         节点金额预览：首付 ￥…  进度 ￥…（按科目原金额合计 × 比例）
费用科目：合同价款 | 税率[__] | 原金额[__] | 增补金额[__]  [+ 添加科目]
[ ] 已对照原图人工核对（仅当①②都识别失败时必填）
[保存项目]
```

- 识别卡片放在表单最上方：用户可以先识别再填，也可以直接手填（不强制上传）。
- 隐藏字段：`contract_id`（当前草稿）、`manual_confirm`（人工核对勾选）。

### 3.3 流程与时序

**A. 识别（上传即暂存）**

1. `GET /projects/new` → 渲染空表单（无草稿）。
2. 用户选三张图 → `POST /projects/new/recognize`（`multipart/form-data`，带 CSRF）。
3. 服务端：复用 `_shot_file_error` 校验格式/大小（①② 必传，`SHOT_REQUIRED`）→ 建草稿 `ContractFile(file_type='img', source='shots', status='uploaded', project_id=NULL, created_by=actor)` 并 `flush()` 拿到 id → 文件落 `UPLOAD_DIR/shots/<contract_id>/<shot_type>-<hex>.<ext>` → 逐张 `ContractScreenshot(contract_id, project_id=NULL, ...)` → OCR → `merge_shot_texts` + `recognize_shots` 写 `result_json`/`raw_text` → `status='recognized'` → 审计 → **302 到 `/projects/new?contract_id=<id>`**（PRG，防刷新重复上传）。
4. `GET /projects/new?contract_id=<id>`：校验草稿存在、`project_id IS NULL`、`created_by` 为本人或 actor 是 admin → 取出识别值渲染表单（§3.4），三张缩略图放识别卡片内。

**B. 保存（绑定草稿）**

1. 用户核对/修改表单 → `POST /projects/new`（带 `contract_id`）。
2. 服务端按现有逻辑校验并创建 Project/科目/节点（`routes.py:722`，含 §2.3 的节点勾选校验）。
3. 同一事务内绑定：`UPDATE contract_files SET project_id=?, status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=? AND project_id IS NULL`（CAS，防两个标签页重复使用同一草稿）；`UPDATE contract_screenshots SET project_id=? WHERE contract_id=?`；项目名称/备注/质保期用表单值（识别值只是初值）。
4. 审计沿用 `project.create` + `contract.confirm`；重定向项目详情。
5. 详情页截图区（`project_detail.html:65-84`）无需改动即可显示（`shot_rows` 依据 `contracts` 关系）。

**C. 校验失败/异常**

- 表单校验失败：不建项目、**不删草稿**、不清文件，重新渲染表单并保留 `contract_id` 与缩略图。
- 上传/识别阶段失败（格式、超限、OCR 异常）：flash 提示并回新建页，草稿不动（已存图片可继续用）。
- 浏览器中途关闭：草稿留存，由清理脚本回收（§3.6）。

### 3.4 回填映射（识别值 → 表单字段）

| 识别结果 | 来源截图 | 回填位置 | 覆盖规则 |
| --- | --- | --- | --- |
| `contract_amount` | ① | 第一个科目「原金额」；科目名默认「合同价款」 | 仅当该字段为空时填充，用户已填不覆盖 |
| `tax_rate` | ① | 第一个科目「税率」 | 同上 |
| `ratios.first/progress/acceptance/warranty` | ② | 比例 > 0 的类型**自动勾选**并填对应比例；其余不勾选 | 仅填空值；识别不到保持空 |
| `payment_method` | ② | 备注前置「付款条件：…」（沿用 `routes.py:1885 _contract_remark` 文案风格） | 追加，不覆盖用户已写内容 |
| `warranty_months` | ③ | 质保期（月） | 识别不到留空（到期日不推算，CR-05 口径） |
| `contract_name` / `project_no` | ①②（可能带出） | 项目名称 / 项目编号 | 名称可用识别值；**项目编号仍必须手填**（不自动生成，CR-04 口径） |
| 三张原图 | — | 识别卡片缩略图 + 详情页缩略图 | 统一走 `contract_shot_image` 鉴权 |

- 多科目/多金额：识别只填第一个科目，其余科目用户手工加；**不强制**「科目原金额合计 = 合同金额」（保持现状，Q4）。
- 「重新识别」= 再次上传覆盖为**新草稿**并回填（旧草稿进入清理队列），页面提示「将覆盖当前表单中的识别值」。

### 3.5 识别失败降级

- 单张 OCR 失败：该维度值留空，页面标红提示「第 X 张未识别，请对照原图手填」，不阻断。
- **①② 两张都识别失败/结果为空**：仍允许保存（识别是辅助），但服务端要求 `manual_confirm=on`，否则拒绝并提示「识别失败，请对照原图核对后勾选确认」；审计 `after_json.manual_confirm=true` 留痕。
- 识别值明显不合法（勾选比例合计≠100、单比例>100、金额≤0）：保存时按 §2.3 校验阻断，提示用户修正。
- LLM 不可用：自动只用规则/OCR，行为与现在一致。

### 3.6 草稿清理

- 新增 `scripts/cleanup_contract_drafts.py`：
  - 默认 **dry-run**：列出候选（`project_id IS NULL AND status IN ('uploaded','recognized') AND created_at < now - N days`）与文件数/体积，不删。
  - `--apply` 执行：删 `contract_screenshots` 行 → 删 `contract_files` 行 → 删 `uploads/shots/<contract_id>/` 目录；逐条输出结果。
  - `--days N`（默认 7）、`--db PATH`（默认实例库，测试用临时库）。
  - 审计：逐条 `contract.draft.purge`（`object_id=contract.id`，`after_json={files, bytes}`）。
- 不做请求内自动清理（GET 不写库、不加延迟）；清理交给计划任务（部署手册补一行）。
- 可选配置 `DRAFT_RETENTION_DAYS=7` 进 `app/config.py` 键表（`:190-215`），脚本默认读它。

### 3.7 旧入口与详情页处理

- `base.html:25` 导航删除「合同识别」项（新建页成为唯一入口）。
- `GET /contracts` 保留但收敛：**仅 admin**（operator 访问 302 到 `/projects/new` 并 flash）；页面顶部横幅「合同识别已并入『新建项目』，此页仅用于历史识别记录与整份合同兜底」；三截图上传表单移除，保留识别记录表与整份合同兜底表单。
- 历史未确认草稿仍可在 `/contracts` 继续走 `contract_confirm`（不动旧流程）。
- 详情页「合同截图」展示不变（需求「图片在项目详情里能直接看到」继续满足）。

### 3.8 ① 的验收点

- A-01：新建页可直接上传三张截图并识别，①② 缺失被拒，③ 可省；识别值回填金额/节点勾选与比例/质保期/付款条件。
- A-02：保存成功后项目与节点生成，详情页可见三张缩略图且原图可点开（admin/operator），viewer 403。
- A-03：识别全失败时手填可保存，但必须勾选人工核对；未勾选被拒。
- A-04：识别后表单校验失败重渲染，草稿与图片保留、可继续提交。
- A-05：导航无「合同识别」；operator 访问 `/contracts` 被导向新建页；admin 可看历史。
- A-06：清理脚本 dry-run 不改库、`--apply` 只删超期草稿（有项目的、近期的不删）。

---

## 4 需求③：付款节点管理里科目显示为空（BUG）

### 4.1 复现

| 项 | 内容 |
| --- | --- |
| 页面 | 项目详情 `/projects/<id>` →「付款节点管理」表格 |
| 数据条件 | 任意项目（单/多科目均可），与科目配置无关 |
| 现象 | 该列空白；同页「费用科目」表科目名正常；导出 xlsx 的「科目」列有值 |
| 已实测 | 测试用临时库复现 100% 必现；开发只读复现：整页三个科目名各只出现 1 次（都在科目明细表），节点表首行科目单元格只剩「已冻结」徽标、无科目名 |

### 4.2 根因

- `app/templates/project_detail.html:212`：`<td>{{ n.item_name }}`。
- `PaymentNode`（`app/models.py:141`）**没有** `item_name` 属性；科目是关系 `ProjectItem.nodes` 的反向引用 `item`（`models.py:124`）。
- Jinja2 对未定义变量默认渲染空串且不报错 → 静默空白。
- 同页科目表用 `it.item_name`（`ProjectItem` 字段，`:162`）正常；导出用 `node.item.item_name`（`exporter.py:226`）正常 → **只有节点表这一处**；全模板 grep 后仅此一处误用。

### 4.3 修复

- `project_detail.html:212` 改为：`<td>{{ n.item.item_name if n.item else '—' }}</td>`（item 为空的降级文案按 Chief 定为「—」）。
- 不在 `PaymentNode` 上加同名 property（会掩盖模板错误，且与 `to_dict`/导出口径重复）。

### 4.4 防回归（测试 4 条，与 `docs/qa-cr07-plan.md` 对齐）

1. 种子项目（多科目）详情页：每行首列 = 对应科目名，非空。
2. 多科目项目：科目名不串行（逐行匹配 `node.item.item_name`）。
3. 合同识别生成的「合同价款」项目：节点表显示「合同价款」。
4. 无科目/异常数据（item 为空）显示「—」，不报 500。
- 可选（Q6）：`scripts/check_templates.py` 用 Jinja2 元数据检查未定义变量，作为 CI 卡点。

---

## 5 需求④：回收站彻底删除

### 5.1 入口、权限与前置条件

- `app/templates/projects.html` 回收站视图（`deleted_view`，`:99-109`）：「恢复」旁增加「彻底删除」按钮（`btn-danger`），点击弹出确认层。
- 新路由 `POST /projects/<int:project_id>/purge`：
  - **仅 admin**；operator/viewer → 403 + 审计 `project.purge.denied`。
  - 项目必须存在且 `deleted_at IS NOT NULL`（先软删进回收站）；否则 404/400 + flash。
  - **业务阻断**：项目存在任一申请（`payment_applications`）、发票（`payment_invoices`）、付款记录（`payment_records`）→ **拒绝**，flash「该项目已有申请/开票/付款记录，不可彻底删除，只能归档」，审计 `project.purge.blocked`。
  - CSRF：沿用全局校验。
- 「只允许归档」：沿用现有软删除=归档语义（`deleted_at`/`deleted_by`，回收站可恢复），不新增状态机；是否要单独「归档」按钮见 Q7。

### 5.2 二次确认与不可恢复提示

```
彻底删除项目「<项目名称>」？
将永久删除：科目 2 / 付款节点 8 / 验收记录 2 / 合同识别 1 / 截图 3，以及全部附件文件。
此操作不可恢复，也不会进入回收站。
[输入项目名称确认] [ ] 我已知悉不可恢复
[取消] [彻底删除]
```

- 必填：输入**项目名称**与服务端 `project.name` 完全相等（strip 首尾空白后比较，不忽略错字/大小写差异）+ 勾选 `confirm_irreversible`；任一不满足 → flash 拒绝，不执行。
- 计数由服务端 POST 时重新计算并展示（GET 展示的计数只作提示）。
- 提交后项目已不存在 → 404「项目已删除」。

### 5.3 级联范围与顺序（单事务）

1. `node_action_tokens`：`node_id IN (SELECT id FROM payment_nodes WHERE project_id=?)`
2. `payment_records`：`node_id IN (…)`（前置已保证为空，保留兜底）
3. `payment_applications`：`node_id IN (…)`（同上）
4. `payment_invoices`：`node_id IN (…)`（同上）
5. `payment_nodes`：`project_id=?`
6. `project_items`：`project_id=?`
7. `acceptance_records`：`project_id=?`
8. `contract_screenshots`：`project_id=? OR contract_id IN (SELECT id FROM contract_files WHERE project_id=?)`
9. `contract_files`：`project_id=?`
10. `projects`：`id=?`

- **保留**：`audit_logs`（无外键，历史留痕必须保留）、`import_batch_rows`（无项目外键）、`user_sessions`（无关）、其他项目的任何数据。
- 先子后父，避免 SQLite 外键报错；全部在一次 `commit()` 内完成。
- `NodeActionToken` 没有 node 外键（`models.py:412`），必须显式按 node_id 删，否则成孤儿。

### 5.4 附件文件清理

- 收集：每个 `contract.stored_path`（`uploads/<contract_id>.<ext>`）+ 每张 `screenshot.stored_path`（`uploads/shots/<contract_id>/…`）+ 对应目录。
- 路径经 `contract_storage_path` / `screenshot_storage_path`（`routes.py:1381/1525`）解析，并校验 `realpath` 落在 `UPLOAD_DIR` 内（防路径穿越/误删）。
- 先 DB `commit()`，再 best-effort 删文件：单个失败记 `logger.warning` 并写入审计 `after_json.file_errors`，不影响删除结果。

### 5.5 审计留痕

- `action='project.purge'`，`object_type='project'`，`object_id=原项目 id`，`project_id=原项目 id`，`result='success'`。
- `before_json`：项目/科目/节点/验收/合同/截图关键字段 + `counts`（各表删除行数）+ `files`（待删路径清单）。
- `after_json`：`{'purged': true, 'file_errors': [...]}`。
- 审计页不 join `projects`，删除后仍可查（现实现即如此）。

### 5.6 幂等 / 并发 / 失败

- 重复提交：第一次成功后再提交 → 404「项目不存在或已删除」。
- 并发两请求：以各 `DELETE` rowcount 判断；项目行删除 0 行 → 404。
- 事务失败：整体回滚，**不动文件**，flash「删除失败，数据未变更，请重试」。

### 5.7 可选：purge 前自动留档（Q8）

- 若同意：删除前把快照写入 `instance/purge_backup/<project_no>-<YYYYmmddHHMMSS>.json`（不含图片二进制，只记路径），保留 30 天由运维/脚本人工清理；不提供「一键恢复」，仅用于取证。
- 成本约 0.2 天，建议做；不同意则只在审计里留 JSON 快照。

### 5.8 ④ 的验收点

- A-12：operator/viewer 调 purge → 403 + 审计 denied；未软删项目 → 400/404 且数据不变。
- A-13：有申请/发票/付款任一的项目 → 拒绝并提示归档，数据不变（含回收站内项目）。
- A-14：项目名称不符或未勾选不可恢复 → 拒绝，数据不变。
- A-15：admin 成功 purge 后：9 类关联数据该项目的行数为 0、附件文件与目录不存在、审计有 `project.purge` 且含计数/快照。
- A-16：回收站列表不再出现该项目，详情 404，报表/导出不含；其他项目数据不受影响。
- A-17：二次 purge 404；模拟文件占用时数据已删、审计记录 `file_errors` 不阻断。

---

## 6 接口清单

| 方法 | 路径 | 权限 | 说明 | 变更 |
| --- | --- | --- | --- | --- |
| GET | `/projects/new` | admin/operator | 新建页（节点勾选；`?contract_id=` 带草稿回填） | 改 |
| POST | `/projects/new/recognize` | admin/operator | 三截图上传+识别，建草稿，302 回新建页 | **新增** |
| POST | `/projects/new` | admin/operator | 建项目（`node_types` 多选 + 可选 `contract_id`） | 改 |
| POST | `/projects/new/discard` | admin/operator | 放弃当前识别草稿（删行+删文件+审计） | **新增** |
| GET | `/contracts` | **admin** | 历史识别记录 + 整份合同兜底 | 改（导航移除、限 admin） |
| POST | `/contracts/shots/upload` | admin | 旧入口兜底 | 保留 |
| GET | `/contracts/shots/<id>/image` | admin/operator | 截图原图（鉴权 + `no-store`） | 不改 |
| POST | `/projects/<id>/purge` | **admin** | 回收站彻底删除 | **新增** |

---

## 7 数据影响与迁移

- **Schema：0 迁移。**
  - ②：`PaymentNode.ratio` 继续表达节点是否存在（不建节点=不存在），不新增 `node_type` 取值、不新增 `seq`、不改历史数据。
  - ①：草稿用现成 `contract_files.source='shots'` + `project_id NULL`、`contract_screenshots.project_id NULL` 表达；保存时回填。
  - ③：模板 1 行。
  - ④：只删行，不改表结构；`audit_logs` 无外键，删除后日志仍在。
- 新增脚本：`scripts/cleanup_contract_drafts.py`（草稿清理，dry-run 默认）；可选 `scripts/report_zero_ratio_nodes.py`（只读报告）。
- 新增配置（可选）：`DRAFT_RETENTION_DAYS`（默认 7）加入 `app/config.py` 键表。
- 前端文件：`project_form.html`（识别卡片/节点勾选/隐藏字段/金额预览）、`projects.html`（彻底删除按钮+确认层）、`project_detail.html`（③ 一行 + 空组不渲染）、`contracts.html`（横幅、移除截图表单）、`base.html`（导航移除）。
- 兼容性：`/projects/new` 新增字段均可选默认，旧表单/书签不破坏；旧 `/contracts` 流程保留给历史草稿。

---

## 8 任务拆分（建议给开发）

| 编号 | 任务 | 依赖 | 写区 | 验收 |
| --- | --- | --- | --- | --- |
| T1 | ③ 修复（`n.item.item_name`，空则「—」）+ 详情页空组不渲染 | 无 | `app/templates/project_detail.html` | A-12~? ③ 4 条用例 |
| T2 | ② 勾选式节点：`validate_node_selection` + `_create_item_nodes(node_types)` + `project_new`/`item_add` + 展示/导出/提醒过滤 + 合同回改边界 | T1 | `app/validation.py`、`app/routes.py`、`app/nodes.py`、`app/exporter.py`、`app/templates/project_form.html` | A-07~A-11 |
| T3 | ① 新建页识别：`/projects/new/recognize` + 草稿落盘 + 回填（含节点自动勾选）+ 绑定 + 放弃 + `manual_confirm` | T2（共用 `project_form.html`） | `app/routes.py`、`app/templates/project_form.html` | A-01~A-04 |
| T4 | ① 旧入口收敛（导航/权限/横幅）+ `cleanup_contract_drafts.py` | T3 | `app/routes.py`、`app/templates/{base,contracts}.html`、`scripts/` | A-05/A-06 |
| T5 | ④ purge 路由 + 级联 + 业务阻断 + 审计 + 文件清理 +（可选备份）+ 回收站按钮 | 无（可与 T2/T3 并行） | `app/routes.py`、`app/templates/projects.html`、`scripts/`（可选） | A-12~A-17 |
| T6 | 测试补用例（③ 4 条、② 勾选组合/历史兼容、① 端到端、④ 安全/级联） | T1–T5 | `tests/` | 405 → 约 450–470 全绿 |
| T7 | 文档：`docs/qa-baseline.md`/`roadmap.md`/部署手册（清理任务） | T6 | `docs/` | — |

- 顺序：T1 → T2 → T3 → T4；T5 可与 T2/T3 并行（`routes.py` 有交叠，按函数分段改或串行合并）。
- 每个任务自带自测（开发负责），测试在 T6 统一加回归。

---

## 9 测试影响（给测试的输入）

- 现有 405 项保持全绿；新增预计 45–65 项：
  - ③：4 项（已列 §4.4）。
  - ②：约 14 项（勾选 1/2/3/4 类、0 勾选、合计≠100、勾选但比例 0、加科目不加类型、历史 4 节点兼容、历史 0 节点隐藏、合同回改补建/删除/阻断、四端一致）。
  - ①：约 20 项（①② 必传缺失、格式/超限、③ 选传、识别回填含自动勾选、保存绑定、失败重渲染保留草稿、manual_confirm、discard、清理脚本 dry-run/apply、权限、详情页截图可见）。
  - ④：约 18 项（三角色权限、未软删拒绝、业务项目拒绝并提示归档、项目名称精确/错字、未勾选、级联计数、文件删除、审计不被级联、二次 purge、列表/详情/导出不可见、其他项目不受影响、并发/CSRF）。
- 测试计划已与 `docs/qa-cr07-plan.md`（测试维护）对齐，新增用例按其骨架补。

---

## 10 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 「同一类型多个进度款/自定义名称」需求本期不做 | 用户预期差（用户提过「大项目可以有多个进度款」） | Q1 明确交用户确认；必要时另立 CR 做 `node_type` 语义 + seq |
| ② 勾选集合在多个入口不一致（新建/加科目/合同回改） | 同项目出现不该有的类型或缺失节点 | 单一 helper（`active_nodes`/`project_node_types`）+ 生成端参数化 + 用例覆盖 4 入口 |
| ② 展示点漏改 | 详情/导出/报表不一致 | 过滤集中到 `nodes.py`；测试四端同数据断言 |
| ② 合同回改补建/删除节点触碰冻结逻辑 | 已发生业务节点被误删/误改 | `_contract_blockers` 扩展阻断 + 审计 `change_note` + 专项用例 |
| ① 草稿堆积占磁盘 | `uploads/shots` 膨胀（每次 ≤30MB） | 清理脚本 + 计划任务 + 上传限 admin/operator |
| ① 两个标签页用同一草稿 | 重复创建或绑定失败 | CAS `project_id IS NULL` 更新，失败提示重新上传 |
| ④ 物理删除不可逆 | 误删无法恢复 | 项目名称精确确认 + 勾选 + 业务阻断 + 审计快照（+可选 JSON 备份） |
| ④ 级联删漏表 | 孤儿数据/报表异常 | 显式 10 步删除清单 + 用例断言各表计数为 0 |

---

## 11 验收标准（DoD）

- ②：A-07~A-11 全过；勾选式新建在详情/列表/报表/导出/提醒五处一致；历史 4 节点项目无变化。
- ①：A-01~A-06 全过；新建页是唯一识别入口；三张图在详情页可见；识别失败可降级且留痕。
- ③：§4.4 四条全过，回归防复发。
- ④：A-12~A-17 全过；仅 admin、仅回收站、输入项目名、业务项目只允许归档、级联完整、审计可查。
- 回归：全量用例两遍 0 FAIL/0 XFAIL/0 XPASS；干净工作区；文档/基线数字同步。

---

## 12 待 Chief / 用户确认的问题（8 条）

| # | 问题 | 建议默认 | 为什么需要拍板 |
| --- | --- | --- | --- |
| **Q1** | 用户提过「大项目可以有多个进度款节点」，且自定义名称未实现：本期先按「勾选固定 4 类、每类 1 个」上线是否可接受？多个同类节点/自定义名称另立 CR（需改 `node_type` 语义 + seq + 历史兼容，约 2–3 天）？ | 本期勾选版先上线，多节点/自定义名称另立 CR | 直接影响用户核心预期，需求原话含「多个进度款」 |
| Q2 | 旧「合同识别」入口：导航移除后 `/contracts` 是否只留 admin（历史草稿 + 整份合同兜底）？ | **是**；operator 访问跳新建页 | 旧流程去留与权限 |
| Q3 | 历史识别草稿保留多久？超期脚本**物理删除**（含图片）还是先备份？ | 保留 **7 天**，物理删 + 审计 | 磁盘与可追溯性取舍 |
| Q4 | 识别回填只填**第一个科目**（默认「合同价款」），其余科目手工加；「科目金额合计 = 合同金额」不强制勾稽（保持现状）？ | **是** | 需求①「直接填入各节点」的边界 |
| Q5 | 历史已存在的 0 比例节点：只隐藏不清理（建议），还是出一次性清理脚本？ | **只隐藏，数据保留** | 是否动历史数据 |
| Q6 | ③ 是否顺带加「模板未定义变量」静态检查进 CI？ | **加**（约 0.3 天） | 少量维护成本换同类回归防护 |
| Q7 | 「只允许归档」是否复用现有「软删除→回收站」流程（`deleted_at`），不新增独立「归档」按钮？ | **复用**，不新增 | 影响 §5 的阻断引导文案与页面入口 |
| Q8 | purge 前是否自动 JSON 快照到 `instance/purge_backup/`（保留 30 天）？是否需要额外口令/双人确认？ | **自动快照：是**；额外口令：**否**（项目名+勾选足够） | 不可恢复操作的安全边界 |

> 备注：Q1 是本期唯一影响「节点模型是否要动」的决策点；其余问题不改变数据模型。

# 批次 3 审计实现 · 审查意见（基于 2026-10-23 10:37 工作区快照）

- 审查人：架构师；代码：`app/audit.py`（164 行）、`app/models.py::AuditLog`、`app/routes.py` 埋点 28 处、`templates/audit.html`
- 方法：代码通读 + 临时库探针实测（不碰 `instance/`）
- 结论：**字段与页面方向正确，但事务语义有实测缺陷、埋点有缺口，修完再合入**。

## 一、必须修（P0）

### 1. `record()` 的事务语义不确定，且会静默丢日志

实测（Python 3.12 + SQLAlchemy 2.1.5 + SQLite）：

| 场景 | 结果 |
| --- | --- |
| 请求内**只有审计写入**（登录/登出/失败登录），不做业务 commit | 审计**落库**（期望同事务应为 0，实际 1） |
| 业务写入（flush）后写审计，再 `rollback()` | 审计与业务**一起回滚**（0） |
| 先写审计、再执行业务 rollback（无前置业务写） | 审计**不随回滚**（1→2） |

原因：`record()` 用 `with db.session.begin_nested()`（SAVEPOINT）包住 INSERT。SQLite/pysqlite 在**没有活动事务**时执行 SAVEPOINT，RELEASE 会隐式提交；有前置 DML 时 SAVEPOINT 才落在事务里。于是同一条代码在"登录"和"业务动作"两种场景下事务语义不同：登录审计靠偶然持久化，业务回滚场景又可能留下"动作未发生但审计已写"的假记录。

修复建议：
1. 去掉 `begin_nested()`，直接在**当前 session** `db.session.add(entry)`，不单独 flush/commit；
2. 由调用方统一 `commit()`（现有业务路由已如此）；`login/logout/login.failed` 这三处补 `db.session.commit()`（目前没有 commit，靠偶然行为落库）；
3. 审计写失败**不能静默**：对 `node.pay`、`node.invoice`、`contract.modify`、`project.delete`、`user.*`、`auth.login.*` 等关键动作，让异常向上抛（业务一起失败）；非关键动作可 `logger.exception` + 返回 False，并在 `/healthz`/日巡检暴露"审计失败计数"；
4. 加一条自动化用例：业务抛出 ValidationError → 断言业务与审计都未落库；正常提交 → 两者都在。

### 2. 埋点缺口（政务审查会问）

- `role_required` 拒绝（越权尝试）**没有审计**：viewer/operator 尝试删除、改状态、进管理页时只 flash+302，查不到痕迹。建议在 `role_required` 里记 `auth.denied`（actor、endpoint、method、result='denied'，object 可空）；
- `project.delete` 仍是**物理删除**（`db.session.delete`），批次 3 承诺的软删除（`deleted_at`）未落地；物理删除后审计"before"是唯一证据，至少先加删除前导出/二次确认；软删除随批次 3 补齐；
- 后续批次必补：改密/重置密码、登录锁定（批次 4）；Excel 导入/导出（批次 6）；报表导出；合同原文查看（敏感读操作，可选）；
- 现有 `result` 值混用（停用账号登录记 `failed`，设计约定用 `denied`），请统一枚举并写进模块 docstring。

## 二、建议补（P1）

| 项 | 说明 |
| --- | --- |
| `actor_role` 快照 | 只存 actor_name 不够：角色变更后无法判断当时权限；加 `actor_role`（VARCHAR(16)），`record()` 自动取 |
| `request_id` / `error_msg` | 排障与串联多次操作需要；`request_id` 用 `X-Request-ID` 或每请求生成；`error_msg` 记录失败原因 |
| 对象维度索引 | 现只有 `ts/action/project/(actor,ts)/(project,ts)/(action,ts)`；按节点/合同查历史（"这个节点被谁改过"）需 `(object_type, object_id, ts)`，页面加实体过滤 |
| IP 信任边界 | `_request_meta()` 直接取 `X-Forwarded-For` 首值，客户端可伪造；只在配置了可信代理（`TRUSTED_PROXY_COUNT`）+ `ProxyFix` 后使用，否则取 `remote_addr` |
| 时间口径 | 审计 `ts` 用 UTC，业务 `created_at` 用本地 `datetime.now`，新加 `updated_at` 又用 `datetime.utcnow`——三种口径并存；建议统一存 UTC、展示按 `APP_TIMEZONE` 转本地，页面筛选日期按本地日界换算 |
| JSON 体积上限 | `diff/blockers` 可能很大；`_dump` 后超过 8KB 截断并标 `truncated`，避免审计表膨胀 |
| 查询/导出 | 页面只有筛选+分页，无 CSV 导出、无实体过滤；政务常见要求"可导出留档"，P1 加只读导出 |
| 保留期 | 无清理/归档策略；按 ≥3 年保留写进运维文档（暂不做自动清理，避免误删证据） |

## 三、数据库迁移风险（阻断部署）

批次 1~3 给 `users` 加了 `is_active`、给 `projects/items/nodes` 加了 `updated_by/updated_at`、新增 `audit_logs` 表，但项目**还没有 Alembic**。`db.create_all()` 不会给已有表加列：旧库升级后会报 `no such column`，且 `audit_logs` 不存在时 `record()` 的异常被吞 → **审计静默全丢**。上线前必须二选一：①提供幂等 `scripts/migrate_p0.py`（ALTER TABLE + CREATE TABLE/INDEX，先备份）；②按路线图重置实例库（会丢现有数据，仅限当前测试库）。正式单位数据必须走 ①，并纳入部署验收。

## 四、合入验收清单

- [ ] 登录/登出审计在同事务内可靠落库（去掉 savepoint，路由补 commit）
- [ ] 业务 rollback 时审计一起回滚；业务提交时审计存在（自动化用例）
- [ ] 越权尝试有 `auth.denied` 记录
- [ ] 审计写失败对关键动作**阻断业务**，非关键动作有 ERROR 日志/计数
- [ ] `audit_logs` 在旧库升级脚本中创建成功；`updated_by/is_active` 列升级成功
- [ ] `python tests/run_qa.py` 退出码 0，审计用例从 XFAIL 转 PASS
- [ ] 页面：admin 可筛选（项目/人/动作/日期）、分页、只读，无编辑删除入口
- [ ] 90 秒内能回答："某项目某节点在什么时间被谁把金额/付款状态从 X 改成 Y"（用页面 + 关联审计）

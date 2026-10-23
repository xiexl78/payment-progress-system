# 批次 5 技术设计：配置外置 + init_db 安全重建

- 版本：v1.0（2026-10-23）；状态：**设计稿，不改 `app/`**；开发 4b 完成后实施
- 主责：架构师（设计）/ 开发（落地）；配套：[`config.md`](config.md)、[`deploy.md`](deploy.md)、[`backup-restore.md`](backup-restore.md)
- 目标：生产配置零代码修改；配置错误启动即失败；`init_db --force` 不可能在无确认、无备份的情况下毁数据。

---

## 一、配置外置落地设计（新增 `app/config.py`）

### 1.1 加载顺序与职责

```
create_app(config=...) 显式覆盖（测试，最后应用）
  > 进程环境变量（NSSM AppEnvironmentExtra）
  > PAYMENT_CONFIG_FILE 指向的 key=value（代码目录外，ACL 保护）
  > DEFAULTS（仅开发/非关键项；生产关键项无默认或默认即报错）
```

`load_config(app_dir, config_file=None) -> (cfg: dict, errors: list[str], warnings: list[str])`：
只做解析/类型转换/范围校验，**不建目录、不连库**；`create_app` 拿到结果后再落盘与 `db.init_app`。

### 1.2 环境变量到 app.config 的映射（批 5 必须项）

| 环境变量 | app.config 键 | 默认 | 校验（失败=拒绝启动） |
| --- | --- | --- | --- |
| `SECRET_KEY` | `SECRET_KEY` | 无（生产） | 必填、≥32 字符、≠历史默认值、非常量重复串 |
| `SECRET_KEY_FALLBACKS` | `SECRET_KEY_FALLBACKS` | 空 | 逗号分隔，每项 ≥32 |
| `DATA_DIR` | `DATA_DIR`、`UPLOAD_DIR` | `%ProgramData%\PaymentSystem\data`（不存在则 `<app>\instance` 仅 development） | 绝对路径；可创建/可写；含 `uploads` 子目录自动创建 |
| `DATABASE_URL` | `SQLALCHEMY_DATABASE_URI` | `sqlite:///{DATA_DIR}/payment_progress.db` | 阶段 1 仅允许 `sqlite:///` 绝对路径 |
| `MAX_UPLOAD_MB` | `MAX_CONTENT_LENGTH` | 20 | 整数 1~100，换算字节 |
| `SESSION_LIFETIME_HOURS` | `PERMANENT_SESSION_LIFETIME`（timedelta）、`SESSION_PERMANENT` | 8 | 0.5~72；0=不启用过期（不推荐） |
| `SESSION_COOKIE_SECURE` | `SESSION_COOKIE_SECURE` | production=true | 布尔；development 可 false |
| `LLM_ENABLED` | `LLM_ENABLED` | false | 布尔；true 时要求 `CONTRACT_LLM_ENDPOINT/API_KEY/MODEL` 齐全，否则拒绝 |
| `APP_ENV` | `ENV`/`DEBUG` | production | production 禁止 DEBUG |
| `LOG_DIR` / `LOG_LEVEL` | `LOG_DIR`/`LOG_LEVEL` | `%ProgramData%\PaymentSystem\logs` / INFO | 绝对路径可写；级别枚举 |
| `TRUSTED_PROXY_COUNT` | `TRUSTED_PROXY_COUNT` | 0 | 0~2；>0 时启用 `ProxyFix`（审计 IP 才可信） |
| `PASSWORD_MIN_LENGTH` / `LOGIN_MAX_FAILURES` / `LOGIN_LOCKOUT_MINUTES` | 同名 | 8 / 5 / 15 | 范围校验（批次 4 使用） |
| `PAYMENT_CONFIG_FILE` | —— | 空 | 存在且可读；文件权限告警（服务账号以外可写则 WARN） |

`MAX_UPLOAD_MB` 与 `SESSION_LIFETIME_HOURS` 等一旦非法：收集**全部**错误后一次性 `raise RuntimeError("配置校验失败: ...")`，进程非零退出（NSSM 不反复重启成"假活"）。

### 1.3 `create_app` 改造要点（伪代码，实施时由开发提交）

```python
def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    cfg, errors, warnings = load_config(base_dir, config_file=os.environ.get('PAYMENT_CONFIG_FILE'))
    for w in warnings: app.logger.warning('[config] %s', w)
    if errors: raise RuntimeError('配置校验失败: ' + '; '.join(errors))
    app.config.from_mapping(cfg)
    if config: app.config.update(config)            # 测试覆盖保持可用
    # 目录：DATA_DIR / UPLOAD_DIR / LOG_DIR 创建 + 可写校验（错误再抛）
    # ProxyFix：仅当 TRUSTED_PROXY_COUNT>0
    # db.init_app(app)；SQLite PRAGMA 监听仅当 engine.dialect.name=='sqlite'
    #   WAL 只在启动时设置一次，busy_timeout/foreign_keys 每连接
    # 启动横幅：版本/commit、APP_ENV、DATA_DIR、DB、监听地址、备份目录、LLM、OCR
```

兼容要求：现有 `tests/` 全部用 `create_app({'SQLALCHEMY_DATABASE_URI': ...})`，覆盖优先级必须保持；`scripts/init_db.py` 改用 `engine.url.database` 取库路径（删掉 `URI.replace('sqlite:///')`）。

### 1.4 启动自检（fail-fast，批 5 范围）

| 级别 | 规则 | 行为 |
| --- | --- | --- |
| FAIL | SECRET_KEY 缺失/短/默认值 | 拒绝启动 |
| FAIL | production 且 DEBUG | 拒绝启动 |
| FAIL | DATA_DIR 不可写、DB 不存在（未 init）或不可读 | 拒绝启动并提示 `scripts/init_db.py` |
| FAIL | DATABASE_URL 非 sqlite（阶段 1） | 拒绝启动 |
| FAIL | 数值/布尔/枚举非法；LLM 半配置 | 拒绝启动 |
| WARN | 最近备份 >48h、备份目录不可写、OCR 不可用、时区与系统不一致、配置文件权限过宽 | 启动日志 WARN + 横幅 |

自检实现位置：`load_config` 只做静态校验；路径/DB/备份等依赖运行环境的检查放在 `create_app` 落盘前（统一收集 errors）；`TESTING=True` 时跳过"DB 必须已存在"但仍校验其余项。

### 1.5 测试与验收（批 5）

- `tests/test_config.py`：默认密钥拒绝启动、弱密钥拒绝、LLM 半配置拒绝、`MAX_UPLOAD_MB=0/101` 拒绝、`DATA_DIR` 指向只读目录拒绝、合法配置启动成功且 `UPLOAD_DIR` 创建、测试覆盖 `create_app(config={...})` 生效、`PAYMENT_CONFIG_FILE` 生效且优先级低于环境变量；
- 部署验收：目标机不设 SECRET_KEY 启动 → 非零退出；设强密钥 → 日志含数据目录/DB/备份目录且不含密钥；上传 19MB 成功、21MB 中文 413 页；
- `python tests/run_qa.py` 退出码 0（现有用例不得回归）。

---

## 二、`scripts/init_db.py` 安全重建设计（二次确认 + 自动备份）

### 2.1 现状与风险

现状：`init_db.py --force` 直接 `os.remove(db)`，无提示、无备份；库路径来自 `SQLALCHEMY_DATABASE_URI.replace('sqlite:///','')`；误执行即全损。

### 2.2 新参数与行为

```
python scripts/init_db.py                    # 仅在库/表不存在时初始化，不动现有数据
python scripts/init_db.py --force            # 重建：显示数据统计 → 自动备份 → 输入确认词 → 删除重建
python scripts/init_db.py --force --yes      # 非交互（计划任务/自动化），仍自动备份
python scripts/init_db.py --force --no-backup --yes   # 显式跳过备份（高危，仅排障；日志 WARN）
python scripts/init_db.py --backup-dir D:\PSBackup\pre-reset   # 自定义备份位置
```

流程（每条失败即退出非零，**库保持原样**）：

1. **解析真实库路径**：`engine.url.database`（替代字符串替换），校验是 sqlite 文件；`--force --no-backup` 强制要求同时给 `--yes`。
2. **数据体检**：库存在则只读查询 users/projects/project_items/payment_nodes/payment_applications/payment_records/contract_files 行数与付款合计，打印"将丢失的数据"；检测 `-wal` 非空 → 说明服务可能仍在运行，**默认中止**（`--allow-running` 才继续，仅排障用）。
3. **自动备份（默认必做）**：调用 `scripts/sqlite_tool.py backup --src <db> --dst <backup_dir>/pre-reset/<YYYYMMDD-HHMMSS>/payment_progress.db`（子进程，同解释器），再跑 `sqlite_tool.py verify --db <备份>`；同时把数据体检 JSON 与操作人/主机/时间写成 `pre-reset.json` 放同目录；`uploads/` 不删不动（无需备份，但在摘要中列出数量）。
4. **二次确认**：交互模式要求输入 **库文件名**（如 `payment_progress.db`）而不是 y/N，输入不符 → 中止；`--yes` 跳过输入但日志标注 `non-interactive`。
5. **删除**：仅删 `payment_progress.db`、`-wal`、`-shm`（逐个存在性检查），不删 `uploads/`；随后 `db.create_all()` + 种子数据 + 默认账号（沿用现有逻辑）。
6. **收尾**：打印备份路径、恢复命令示例（`scripts/restore.ps1 -BackupPath ... -TargetDir <DATA_DIR> -Force`）、新库行数；退出码 0。
7. `--force` 且备份失败/校验失败/备份目录不可写 → **拒绝删除**（错误信息给出 `--no-backup` 用法但强调风险）。

### 2.3 关键实现约束

- 只用标准库 + SQLAlchemy 现有能力，不引入新依赖；`subprocess.run([sys.executable, 'scripts/sqlite_tool.py', ...])`，路径用 `sys.executable` 保证同环境；
- 备份完成后必须**校验**（integrity + 行数与体检一致）才允许删除；
- 输出兼容 PowerShell/cmd 中文（UTF-8），退出码约定：0 成功、1 失败/中止、2 参数错误；
- 不在 `--force` 路径里自动 `create_all` 之前删库之外的任何文件；
- 与批 5 配置联通：路径优先取 `DATA_DIR`/`DATABASE_URL`（`create_app` 已解析），不再硬编码 `instance/`。

### 2.4 测试与验收（批 5）

- 临时库：有数据 → `--force` 自动备份成功、确认词错误中止、正确后新库为空库+种子、备份可 `restore.ps1` 恢复且行数一致；
- 备份失败模拟（备份目录只读/`--backup-dir` 指向不存在且不可建的路径）→ 库未被删除、退出码非 0；
- `-wal` 非空（模拟服务在跑）→ 默认中止；
- `--force --yes` 非交互成功；`--force --no-backup` 不带 `--yes` 拒绝；
- 验收：任意一次真实重建前都能在 `pre-reset/` 找到可恢复备份。

---

## 三、实施顺序（开发 4b 之后）

1. 新增 `app/config.py` + `tests/test_config.py`（不改现有路由行为）；
2. `create_app` 接入并跑 `tests/run_qa.py` 全绿（回归门禁）；
3. `init_db.py` 改造 + `scripts/init_db.py` 测试（临时库）；
4. 更新 `docs/deploy.md` 第 3/9 节为"已实现"状态；目标机按部署验收执行；
5. 若 4b 已改 `__init__.py`，批 5 只做增量（配置模块 + 接入），避免与安全基线冲突。

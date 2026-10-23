# 配置外置清单（设计稿，批次 4 落地）

- 版本：v1.0（2026-10-23）
- 状态：**仅设计，未改 `app/__init__.py`**；批次 4（安全基线）由开发一次性落地，避免与批次 3 审计改动冲突。
- 目标：代码里不出现密钥/路径/环境差异；同一份代码在开发机、目标机运行；配置错误**启动即失败**，不带病运行。

## 1. 加载优先级

```
create_app(config=...) 显式传入（测试用，最高）
  > 进程环境变量（NSSM AppEnvironmentExtra / 系统环境变量）
  > PAYMENT_CONFIG_FILE 指向的 key=value 文件（ACL 保护，放在代码目录之外）
  > 代码内默认值（仅用于开发；生产关键项无默认或默认即报错）
```

规则：先解析所有值 → 做类型/范围校验 → 生成 `app.config` → 再建 engine。任何校验失败收集为列表，一次性输出到日志并 `raise RuntimeError`（退出码非 0，NSSM 不会反复重启成"假活"）。

## 2. 环境变量清单

| 变量 | 默认值 | 必填 | 校验规则（启动） | 对应 `app.config` | 代码落点 |
| --- | --- | --- | --- | --- | --- |
| `SECRET_KEY` | 无（开发兜底值必须删除） | **生产必填** | 长度 ≥32；不得等于历史默认串 `payment-progress-secret-key-change-in-prod`；不得全同字符 | `SECRET_KEY` | `app/__init__.py` |
| `SECRET_KEY_FALLBACKS` | 空 | 否 | 逗号分隔，每项 ≥32（密钥轮换过渡期用） | `SECRET_KEY_FALLBACKS` | `app/__init__.py` |
| `DATA_DIR` | `%ProgramData%\PaymentSystem\data`（存在时）；否则 `<app>\instance` | 生产必填 | 绝对路径；目录可创建且**可写**；DB 文件存在且可读（首次初始化除外） | 自定义 `DATA_DIR` | `__init__.py`、`routes.py` 上传目录、`scripts/init_db.py` |
| `DATABASE_URL` | `sqlite:///{DATA_DIR}/payment_progress.db` | 否 | 必须为 `sqlite:///` 绝对路径；安装 `history`/非 SQLite 时启动报"暂不支持"（MSSQL 阶段 3 再放开） | `SQLALCHEMY_DATABASE_URI` | `__init__.py` |
| `MAX_UPLOAD_MB` | `20`（与 README 对齐；当前实现 16，批次 4 统一为 20） | 否 | 整数 1~100；`MAX_CONTENT_LENGTH = MB*1024*1024` | `MAX_CONTENT_LENGTH` | `__init__.py` |
| `SESSION_LIFETIME_HOURS` | `8` | 否 | 数字 0.5~72；0 表示关闭过期 | `PERMANENT_SESSION_LIFETIME` | `__init__.py`、登录处 `session.permanent=True` |
| `SESSION_COOKIE_SECURE` | `true`（生产；HTTPS 反代后） | 否 | 布尔；开发 HTTP 可设 false | `SESSION_COOKIE_SECURE` | `__init__.py` |
| `LLM_ENABLED` | `false` | 否 | 布尔；`true` 时必须同时有 `CONTRACT_LLM_ENDPOINT/API_KEY/MODEL`，否则**拒绝启动**（防止误以为在用 LLM 或半配置外发） | 自定义 `LLM_ENABLED` | `contract_engine.py` 的 `recognize_contract` 入口 |
| `CONTRACT_LLM_ENDPOINT` | 空 | LLM 开启时必填 | http(s) URL；**必须在内网/白名单**，公网地址需审批 | 同环境变量 | `contract_engine.load_llm_config` |
| `CONTRACT_LLM_API_KEY` | 空 | LLM 开启时必填 | 非空；不得写入日志/页面 | 同环境变量 | 同上 |
| `CONTRACT_LLM_MODEL` | 空 | LLM 开启时必填 | 非空字符串 | 同环境变量 | 同上 |
| `CONTRACT_LLM_TIMEOUT` | `60` | 否 | 5~300 秒 | 同上 | 同上 |
| `PAYMENT_BACKUP_DIR` | `%ProgramData%\PaymentSystem\backups` | 生产建议显式 | 绝对路径；目录可写（不存在则创建） | 备份脚本读取 | `scripts/backup.ps1` / `restore.ps1` |
| `APP_ENV` | `production` | 否 | `production`/`development`；production 时禁止 `DEBUG` | `ENV`/`DEBUG` | `__init__.py` / `run.py` |
| `LOG_DIR` | `%ProgramData%\PaymentSystem\logs` | 否 | 绝对路径；可写；不存在则创建 | 自定义 | 批次 4 日志初始化 |
| `LOG_LEVEL` | `INFO` | 否 | DEBUG/INFO/WARNING/ERROR | 自定义 | 日志初始化 |
| `APP_TIMEZONE` | `Asia/Shanghai` | 否 | IANA 时区名（审计时间展示用；DB 内建议统一存 UTC） | 自定义 | 审计/时间格式化 |
| `TRUSTED_PROXY_COUNT` | `0` | 否 | 0~2；>0 时启用 `ProxyFix(x_for=n)`，仅信任指定层数代理 | `ProxyFix` 参数 | `__init__.py`，配合审计取 IP |
| `PASSWORD_MIN_LENGTH` | `8` | 否 | 6~64 | 自定义 | 批次 4 改密/建用户校验 |
| `LOGIN_MAX_FAILURES` | `5` | 否 | 1~20 | 自定义 | 批次 4 登录限速 |
| `LOGIN_LOCKOUT_MINUTES` | `15` | 否 | 1~1440 | 自定义 | 批次 4 登录限速 |
| `AUDIT_RETENTION_DAYS` | `1095`（≥3 年） | 否 | ≥365 | 自定义 | 批次 3/4 审计查询页 |

## 3. 启动自检规则（fail-fast）

实现为 `app/config.py::load_config()` + `validate()`，返回 `(config, errors, warnings)`；`create_app` 在 `db.init_app` 前执行：

| 级别 | 规则 | 行为 |
| --- | --- | --- |
| FAIL | `SECRET_KEY` 缺失/短/为默认值 | 拒绝启动（"可伪造管理员会话"是实测 P0） |
| FAIL | `production` 且 `DEBUG=true` | 拒绝启动 |
| FAIL | `DATA_DIR` 无法创建/不可写；DB 不存在且未执行 init | 拒绝启动并提示 `python scripts/init_db.py` |
| FAIL | `DATABASE_URL` 非 sqlite（阶段 1） | 拒绝启动 |
| FAIL | `MAX_UPLOAD_MB`/`SESSION_LIFETIME_HOURS`/`LLM_ENABLED` 类型或范围错误 | 拒绝启动 |
| FAIL | `LLM_ENABLED=true` 但 LLM 配置不完整 | 拒绝启动 |
| WARN | 备份目录不存在/不可写；最近备份 >48h | 记日志（部署验收兜底） |
| WARN | 上传目录缺失（自动创建）；时区与系统不一致 | 记日志 |
| WARN | OCR 引擎/语言包不可用（当前仅运行期报错） | 记日志 + 启动横幅提示；目标机验收必须实测 |

启动横幅（日志）：版本/commit、环境、数据目录、DB 路径、监听地址、备份目录、LLM 开关、OCR 状态；**绝不打印密钥**。

## 4. 建议代码骨架（批次 4 落地，不在此次提交）

```python
# app/config.py（新）
import os
from pathlib import Path

DEFAULTS = {
    'APP_ENV': 'production', 'MAX_UPLOAD_MB': '20', 'SESSION_LIFETIME_HOURS': '8',
    'LLM_ENABLED': 'false', 'LOG_LEVEL': 'INFO', 'APP_TIMEZONE': 'Asia/Shanghai',
    'PASSWORD_MIN_LENGTH': '8', 'LOGIN_MAX_FAILURES': '5', 'LOGIN_LOCKOUT_MINUTES': '15',
}

def _raw(name, config_file_values):
    return os.environ.get(name, config_file_values.get(name, DEFAULTS.get(name)))

def load_config(app_dir: Path, config_file: Path | None = None):
    file_values = _read_env_file(config_file) if config_file else {}
    errors, warnings = [], []
    # ... 逐项解析/校验，错误 append(errors) ...
    data_dir = Path(_raw('DATA_DIR', file_values) or (Path(os.environ['ProgramData']) / 'PaymentSystem' / 'data'))
    database_url = _raw('DATABASE_URL', file_values) or ('sqlite:///' + str(data_dir / 'payment_progress.db'))
    cfg = {
        'SECRET_KEY': _raw('SECRET_KEY', file_values),
        'SQLALCHEMY_DATABASE_URI': database_url,
        'MAX_CONTENT_LENGTH': int(_raw('MAX_UPLOAD_MB', file_values)) * 1024 * 1024,
        'PERMANENT_SESSION_LIFETIME': timedelta(hours=float(_raw('SESSION_LIFETIME_HOURS', file_values))),
        'SESSION_COOKIE_HTTPONLY': True, 'SESSION_COOKIE_SAMESITE': 'Lax',
        'SESSION_COOKIE_SECURE': _bool(...),
        'DATA_DIR': str(data_dir), 'UPLOAD_DIR': str(data_dir / 'uploads'),
        'LLM_ENABLED': _bool(...),
        # ...
    }
    return cfg, errors, warnings

def validate(cfg, errors, warnings): ...   # 见第 3 节
```

`create_app(config=None)` 改为：

```python
cfg, errors, warnings = load_config(base_dir, config_file=os.environ.get('PAYMENT_CONFIG_FILE'))
for w in warnings: app.logger.warning(w)
if errors: raise RuntimeError('配置校验失败: ' + '; '.join(errors))
app.config.from_mapping(cfg)
if config: app.config.update(config)      # 测试覆盖仍可用
```

## 5. 密钥与配置存储

- 生成：`python -c "import secrets; print(secrets.token_urlsafe(48))"`；
- 存储：NSSM `AppEnvironmentExtra`（注册表，受 ACL 保护）或 `PAYMENT_CONFIG_FILE`（文件 ACL 仅服务账号+管理员，配合 BitLocker）；**不得**提交进 git、不得写在代码/README/日志；
- 轮换：新密钥写入 `SECRET_KEY`，旧密钥放 `SECRET_KEY_FALLBACKS` 24 小时 → 移除旧密钥（所有会话失效，需重新登录）；
- 备份配置：`backup.json` 不记录任何密钥；配置文件与备份分开存放。

## 6. 批次 4 实施清单（开发执行，依赖批次 3）

1. 新增 `app/config.py`（上述骨架 + 校验 + 启动横幅）；
2. `app/__init__.py`：改用 `load_config`；`os.makedirs(DATA_DIR/uploads)`；按 `TRUSTED_PROXY_COUNT` 加 `ProxyFix`；注册统一错误页（404/403/413/500）；`/healthz`；
3. `app/routes.py`：上传目录改 `current_app.config['UPLOAD_DIR']`；登录 `session.permanent=True`；改密/限速/CSRF 见批次 4 清单；
4. `scripts/init_db.py`：去掉 `URI.replace('sqlite:///')`，改 `engine.url.database`；遵守 `DATA_DIR`；
5. 测试：`tests/` 增加配置校验用例（默认密钥拒绝启动、LLM 半配置拒绝、DATA_DIR 不可写拒绝）；`create_app(config=...)` 测试覆盖行为不变；
6. 文档：更新 README 快速启动（环境变量）与 `docs/deploy.md` 验收项。

## 7. 验收标准

- [ ] 不设 `SECRET_KEY` 启动 → 非零退出并给出明确中文错误；
- [ ] 设了强密钥后启动 → 日志横幅含数据目录/DB/备份目录/LLM/OCR 状态，不含密钥；
- [ ] `DATA_DIR` 指向 `C:\ProgramData\PaymentSystem\data`，数据与上传落盘正确；
- [ ] `MAX_UPLOAD_MB=20` 时上传 19MB 成功、21MB 返回中文 413 页；
- [ ] `LLM_ENABLED=true` 缺 endpoint → 拒绝启动；false 时完全不调用外网；
- [ ] 会话 8 小时过期；HTTPS 下 cookie 带 Secure；
- [ ] `PAYMENT_CONFIG_FILE` 可替代 OS 环境变量；测试 `create_app(config={...})` 仍可覆盖；
- [ ] 备份脚本在目标机读取 `DATA_DIR`/`PAYMENT_BACKUP_DIR` 正常。

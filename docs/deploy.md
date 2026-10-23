# 部署手册（内网 Windows 单机 / 小服务器）

- 版本：v1.0（2026-10-23）
- 适用：单位内网，Windows 10/11 单机或 Windows Server 2019/2022，并发 5~20 人
- 配套：[`backup-restore.md`](backup-restore.md)（备份恢复）、[`config.md`](config.md)（配置外置清单）
- 前置依赖：**批次 4（安全基线 + 配置外置）完成前，本文中「数据目录外置」「启动自检」「/healthz」尚不能完全落地**；批次 4 前只能在开发目录内试运行，不得作为正式上线依据。

## 1. 部署形态

```
浏览器(内网) → IIS ARR 或 nginx（HTTPS，443）
                 ↓ 仅本机回环
              Waitress 127.0.0.1:8080（单进程、8 线程、Windows 服务）
                 ↓
  C:\Apps\payment-progress-system\        代码（服务账号只读）
  C:\ProgramData\PaymentSystem\data\      数据：payment_progress.db + uploads\
  C:\ProgramData\PaymentSystem\backups\   备份：daily/weekly/monthly + 日志
  C:\ProgramData\PaymentSystem\logs\      应用日志
```

容量假设：5~20 并发用户、几百个项目量级；SQLite + 单进程线程足够。**禁止 gunicorn 多进程**访问同一 SQLite 文件（写锁竞争）。

## 2. 目录与账号

| 项 | 建议 |
| --- | --- |
| 代码目录 | `C:\Apps\payment-progress-system`，仅管理员可写；服务账号只读 |
| 数据目录 | `C:\ProgramData\PaymentSystem\data`（不要放在代码目录；Program Files 下不可写） |
| 备份目录 | `C:\ProgramData\PaymentSystem\backups`；有条件的单位放到独立磁盘/NAS（见备份手册） |
| 服务账号 | 专用低权限账号（或 `NT SERVICE\PaymentSystem` 虚拟账号）；**禁止**用个人域账号/Administrator 日常运行 |
| ACL | `icacls` 只授予服务账号 + 管理员对数据/备份/日志目录的修改权限；普通用户只读 |
| 防病毒 | 排除数据目录与备份目录（否则扫描锁文件可能引发 `database is locked`） |

## 3. 安装步骤（一次性）

1. 安装 Python 3.12（勾选 Add to PATH；目标机实测版本记录到验收单）。
2. 部署代码：解压/复制到 `C:\Apps\payment-progress-system`（含 `app/`、`scripts/`、`docs/`、`run.py`）。
3. 创建虚拟环境并安装锁定依赖：

   ```powershell
   cd C:\Apps\payment-progress-system
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install --upgrade pip
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe -m pip install waitress==3.0.2
   .\.venv\Scripts\python.exe -m pip freeze > C:\ProgramData\PaymentSystem\logs\pip-freeze-target.txt
   ```

4. 建数据目录（批次 4 后由配置决定；批次 4 前应用仍写项目内 `instance/`）：

   ```powershell
   New-Item -ItemType Directory -Force C:\ProgramData\PaymentSystem\data, C:\ProgramData\PaymentSystem\backups, C:\ProgramData\PaymentSystem\logs
   ```

5. 初始化数据库（**仅首次**；会建默认账号，随后立即改密）：

   ```powershell
   cd C:\Apps\payment-progress-system
   .\.venv\Scripts\python.exe scripts\init_db.py
   ```

6. 配置环境变量（见 [`config.md`](config.md)），至少设置 `SECRET_KEY`、`DATA_DIR`、`PAYMENT_BACKUP_DIR`。
7. 首次登录后：修改 admin 密码；创建经办/查看账号；删除/停用不需要的示例账号。
8. 按第 5 节注册服务，按第 8 节注册计划任务，按第 6 节配置 HTTPS。

## 4. 运行方式与并发设计

- 生产用 Waitress：`waitress-serve --listen=127.0.0.1:8080 --threads=8 --connection-limit=100 run:app`；
- 单进程多线程，不启多 worker；SQLite 已开 WAL + `busy_timeout=5000`；
- 5~20 人日常录入（短事务）实测可承载；**合同扫描件 OCR 仍在请求内持写事务（阶段 2 修复）**，上线初期约定「同一时段只允许一人上传扫描件」，并在培训中说明保存失败重试；
- 报表/列表当前为内存聚合，项目数超过约 500 后关注页面耗时（阶段 2 SQL 化）。

## 5. 注册为 Windows 服务（NSSM）

```powershell
# 下载 nssm.exe 放到 C:\Tools\nssm\nssm.exe（或使用 WinSW）
$nssm = 'C:\Tools\nssm\nssm.exe'
& $nssm install PaymentSystem 'C:\Apps\payment-progress-system\.venv\Scripts\waitress-serve.exe' `
  '--listen=127.0.0.1:8080 --threads=8 --connection-limit=100 run:app'
& $nssm set PaymentSystem AppDirectory 'C:\Apps\payment-progress-system'
& $nssm set PaymentSystem AppStdout 'C:\ProgramData\PaymentSystem\logs\waitress-out.log'
& $nssm set PaymentSystem AppStderr 'C:\ProgramData\PaymentSystem\logs\waitress-err.log'
& $nssm set PaymentSystem AppRotateFiles 1
& $nssm set PaymentSystem AppRotateOnline 1
& $nssm set PaymentSystem AppRotateBytes 10485760
& $nssm set PaymentSystem Start SERVICE_AUTO_START
# 服务账号与环境变量（示例，值按实际填；密钥不要写进代码仓库）
& $nssm set PaymentSystem ObjectName '.\svc-payment' '密码'
& $nssm set PaymentSystem AppEnvironmentExtra `
  'SECRET_KEY=<32字节随机串>' 'DATA_DIR=C:\ProgramData\PaymentSystem\data' `
  'PAYMENT_BACKUP_DIR=C:\ProgramData\PaymentSystem\backups' 'APP_ENV=production'
& $nssm start PaymentSystem
```

验证：`Get-Service PaymentSystem`、`Invoke-WebRequest http://127.0.0.1:8080/login`、错误日志为空。

## 6. HTTPS（IIS ARR 或 nginx）

- Waitress 不支持 TLS，必须由反向代理终结；代理只监听内网网卡，后端指 `http://127.0.0.1:8080`；
- 证书：优先单位 CA 签发；临时可用自签（仅内网）：
  `New-SelfSignedCertificate -DnsName payment.单位内网域名 -CertStoreLocation Cert:\LocalMachine\My -NotAfter (Get-Date).AddYears(3)`
- 代理需转发 `X-Forwarded-For`/`X-Forwarded-Proto`；应用侧批次 4 加 `ProxyFix` 并把客户端 IP 写入审计（仅信任代理来源）；
- 开启 HSTS（内网自签场景可暂缓）；只放行内网网段 443，关闭 8080 对外。

## 7. 日志与监控

| 项 | 做法 |
| --- | --- |
| 访问日志 | NSSM 重定向 Waitress 输出，按 10MB 轮转（见第 5 节） |
| 应用日志 | 批次 4 增加 `logging` + RotatingFileHandler，写 `C:\ProgramData\PaymentSystem\logs\app.log`，保留 90 天 |
| 健康检查 | 批次 4 增加 `/healthz`（DB 可读 + 数据目录可写 + 版本号）；临时用 `GET /login` 200 判断 |
| 巡检 | 每日计划任务输出：项目数/付款合计/最近备份时间/磁盘剩余到日志；备份脚本失败返回非零退出码，需接监控或人工查看 |
| 服务 | NSSM 自动重启；Windows 事件日志记录服务启停 |

## 8. 计划任务（schtasks）

```powershell
schtasks /Create /TN "PaymentSystem\Backup-1230" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Apps\payment-progress-system\scripts\backup.ps1" /SC DAILY /ST 12:30 /RU "SYSTEM" /RL HIGHEST /F
schtasks /Create /TN "PaymentSystem\Backup-1830" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Apps\payment-progress-system\scripts\backup.ps1" /SC DAILY /ST 18:30 /RU "SYSTEM" /RL HIGHEST /F
schtasks /Query /TN "PaymentSystem\Backup-1230" /V /FO LIST
```

说明：脚本自动读取环境变量（`DATA_DIR`/`PAYMENT_BACKUP_DIR`），服务化后建议在系统环境变量中设置，计划任务以 SYSTEM 运行时同样可见；日志在 `backups\logs\backup-YYYYMMDD.log`。备份细节与恢复演练见 [`backup-restore.md`](backup-restore.md)。

## 9. 启动自检（批次 4 落地，上线前必须生效）

启动失败必须**快速失败并写日志**，不允许带病运行：

| 检查 | 处理 |
| --- | --- |
| `SECRET_KEY` 缺失 / 长度 < 32 / 等于默认值 | **拒绝启动**（否则可伪造会话） |
| `APP_ENV=production` 且 `DEBUG=True` | **拒绝启动** |
| `DATA_DIR` 不存在/不可写、DB 文件不可读 | **拒绝启动**，提示初始化或恢复 |
| `MAX_UPLOAD_MB` 非 1~100 整数 | **拒绝启动** |
| `LLM_ENABLED=true` 但 `CONTRACT_LLM_*` 不完整 | 拒绝启动或降级并 WARN（二选一，配置文档定稿为拒绝） |
| OCR 语言包/引擎不可用 | WARN + 记录；上线验收必须实测扫描件 |
| 备份目录不存在/不可写、最近备份超过 48h | WARN（由部署验收兜底） |

## 10. 上线检查表（目标机验收）

- [ ] 依赖安装完成，`pip freeze` 归档；`python tests/run_qa.py` 退出码 0
- [ ] 服务开机自启、重启后自动恢复；`GET /login` 200
- [ ] HTTPS 可访问，HTTP 自动跳转；仅内网可达 443
- [ ] 默认账号已改密；`SECRET_KEY` 非默认值；DEBUG 关闭
- [ ] 数据目录为 `C:\ProgramData\PaymentSystem\data`；uploads 可写
- [ ] 每日两次备份任务成功，最近一次备份 `manifest.sha256` 校验通过
- [ ] **恢复演练通过**：从备份恢复到临时目录 → `verify_restored.py` 9/9 通过 → 恢复到在线目录（停服务）→ 启动 → 登录/项目/报表正常（见备份手册第 7~8 节）
- [ ] **扫描件 OCR 实测（必做）**：上传 1 份真实扫描件 PDF 与 1 张实拍图片合同，确认识别出文字并进入确认页。注意本机开发环境因 **WinRT 沙箱路径限制（0x80070005）** 无法验证 OCR，目标机必须用真实文件实测；若目标机也失败，记录错误码并回退为「人工录入/文字版合同」流程
- [ ] 并发冒烟：2 人同时录入不同项目、1 人上传扫描件时另一人保存，无 `database is locked` 报错（OCR 期间按第 4 节约定错峰）
- [ ] 浏览器兼容：Edge/Chrome 当前版本；打印/导出样式（阶段 2/3）
- [ ] 运维交接：备份/恢复手册、账号清单、日志位置、服务启停命令

## 11. 升级与回滚

1. 升级前：手动跑一次备份；记录当前 commit/tag；停服务；
2. 替换代码目录（保留 `instance/` 与数据目录不动）；安装新依赖；
3. 执行数据库迁移（Alembic 接入后：`flask db upgrade`）；启动，跑 QA 冒烟；
4. 失败回滚：停服务 → 恢复上一版代码 → 用升级前备份恢复数据库 → 启动 → 验证；
5. 每次升级在 `docs/` 记录变更与验证结果。

## 12. 常见故障

| 现象 | 排查 |
| --- | --- |
| 服务启动即退出 | NSSM 错误日志；配置自检报错；端口 8080 被占 |
| 保存报 `database is locked` | 是否有长事务（OCR/大数据导入）；备份是否在跑；防病毒是否锁文件；`busy_timeout` 是否生效 |
| 上传 413 | 文件超过 `MAX_UPLOAD_MB`（当前实现 16MB，README 写 20MB，批次 4 统一） |
| 图片/扫描件识别失败 | 目标机 OCR 语言包（设置→时间和语言→语言→中文 OCR）；WinRT 错误码 0x80070005 见第 10 节 |
| 登录后立即掉线 | `SECRET_KEY` 每次重启变化（未外置）；多实例部署 |
| 页面金额不对 | 是否导入历史数据口径问题；跑一致性自检脚本（阶段 1 交付） |

# 备份与恢复手册（Windows / SQLite）

- 版本：v1.0（2026-10-23，含 2026-10-22 实测记录）
- 适用：单位内网 Windows 单机/小服务器；并发 5~20 人；SQLite 单文件库
- 交付物：
  - `scripts/backup.ps1`：在线备份 + 代码快照 + 校验清单 + 保留策略
  - `scripts/restore.ps1`：校验 + 恢复 + 现场保护
  - `scripts/sqlite_tool.py`：SQLite 在线备份/校验（供上面两个脚本调用）
  - `scripts/verify_restored.py`：恢复后页面级冒烟校验（只读）
- 原则：**不用直接复制正在写的 .db 文件**（会丢 WAL/不一致）。数据库备份一律走 SQLite Online Backup API。

## 1. 备份目录结构

```
<BackupRoot>\
├── daily\20261022-164947\          # 每次备份一个目录
│   ├── db\payment_progress.db      # 一致性快照（单文件，无 -wal/-shm）
│   ├── uploads\...                 # 合同文件（原样复制）
│   ├── code\source.zip             # 代码快照（不含 instance/）
│   ├── code\baseline-source.zip    # 接手基线包（若存在）
│   ├── backup.json                 # 元数据：时间/主机/路径/大小/行数/schema 指纹/耗时
│   └── manifest.sha256             # 除自身外所有文件的 SHA256 清单
├── weekly\2026-W43_20261022-164947\    # 每周首个成功备份自动提升（保留 N 周）
├── monthly\2026-10_20261022-164947\    # 每月首个成功备份自动提升（保留 N 月）
├── logs\backup-YYYYMMDD.log         # 每次运行的完整日志
└── .tmp\                            # 临时目录；失败现场保留为 FAILED-*，2 天后清理
```

默认位置：环境变量 `PAYMENT_BACKUP_DIR` → 否 `%ProgramData%\PaymentSystem\backups` → 否 `<项目上级目录>\backups`。
默认数据目录：环境变量 `DATA_DIR` → 否 `%ProgramData%\PaymentSystem\data`（存在时）→ 否 `<项目>\instance`。

## 2. 备份用法

```powershell
# 标准备份（读取环境变量/默认目录）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backup.ps1

# 指定目录 + 保留策略（日备 14、周备 8、月备 12）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backup.ps1 `
  -DataDir D:\PSData -BackupRoot E:\PSBackup -KeepDaily 14 -KeepWeekly 8 -KeepMonthly 12

# 仅数据（不拍代码快照，适合高频执行）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backup.ps1 -SkipCodeSnapshot -SkipUploads
```

参数：`-DataDir`、`-BackupRoot`、`-AppDir`、`-KeepDaily`(7)、`-KeepWeekly`(4)、`-KeepMonthly`(12)、`-PythonExe`、`-BaselineZip`、`-SkipCodeSnapshot`、`-SkipUploads`、`-SkipVerify`、`-Quiet`。

退出码：**0=成功，1=备份失败，2=参数/环境错误**。失败时保留 `FAILED-*` 现场、写日志、非零退出（计划任务据此告警）。

流程：并发锁 → 磁盘空间预检（需 DB+uploads+40MB）→ SQLite 在线备份（备份期间业务可读写）→ 复制 uploads → 代码快照 → 写 `backup.json` + `manifest.sha256` → 自检（全部 SHA256 + 数据库完整性/行数/schema 指纹）→ 移动到 `daily\` → 提升周备/月备 → 按保留数清理。

成功输出示例（实测）：

```
[INFO] 数据库快照 49,152 B，integrity=ok，耗时 0.41s
[INFO] 上传文件 2 个 / 1,575,913 B，耗时 0.24s
[INFO] 代码快照 202,591 B（sha256=0d67d2…），baseline-source.zip=True，耗时 0.10s
[INFO] 自检通过（manifest + 数据库完整性/行数）
备份完成
  数据库    : 49,152 B, integrity=ok, 行数={"users":3,"projects":1,"payment_nodes":12,...}
  校验清单  : 7 个文件 SHA256 通过
  耗时      : 5.03s
```

## 3. 计划任务与告警

见 [`deploy.md`](deploy.md) 第 8 节的 `schtasks` 命令（每日 12:30、18:30，SYSTEM 运行）。建议再增加外部巡检：每日读取最新 `daily\` 时间戳，超过 26 小时无新备份即告警；备份脚本非零退出码写入事件日志（可在计划任务后追加 `if ($LASTEXITCODE -ne 0) { Write-EventLog ... }` 包装脚本）。

## 4. 恢复用法

**恢复前必须先停服务**（`Stop-Service PaymentSystem`），否则 SQLite 可能仍持有连接。

```powershell
# 第 1 步：只校验备份（不改任何文件，安全）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restore.ps1 `
  -BackupPath E:\PSBackup\daily\20261022-164947 -TargetDir X -VerifyOnly

# 第 2 步：先恢复到临时目录做演练/核对
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restore.ps1 `
  -BackupPath E:\PSBackup\daily\20261022-164947 -TargetDir D:\RestoreTest
python scripts\verify_restored.py --db D:\RestoreTest\payment_progress.db

# 第 3 步：确认无误后恢复到在线数据目录（会把现场移到 .pre-restore-*，不直接删）
Stop-Service PaymentSystem
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restore.ps1 `
  -BackupPath E:\PSBackup\daily\20261022-164947 -TargetDir C:\ProgramData\PaymentSystem\data -Force
python scripts\verify_restored.py --db C:\ProgramData\PaymentSystem\data\payment_progress.db
Start-Service PaymentSystem
```

恢复脚本行为：
1. 校验 `backup.json` + `manifest.sha256`（任何文件被改/缺失 → 拒绝恢复，退出码 1）；
2. 校验备份库完整性，并与元数据行数、schema 指纹、字节数比对；
3. 拒绝恢复到「在线数据目录」或非空目录（除非 `-Force`），`-Force` 时先把现场移到 `<TargetDir>\.pre-restore-<时间>`；
4. 恢复 db + uploads（`-RestoreCode` 时另恢复代码快照到 `TargetDir\code`）；
5. 恢复后再次校验完整性/行数/uploads 数量与字节数，输出 JSON 摘要。

参数：`-BackupPath`（必填）、`-TargetDir`、`-DataDir`、`-RestoreCode`、`-Force`、`-VerifyOnly`、`-SkipManifestVerify`、`-DbName`、`-PythonExe`。

## 5. 保留策略与容量

- 日备保留 7 份、周备 4 份、月备 12 份（可调）；提升周/月备是完整复制（含 uploads），容量按「单份大小 × (日+周+月份数)」估算；
- 单份实测约 1.9MB（DB 49KB + uploads 1.54MB + 代码约 0.3MB）；合同文件多了以后 uploads 会快速增长，需按实际合同目录大小估算（建议备份盘 ≥ 数据目录 30 倍）；
- 备份目录与数据目录**不要在同一块盘**；重要单位再加一份异机/NAS 副本（脚本不自动异地复制，需计划任务 `robocopy` 或备份软件）。

## 6. 恢复演练（每季度一次，留记录）

1. 选最近一份备份，`restore.ps1 -VerifyOnly` → 通过；
2. 恢复到临时目录 → `verify_restored.py` 全部通过（9/9）；
3. 记录：备份时间、恢复耗时、校验结果、演练人；
4. 生产环境不建议直接演练覆盖；如需验证「从零恢复」，可在备用机按本文完整走一遍。

## 7. 灾难场景怎么救

| 场景 | 处理 |
| --- | --- |
| **数据库文件被误删** | 停服务 → 用最近 `daily\` 备份恢复到数据目录（`-Force`）→ `verify_restored.py` → 启动。丢失窗口 = 距上次备份时间；uploads 只恢复备份内文件 |
| **数据库损坏（打不开/integrity 失败）** | 先把损坏文件改名留证（`payment_progress.db.corrupt-<时间>`）→ 恢复到临时目录校验 → 再恢复上线；如只是 WAL 残留可先尝试用副本打开（`sqlite3`/`sqlite_tool.py info`），不要在原库上反复操作 |
| **误删项目/误改金额** | 无审计前的数据无法追溯，只能整库回退到最近备份（会丢其后所有操作）；**审计+软删除上线后**改为按审计记录定位并定向修复。参见 `docs/review-technical.md` 第 10 节 |
| **备份校验失败/备份被篡改** | 不要恢复该份；换更早的备份；排查备份盘权限/勒索软件；保留损坏备份取证 |
| **备份盘损坏** | 用异机/NAS 副本或 monthly 备份；若全损，只能从生产库（若还在）重新备份 |
| **服务起不来但数据正常** | 先看 `logs\`、NSSM 错误日志；恢复代码快照（`restore.ps1 -RestoreCode` 提取到临时目录后人工替换） |
| **上传文件丢失但 DB 正常** | 从备份 `uploads\` 单独复制回数据目录（文件名按记录 ID 存储，可直接对照） |

## 8. 实测记录（2026-10-22，测试副本，未触碰生产库）

环境：Windows 10.0.17763 / PowerShell 5.1 / Python 3.12.2；测试数据：生产库只读在线快照（49KB）+ 2 个模拟 uploads（1.5MB）。

| 项 | 结果 |
| --- | --- |
| 首次备份 | 成功，内部耗时 **5.03s**（含提升周/月备），产出 7 文件，DB 49,152B，uploads 1,575,913B，代码 202,591B |
| 有并发写入时备份 | 成功，**3.27s**，DB 61,440B，integrity=ok（在线备份期间写入正常提交） |
| 仅校验（VerifyOnly） | 通过，**0.46s** |
| 恢复到原目录（模拟删库） | 成功，**0.69s**，行数/users=3、projects=1、payment_nodes=12 与备份一致 |
| 恢复 + 代码快照到空目录 | 成功，**2.38s**，代码可解压使用 |
| 恢复后冒烟校验 | `verify_restored.py` **9/9 通过**（登录/项目列表/报表/合同/详情） |
| 恢复后完整 e2e（隔离临时服务） | **92/93 通过**；唯一失败项为旧 e2e 断言与当前「已发生业务节点冻结日期」新规则冲突，与恢复无关（用当前代码复现同样结果） |
| 篡改备份文件 | 拒绝恢复，退出码 1，报「哈希不匹配: uploads/scan-test.bin」 |
| 备份库被破坏 | 拒绝恢复，退出码 1，数据库完整性校验失败 |
| 恢复到在线目录（未 -Force） | 拒绝，退出码 1，提示先停服务并加 -Force |
| 数据目录不存在时备份 | 退出码 1，明确报「数据库不存在」 |
| 保留策略 | 日备保留数生效并删除最旧；周备/月备按周/月唯一提升，超保留数清理 |
| -Force 覆盖恢复 | 现场移动到 `.pre-restore-<时间>`，原数据未丢 |

## 9. 已知限制与后续

1. 代码快照是「当前工作目录」的 zip，**不含 git commit 信息**（git 已启用后可把 commit 写入 `backup.json`，P1 改进）；
2. 脚本不自动异机复制，异地副本需另配计划任务/备份软件；
3. 备份含完整业务数据与合同文件，**必须**依赖 BitLocker/ACL 保护，重要场景建议对备份目录再加一层加密（如 7z 加密归档）；
4. 恢复脚本不会自动启停服务，操作人需自行 `Stop-Service`/`Start-Service`；
5. PyMuPDF 的 OCR 相关依赖较重，目标机安装前确认磁盘（见 requirements.txt 注释）；
6. `/healthz`、应用日志、配置外置属于批次 4，部署验收前需补齐。

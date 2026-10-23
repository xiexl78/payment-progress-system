# 部署演练记录（上线前验证）

- 日期：2026-10-23；执行：架构师
- 目的：按 [`deploy.md`](deploy.md) / [`backup-restore.md`](backup-restore.md) 从零走一遍「干净环境 → 起服务 → QA → 备份 → 损坏/删库 → 恢复 → 健康检查 + 冒烟」，记录实测数字与文档错误（已在本次提交中修正）。
- 结论：**文档路径基本可用，QA 达标（213/201/0/12）、备份恢复达标；但目标机首次安装依赖必须先准备离线 wheelhouse（本机 PyPI 下载不可用），且依赖安装的"干净环境"环节未能在本机完整验证——列为上线前必办项。**

## 1. 环境

| 项 | 值 |
| --- | --- |
| OS / Shell | Windows 10.0.17763（中文）/ PowerShell 5.1 |
| Python | 3.12.2（系统解释器建 venv；venv 内为 3.12.2） |
| 演练目录 | `C:\Users\谢小乐\.dsh\bot\workspace\deploy-drill`（代码副本 `app\`，数据 `data\`，备份 `backups\`，venv `venv\`） |
| 部署方式 | `waitress-serve --listen=127.0.0.1:3652 --threads=8 run:app`（工作目录 = 代码副本，`APP_ENV=production`） |
| 数据库 | SQLite，`DATA_DIR` 指向演练目录（与仓库 `instance/` 隔离） |
| 验证脚本 | `tests/run_qa.py`、`scripts/backup.ps1`、`scripts/restore.ps1`、`scripts/verify_restored.py` |

## 2. 依赖安装（关键环节，有阻塞）

| 尝试 | 方法 | 结果 |
| --- | --- | --- |
| 1 | `python -m venv` + `pip install -r requirements-lock.txt`（原始文件，含中文注释） | 立即失败：`UnicodeDecodeError: 'gbk' codec can't decode byte 0xac`（pip 按 cp936 解码无 BOM UTF-8 注释）→ **已把三个 requirements 文件改为纯 ASCII** |
| 2 | 修正后用 `pip install -r requirements-lock.txt` | 失败：pip 解析/下载停滞，600s 无任何包落盘，进程 CPU 空转（多次重试/杀进程后仍复现） |
| 3 | `pip install --no-deps -r requirements-lock.txt` | 同样下载停滞（PyPI 在本环境约 1KB/s 级；waitress 56KB 单独装耗时 67.6s） |
| 4（降级验证） | venv + 把系统 site-packages（382MB，版本与 lock 同源）复制进 venv + 单独装 waitress | 成功：复制 155.6s，waitress 67.6s，`pip check` 通过，`import flask/sqlalchemy/pdfplumber/docx/openpyxl` 正常 |
| 计时参考 | venv 创建 | 9.1s（首次 13.9s） |

> 因此本次只验证了「同一份包集合在 venv 中可运行」，**没有验证 requirements-lock.txt 在全新网络环境可安装**。目标机必须先在可联网机器做 `pip download -r requirements-lock.txt -d wheelhouse`，再离线 `pip install --no-index --find-links wheelhouse -r requirements-lock.txt`；这一步列入上线检查表（已写入 `deploy.md` 第 3 节）。
> 为节省空间/时间，本次未安装 PyMuPDF 大依赖（onnxruntime 等约 200MB+），因此**扫描件 PDF 渲染链路未能在 venv 中验证**；文字版 PDF/Word/TXT 与 OCR 走 Windows OCR 的路径不受影响。

## 3. 起服务与 QA

- 初始化（隔离 DATA_DIR）：`.venv\Scripts\python.exe scripts\init_db.py --data-dir <演练数据目录>`，耗时 3.96s，种子项目/3 个账号创建成功。
- 起服务：waitress 启动后 `/healthz` 返回 200，DB 路径正确；`/login` 200 且页面含 `csrf_token`。
- QA（venv 执行 `tests/run_qa.py`）：

| 运行 | 环境准备 | 结果 | 耗时 |
| --- | --- | --- | --- |
| 首次 | 直接跑（`C:\ProgramData\PaymentSystem` 存在但对沙箱不可写；中文 Windows 默认 GBK I/O） | 196 PASS / 5 FAIL / 12 XFAIL（配置子进程用例输出为空、initdb 用例因继承 `DATA_DIR` 失败） | ~54s |
| 最终 | `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`、`APP_ENV=development`、把 `%ProgramData%` 指到可写目录（目标机上 `ProgramData` 本身可写，无需此步） | **213 项 / PASS 201 / FAIL 0 / XFAIL 12**，退出码 0 | 53.9s |

**结论**：QA 基线达标。中文 Windows 上跑 QA 必须设 UTF-8 I/O 环境变量（QA 子进程会输出中文错误信息，测试脚本按 UTF-8 读文件）；已写入 `deploy.md` 第 10 节。

## 4. 备份（按 backup-restore.md）

命令（代码副本内，Python 用 venv）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backup.ps1 `
  -DataDir <演练数据> -BackupRoot <演练备份> -AppDir <代码副本> `
  -PythonExe <venv>\Scripts\python.exe
```

实测：**退出码 0，脚本内部 7.95s（含提升周/月备），外部总 8.72s**。

| 产物 | 大小 |
| --- | --- |
| DB 快照 `db/payment_progress.db` | 102,400 B（integrity=ok，行数 users=3/projects=1/items=3/nodes=12） |
| `code/source.zip` | 313,342 B |
| uploads | 0 个（演练库无合同文件） |
| `manifest.sha256` | 7 个文件，全部哈希通过 |
| 单份备份合计 | 约 409.7 KB（含 backup.json/pre-reset 元数据） |

## 5. 故障恢复（损坏 + 删库两种场景）

| 场景 | 操作 | 结果 |
| --- | --- | --- |
| **数据库损坏** | 向 DB 第 8192 字节起写入 512B 垃圾 → `sqlite_tool.py verify` 报 `database disk image is malformed`（退出码 1）；生产模式 `create_app()` **仍能启动（exit 0）**——页级损坏未被启动自检拦住；`restore.ps1 -Force` 从最近备份恢复 | 恢复成功：外部 1.05s / 脚本内部 0.63s；`integrity=ok`，行数与备份一致；`verify_restored.py` 首次因未带 CSRF token 登录 403，脚本改为自动抓 token 后 **9/9 通过** |
| **数据库被删除** | 停服务后删除 `payment_progress.db(-wal/-shm)` → 生产模式启动 | **明确 fail-fast：exit 1**，中文报错「数据库不存在：…请先运行 python scripts/init_db.py」 |
| 恢复后起服务 | 用恢复后的数据启动 waitress | `/healthz` **200**，`/login` **200**；`verify_restored.py` 9/9 通过；恢复脚本 1.06s（外部）/0.66s（内部） |

## 6. 演练暴露的问题与已修正项

| # | 问题 | 修正 |
| --- | --- | --- |
| 1 | `requirements*.txt` 含 UTF-8 无 BOM 中文注释，中文 Windows 下 pip 直接崩 | 三个文件改为纯 ASCII 注释，并在文件头写明"必须 ASCII"；`deploy.md` 补充说明 |
| 2 | 依赖安装只能用系统包复制降级，lock 文件未真正验证 | `deploy.md` 第 3 节新增离线 wheelhouse 步骤 + 列入上线检查表（遗留风险见第 7 节） |
| 3 | `deploy.md` 的 QA 命令用系统 `python`，未说明 UTF-8/可写 ProgramData 前置 | 改为 venv 执行并写明 213/201/0/12 与环境变量要求 |
| 4 | 备份/恢复命令用 `python`，SYSTEM 计划任务没有 venv Python | 计划任务与恢复示例统一加 `-PythonExe <venv>\Scripts\python.exe` |
| 5 | `verify_restored.py` 在 CSRF 启用后登录 403、登出仍用 GET | 脚本改为从登录页抓 `csrf_token` 提交、登出改 POST；恢复冒烟恢复 9/9 |
| 6 | 文档称"DB 损坏会拒绝启动" | 实际页级损坏可启动、靠 `/healthz` 503 发现；`deploy.md` 第 9 节更正并提示用备份恢复 |
| 7 | 文档称应用有 `app.log` 轮转 | 实际仅 NSSM 重定向日志（QA P2-09 XFAIL）；文档已改为现状并标待办 |
| 8 | `MAX_UPLOAD_MB` 文档写 16MB/批次 4 统一 | 实际默认 20MB 且可配；文档已更正（413 行） |

## 7. 遗留风险（上线前必须处理/确认）

1. **依赖离线安装未验证**（P0）：目标机必须准备 wheelhouse 并在目标机重跑 `pip install -r requirements-lock.txt` + `pip freeze` 归档；本机 PyPI 不可用属环境限制。
2. **扫描件 OCR 链路**：本机因 WinRT 0x80070005 不可测；目标机必须用真实扫描件/照片实测（同 `deploy.md` 第 10 节）。
3. **页级数据库损坏不被启动自检拦截**（P1）：靠 `/healthz` 503 发现；建议后续在启动自检中加 `PRAGMA quick_check`（会拖慢启动，可设为每日巡检任务）。
4. **代理头配置不一致**（P1）：config 暴露 `TRUSTED_PROXY_COUNT`，审计模块读取的是 `TRUST_PROXY_HEADERS`，HTTPS 反代下审计 IP/UA 可能仍是代理地址；需统一（未改 `app/`，报开发）。
5. **服务进程在本次工具环境不持久**（环境限制，非产品问题）：后台服务需在真实 NSSM 环境验证开机自启/崩溃重启。
6. PyMuPDF 未装导致 venv 与 lock 不完全一致；部署机安装后以 `pip freeze` 为准。

## 8. 验收结论

- 部署文档按步骤可执行（含上述修正后），QA 基线 213/201/0/12 达标；
- 备份 → 损坏/删库 → 恢复 → `/healthz` + 冒烟闭环通过（恢复 1.05~1.06s，冒烟 9/9）；
- 上线前剩余动作：目标机离线安装 + OCR 实测 + NSSM 服务化验证（见第 7 节）。

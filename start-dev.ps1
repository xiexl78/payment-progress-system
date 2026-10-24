$ErrorActionPreference = 'Stop'

$Code = 'C:\Users\谢小乐\.dsh\bot\workspace\payment-system\payment-system'   # 代码目录
$Data = 'C:\Users\谢小乐\.dsh\bot\workspace\payment-system\data-dev'         # 调试数据目录（工作区内，避免 ProgramData 权限/占用问题）
$Old  = 'C:\ProgramData\PaymentSystem\data-dev'                              # 旧数据目录（首次运行自动迁移过来）

New-Item -ItemType Directory -Force $Data, (Join-Path $Data 'logs') | Out-Null

# 首次运行：旧目录有库就迁移过来，没有则稍后初始化
if (-not (Test-Path (Join-Path $Data 'payment_progress.db'))) {
    if (Test-Path (Join-Path $Old 'payment_progress.db')) {
        Write-Host "迁移旧开发库: $Old -> $Data"
        Copy-Item (Join-Path $Old 'payment_progress.db') $Data
        if (Test-Path (Join-Path $Old 'uploads')) {
            Copy-Item (Join-Path $Old 'uploads') (Join-Path $Data 'uploads') -Recurse -Force -ErrorAction SilentlyContinue
        }
    } else {
        Write-Host "初始化开发数据库: $Data"
    }
}

# 调试用固定密钥，不用每次生成
$env:SECRET_KEY = 'dev-only-secret-key-not-for-production-0123456789abcdef'
$env:DATA_DIR   = $Data
$env:APP_ENV    = 'development'

Set-Location $Code

if (-not (Test-Path (Join-Path $Data 'payment_progress.db'))) {
    python scripts\init_db.py --no-seed
}

Write-Host "数据目录: $Data"
Write-Host '启动开发服务器 http://127.0.0.1:8080  (Ctrl+C 停止)'
Write-Host '启动后另开窗口自检:  irm http://127.0.0.1:8080/healthz'
python -m flask --app run run --debug --host=127.0.0.1 --port=8080

$ErrorActionPreference = 'Stop'

$Code = 'C:\Users\谢小乐\.dsh\bot\workspace\payment-system\payment-system'          # 改成你的代码目录
$Data = 'C:\ProgramData\PaymentSystem\data-dev'    # 调试用独立数据目录，别污染正式数据

New-Item -ItemType Directory -Force $Data, 'C:\ProgramData\PaymentSystem\logs' | Out-Null

# 调试用固定密钥，不用每次生成
$env:SECRET_KEY = 'dev-only-secret-key-not-for-production-0123456789abcdef'
$env:DATA_DIR   = $Data
$env:APP_ENV    = 'development'

Set-Location $Code

# 开发库不存在时自动初始化
if (-not (Test-Path (Join-Path $Data 'payment_progress.db'))) {
    Write-Host '初始化开发数据库...'
    python scripts\init_db.py --no-seed
}

Write-Host '启动开发服务器 http://127.0.0.1:8080  (Ctrl+C 停止)'
python -m flask --app run run --debug --host=127.0.0.1 --port=8080
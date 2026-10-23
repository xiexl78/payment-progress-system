#requires -Version 5.1
<#
.SYNOPSIS
  付款进度管理系统 - 在线备份脚本（Windows / PowerShell 5.1+）

.DESCRIPTION
  1) 使用 SQLite 在线备份 API（scripts/sqlite_tool.py backup）生成一致性数据库快照，
     不直接复制正在写入的库文件，备份期间业务可继续使用；
  2) 复制 instance/uploads 合同文件；
  3) 生成代码快照（含 baseline-source.zip，若存在）；
  4) 生成 backup.json 元数据 + manifest.sha256 校验清单，并立即自检（哈希 + 数据库校验）；
  5) 保留策略：日备 N 份、每周首份自动提升为周备（保留 N 周）、每月首份提升为月备；
  6) 失败返回非零退出码（0=成功，1=备份失败，2=参数/环境错误）。

.PARAMETER DataDir
  数据目录（含 payment_progress.db 与 uploads）。默认：$env:PAYMENT_DATA_DIR，
  其次 %ProgramData%\PaymentSystem\data（若存在），最后 <AppDir>\instance。

.PARAMETER BackupRoot
  备份根目录。默认：$env:PAYMENT_BACKUP_DIR，其次 %ProgramData%\PaymentSystem\backups，
  最后 <AppDir>\..\backups（仓库外，避免代码目录只读/被覆盖）。

.PARAMETER AppDir
  应用根目录（代码快照来源）。默认：本脚本的上级目录。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\backup.ps1
  powershell -ExecutionPolicy Bypass -File scripts\backup.ps1 -DataDir D:\PSData -BackupRoot E:\PSBackup -KeepDaily 14
#>
[CmdletBinding()]
param(
    [string]$DataDir = $env:PAYMENT_DATA_DIR,
    [string]$BackupRoot = $env:PAYMENT_BACKUP_DIR,
    [string]$AppDir = '',
    [int]$KeepDaily = 7,
    [int]$KeepWeekly = 4,
    [int]$KeepMonthly = 12,
    [string]$PythonExe = 'python',
    [string]$BaselineZip = '',
    [switch]$SkipCodeSnapshot,
    [switch]$SkipUploads,
    [switch]$SkipVerify,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$script:Quiet = [bool]$Quiet
$script:LogFile = $null
$script:LockStream = $null
$script:LockWriter = $null

# 让 Python 输出统一为 UTF-8，避免中文路径在日志里乱码
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = '[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    if (-not $script:Quiet) { Write-Host $line }
    if ($script:LogFile) {
        try { Add-Content -Path $script:LogFile -Value $line -Encoding UTF8 } catch { }
    }
}

function Write-Utf8NoBom {
    param([string]$Path, [string[]]$Lines)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, $Lines, $encoding)
}

function Get-IsoWeekLabel {
    param([datetime]$Date)
    # 取该周周四所在年份计算 ISO 周号（周一为一周开始）
    $mondayIndex = (([int]$Date.DayOfWeek) + 6) % 7
    $thursday = $Date.AddDays(3 - $mondayIndex)
    $year = $thursday.Year
    $jan1 = Get-Date -Year $year -Month 1 -Day 1 -Hour 0 -Minute 0 -Second 0
    $week = [int][Math]::Floor((($thursday - $jan1).TotalDays) / 7) + 1
    return ('{0}-W{1:00}' -f $year, $week)
}

function Invoke-PythonTool {
    param([string[]]$Arguments, [string]$OutFile, [string]$ErrFile)
    & $PythonExe @Arguments 1> $OutFile 2> $ErrFile
    return $LASTEXITCODE
}

function Get-DirectoryStats {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return @{ Files = 0; Bytes = 0 } }
    $items = @(Get-ChildItem -Path $Path -Recurse -File -ErrorAction SilentlyContinue)
    $sum = 0
    foreach ($item in $items) { $sum += $item.Length }
    return @{ Files = $items.Count; Bytes = $sum }
}

function Write-Manifest {
    param([string]$Root)
    $files = @(Get-ChildItem -Path $Root -Recurse -File | Where-Object { $_.Name -ne 'manifest.sha256' } | Sort-Object FullName)
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($Root.Length).TrimStart('\', '/').Replace('\', '/')
        $hash = (Get-FileHash -Path $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $lines.Add(('{0}  {1}' -f $hash, $relative))
    }
    Write-Utf8NoBom -Path (Join-Path $Root 'manifest.sha256') -Lines $lines.ToArray()
    return $files.Count
}

function Test-Manifest {
    param([string]$Root)
    $manifestPath = Join-Path $Root 'manifest.sha256'
    if (-not (Test-Path $manifestPath)) { return @{ Ok = $false; Errors = @('manifest.sha256 不存在') } }
    $errors = New-Object System.Collections.Generic.List[string]
    $checked = 0
    foreach ($line in (Get-Content -Path $manifestPath -Encoding UTF8)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch '^([0-9a-fA-F]{64})\s\s(.+)$') { $errors.Add(('无法解析清单行: {0}' -f $line)); continue }
        $expected = $Matches[1].ToLowerInvariant()
        $relative = $Matches[2]
        $full = Join-Path $Root ($relative.Replace('/', '\'))
        if (-not (Test-Path $full)) { $errors.Add(('清单中的文件缺失: {0}' -f $relative)); continue }
        $actual = (Get-FileHash -Path $full -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) { $errors.Add(('哈希不匹配: {0}' -f $relative)) }
        $checked++
    }
    $onDisk = @(Get-ChildItem -Path $Root -Recurse -File | Where-Object { $_.Name -ne 'manifest.sha256' }).Count
    if ($onDisk -ne $checked) { $errors.Add(('清单文件数 {0} 与实际文件数 {1} 不一致' -f $checked, $onDisk)) }
    return @{ Ok = ($errors.Count -eq 0); Errors = $errors.ToArray(); Checked = $checked }
}

function Remove-OldBackups {
    param([string]$Root, [int]$Keep, [string]$Pattern)
    if ($Keep -le 0) { return 0 }
    if (-not (Test-Path $Root)) { return 0 }
    $items = @(Get-ChildItem -Path $Root -Directory |
        Where-Object { $_.Name -match $Pattern } |
        Sort-Object Name -Descending)
    $removed = 0
    if ($items.Count -gt $Keep) {
        for ($i = $Keep; $i -lt $items.Count; $i++) {
            try {
                Remove-Item -Path $items[$i].FullName -Recurse -Force
                Write-Log ("保留策略：删除过期备份 {0}" -f $items[$i].FullName)
                $removed++
            } catch {
                Write-Log ("保留策略：删除失败 {0}: {1}" -f $items[$i].FullName, $_.Exception.Message) 'WARN'
            }
        }
    }
    return $removed
}

# ---------------- 参数与路径解析 ----------------
if (-not $AppDir) { $AppDir = Split-Path -Parent $PSScriptRoot }
$AppDir = [System.IO.Path]::GetFullPath($AppDir)
if (-not (Test-Path (Join-Path $AppDir 'run.py'))) {
    Write-Host "ERROR: AppDir 不像项目根目录（缺少 run.py）: $AppDir" -ForegroundColor Red
    exit 2
}

if (-not $DataDir) {
    $programDataData = Join-Path $env:ProgramData 'PaymentSystem\data'
    if (Test-Path $programDataData) { $DataDir = $programDataData }
    else { $DataDir = Join-Path $AppDir 'instance' }
}
$DataDir = [System.IO.Path]::GetFullPath($DataDir)

if (-not $BackupRoot) {
    if ($env:PAYMENT_BACKUP_DIR) {
        $BackupRoot = $env:PAYMENT_BACKUP_DIR
    } elseif (Test-Path (Join-Path $env:ProgramData 'PaymentSystem')) {
        $BackupRoot = Join-Path $env:ProgramData 'PaymentSystem\backups'
    } else {
        $BackupRoot = Join-Path (Split-Path -Parent $AppDir) 'backups'
    }
}
$BackupRoot = [System.IO.Path]::GetFullPath($BackupRoot)

$dbPath = Join-Path $DataDir 'payment_progress.db'
$uploadsDir = Join-Path $DataDir 'uploads'
$toolPath = Join-Path $AppDir 'scripts\sqlite_tool.py'

$exitCode = 0
$startedAt = Get-Date
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$dailyRoot = Join-Path $BackupRoot 'daily'
$weeklyRoot = Join-Path $BackupRoot 'weekly'
$monthlyRoot = Join-Path $BackupRoot 'monthly'
$logsRoot = Join-Path $BackupRoot 'logs'
$tmpRoot = Join-Path $BackupRoot '.tmp'
$stage = $null
$finalDir = $null
$lockPath = $null

foreach ($dir in @($BackupRoot, $dailyRoot, $weeklyRoot, $monthlyRoot, $logsRoot, $tmpRoot)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}
$script:LogFile = Join-Path $logsRoot ('backup-{0}.log' -f (Get-Date -Format 'yyyyMMdd'))

try {
    Write-Log "===== 开始备份 ====="
    Write-Log "AppDir=$AppDir"
    Write-Log "DataDir=$DataDir"
    Write-Log "BackupRoot=$BackupRoot"

    if (-not (Test-Path $dbPath)) { throw ("数据库不存在: {0}" -f $dbPath) }
    if (-not (Test-Path $toolPath)) { throw ("缺少 tools 脚本: {0}" -f $toolPath) }

    # 并发锁（避免两个备份同时写同一目录）
    $lockPath = Join-Path $BackupRoot '.backup.lock'
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        try {
            $script:LockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
            break
        } catch {
            $stale = $true
            if (Test-Path $lockPath) {
                try {
                    $lockText = (Get-Content -Path $lockPath -TotalCount 1 -ErrorAction Stop)
                    if ($lockText -match 'pid=(\d+)') {
                        $other = Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue
                        if ($other) { $stale = $false }
                    }
                } catch { $stale = $true }
            }
            if ($stale) {
                Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue
            } else {
                throw ("另一个备份进程正在运行（锁文件: {0}），本次放弃" -f $lockPath)
            }
        }
    }
    if (-not $script:LockStream) { throw '无法获取备份锁' }
    $script:LockWriter = New-Object System.IO.StreamWriter($script:LockStream)
    $script:LockWriter.WriteLine(('pid={0} started={1}' -f $PID, (Get-Date).ToString('o')))
    $script:LockWriter.Flush()

    # 磁盘空间预检
    $dbBytes = (Get-Item -Path $dbPath).Length
    $uploadStats = Get-DirectoryStats -Path $uploadsDir
    $neededBytes = $dbBytes + $uploadStats.Bytes + 40MB
    $driveRoot = [System.IO.Path]::GetPathRoot($BackupRoot)
    $freeBytes = ([System.IO.DriveInfo]::new($driveRoot)).AvailableFreeSpace
    Write-Log ("空间检查：需要约 {0:N1} MB，备份盘可用 {1:N1} MB" -f ($neededBytes / 1MB), ($freeBytes / 1MB))
    if ($freeBytes -lt $neededBytes) {
        throw ("备份磁盘空间不足：需要约 {0:N1} MB，可用 {1:N1} MB" -f ($neededBytes / 1MB), ($freeBytes / 1MB))
    }

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $stage = Join-Path $tmpRoot $timestamp
    if (Test-Path $stage) { Remove-Item -Path $stage -Recurse -Force }
    $dbDir = Join-Path $stage 'db'
    $uploadsStage = Join-Path $stage 'uploads'
    $codeStage = Join-Path $stage 'code'
    New-Item -ItemType Directory -Path $dbDir, $uploadsStage, $codeStage -Force | Out-Null

    # 1) 数据库在线备份
    $dbTarget = Join-Path $dbDir 'payment_progress.db'
    $dbOut = Join-Path $stage 'db-tool-out.json'
    $dbErr = Join-Path $stage 'db-tool-err.txt'
    $dbWatch = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Log '1/4 正在做数据库在线备份（SQLite Backup API）...'
    $dbCode = Invoke-PythonTool -Arguments @($toolPath, 'backup', '--src', $dbPath, '--dst', $dbTarget) -OutFile $dbOut -ErrFile $dbErr
    $dbWatch.Stop()
    $dbErrText = ''
    if (Test-Path $dbErr) { $dbErrText = (Get-Content -Path $dbErr -Raw -Encoding UTF8) }
    if ($dbCode -ne 0) {
        throw ("数据库在线备份失败（退出码 {0}）: {1}" -f $dbCode, $dbErrText)
    }
    $dbInfo = (Get-Content -Path $dbOut -Raw -Encoding UTF8) | ConvertFrom-Json
    if (-not $dbInfo.ok) {
        throw ("数据库备份完整性校验失败: {0}" -f $dbOut)
    }
    if (Test-Path $dbErr) { Remove-Item -Path $dbErr -Force -ErrorAction SilentlyContinue }
    Write-Log ("    数据库快照 {0:N0} B，integrity={1}，耗时 {2:N2}s" -f $dbInfo.bytes, $dbInfo.integrity, $dbWatch.Elapsed.TotalSeconds)

    # 2) 上传文件
    $uploadsWatch = [System.Diagnostics.Stopwatch]::StartNew()
    $copiedUploadFiles = 0
    $copiedUploadBytes = 0
    if ($SkipUploads) {
        Write-Log '2/4 已按要求跳过 uploads 复制'
        Remove-Item -Path $uploadsStage -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Log '2/4 正在复制 uploads 合同文件...'
        if (Test-Path $uploadsDir) {
            $null = robocopy $uploadsDir $uploadsStage /E /NFL /NDL /NJH /NJS /NP
            if ($LASTEXITCODE -ge 8) { throw ("复制 uploads 失败（robocopy 退出码 {0}）" -f $LASTEXITCODE) }
        }
        $uploadsWatch.Stop()
        $copied = Get-DirectoryStats -Path $uploadsStage
        $copiedUploadFiles = $copied.Files
        $copiedUploadBytes = $copied.Bytes
        Write-Log ("    上传文件 {0} 个 / {1:N0} B，耗时 {2:N2}s" -f $copiedUploadFiles, $copiedUploadBytes, $uploadsWatch.Elapsed.TotalSeconds)
    }
    $uploadsWatch.Stop()

    # 3) 代码快照
    $codeWatch = [System.Diagnostics.Stopwatch]::StartNew()
    $codeZip = Join-Path $codeStage 'source.zip'
    $codeZipSha = ''
    $baselineCopied = $false
    if ($SkipCodeSnapshot) {
        Write-Log '3/4 已按要求跳过代码快照'
        Remove-Item -Path $codeStage -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Log '3/4 正在生成代码快照...'
        $codeTmp = Join-Path $stage '.code-stage'
        $null = robocopy $AppDir $codeTmp /E /XD instance __pycache__ .git .venv venv backups .pytest_cache /XF *.pyc *.log /NFL /NDL /NJH /NJS /NP
        if ($LASTEXITCODE -ge 8) { throw ("代码快照复制失败（robocopy 退出码 {0}）" -f $LASTEXITCODE) }
        $codeWatch.Stop()
        Compress-Archive -Path (Join-Path $codeTmp '*') -DestinationPath $codeZip -CompressionLevel Optimal -Force
        Remove-Item -Path $codeTmp -Recurse -Force -ErrorAction SilentlyContinue
        $codeZipSha = (Get-FileHash -Path $codeZip -Algorithm SHA256).Hash.ToLowerInvariant()

        $baselineSource = $BaselineZip
        if (-not $baselineSource) { $baselineSource = Join-Path (Split-Path -Parent $AppDir) 'baseline-source.zip' }
        if ($baselineSource -and (Test-Path $baselineSource)) {
            Copy-Item -Path $baselineSource -Destination (Join-Path $codeStage 'baseline-source.zip') -Force
            $baselineCopied = $true
        }
        Write-Log ("    代码快照 {0:N0} B（sha256={1}），baseline-source.zip={2}，耗时 {3:N2}s" -f (Get-Item $codeZip).Length, $codeZipSha, $baselineCopied, $codeWatch.Elapsed.TotalSeconds)
    }
    $codeWatch.Stop()

    # 4) 元数据 + 校验清单
    Write-Log '4/4 正在生成 backup.json 与 manifest.sha256...'
    $codeZipBytes = 0
    if (-not $SkipCodeSnapshot) { $codeZipBytes = [long](Get-Item $codeZip).Length }
    $meta = [ordered]@{
        tool            = 'backup.ps1'
        tool_version    = '1.0'
        created_at      = (Get-Date).ToString('o')
        host            = $env:COMPUTERNAME
        user            = $env:USERNAME
        app_dir         = $AppDir
        data_dir        = $DataDir
        backup_root     = $BackupRoot
        db              = [ordered]@{
            path                  = 'db/payment_progress.db'
            bytes                 = [long]$dbInfo.bytes
            integrity             = [string]$dbInfo.integrity
            quick_check           = [string]$dbInfo.quick_check
            schema_sha256         = [string]$dbInfo.schema_sha256
            counts                = $dbInfo.counts
            source_bytes          = [long]$dbInfo.source.bytes
            source_may_have_changed = [bool]$dbInfo.source_may_have_changed
            seconds               = [double]$dbInfo.seconds
        }
        uploads         = [ordered]@{
            dir   = 'uploads/'
            files = [int]$copiedUploadFiles
            bytes = [long]$copiedUploadBytes
            skipped = [bool]$SkipUploads
        }
        code            = [ordered]@{
            source_zip        = 'code/source.zip'
            source_zip_bytes  = [long]$codeZipBytes
            source_zip_sha256 = [string]$codeZipSha
            baseline_zip      = [bool]$baselineCopied
            skipped           = [bool]$SkipCodeSnapshot
        }
        environment     = [ordered]@{
            powershell = $PSVersionTable.PSVersion.ToString()
            python     = $PythonExe
        }
        timings_seconds = [ordered]@{
            db      = [math]::Round($dbWatch.Elapsed.TotalSeconds, 3)
            uploads = [math]::Round($uploadsWatch.Elapsed.TotalSeconds, 3)
            code    = [math]::Round($codeWatch.Elapsed.TotalSeconds, 3)
        }
    }
    $jsonText = ($meta | ConvertTo-Json -Depth 8)
    Write-Utf8NoBom -Path (Join-Path $stage 'backup.json') -Lines @($jsonText)

    $fileCount = Write-Manifest -Root $stage

    # 自检：清单哈希 + 数据库一致性（默认开启）
    if (-not $SkipVerify) {
        Write-Log ("    正在自检 {0} 个文件的 SHA256..." -f $fileCount)
        $manifestCheck = Test-Manifest -Root $stage
        if (-not $manifestCheck.Ok) {
            throw ("备份自检失败: {0}" -f ($manifestCheck.Errors -join '; '))
        }
        $verifyOut = Join-Path $stage 'verify-out.json'
        $verifyErr = Join-Path $stage 'verify-err.txt'
        $verifyCode = Invoke-PythonTool -Arguments @($toolPath, 'verify', '--db', $dbTarget, '--expect-json', (Join-Path $stage 'backup.json')) -OutFile $verifyOut -ErrFile $verifyErr
        if ($verifyCode -ne 0) {
            $verifyErrText = ''
            if (Test-Path $verifyErr) { $verifyErrText = (Get-Content -Path $verifyErr -Raw -Encoding UTF8) }
            $detail = $verifyErrText
            if (Test-Path $verifyOut) {
                try {
                    $vj = (Get-Content -Path $verifyOut -Raw -Encoding UTF8) | ConvertFrom-Json
                    if ($vj -and $vj.mismatches) { $detail = ($vj.mismatches -join '; ') }
                } catch { }
            }
            throw ("备份数据库复核失败（退出码 {0}）: {1}" -f $verifyCode, $detail)
        }
        Remove-Item -Path $verifyOut, $verifyErr -Force -ErrorAction SilentlyContinue
        Write-Log '    自检通过（manifest + 数据库完整性/行数）'
    }

    # 收尾：移动到 daily
    $finalName = $timestamp
    $finalDir = Join-Path $dailyRoot $finalName
    if (Test-Path $finalDir) {
        $suffix = 1
        while (Test-Path (Join-Path $dailyRoot ('{0}-{1}' -f $timestamp, $suffix))) { $suffix++ }
        $finalName = '{0}-{1}' -f $timestamp, $suffix
        $finalDir = Join-Path $dailyRoot $finalName
    }
    Move-Item -Path $stage -Destination $finalDir
    $stage = $null

    # 保留策略：日备 / 周备 / 月备（此段失败只告警，不影响本次备份的有效性）
    $removedDaily = 0
    $removedWeekly = 0
    $removedMonthly = 0
    try {
        $removedDaily = Remove-OldBackups -Root $dailyRoot -Keep $KeepDaily -Pattern '^\d{8}-\d{6}(-\d+)?$'
        $weekLabel = Get-IsoWeekLabel -Date (Get-Date)
        $weeklyExisting = @(Get-ChildItem -Path $weeklyRoot -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like ($weekLabel + '_*') })
        if ($weeklyExisting.Count -eq 0) {
            $weeklyTarget = Join-Path $weeklyRoot ('{0}_{1}' -f $weekLabel, $finalName)
            Copy-Item -Path $finalDir -Destination $weeklyTarget -Recurse -Force
            Write-Log ("保留策略：本周首个备份已提升为周备 {0}" -f $weeklyTarget)
        }
        $removedWeekly = Remove-OldBackups -Root $weeklyRoot -Keep $KeepWeekly -Pattern '^\d{4}-W\d{2}_.+$'
        $monthLabel = Get-Date -Format 'yyyy-MM'
        $monthlyExisting = @(Get-ChildItem -Path $monthlyRoot -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like ($monthLabel + '_*') })
        if ($monthlyExisting.Count -eq 0) {
            $monthlyTarget = Join-Path $monthlyRoot ('{0}_{1}' -f $monthLabel, $finalName)
            Copy-Item -Path $finalDir -Destination $monthlyTarget -Recurse -Force
            Write-Log ("保留策略：本月首个备份已提升为月备 {0}" -f $monthlyTarget)
        }
        $removedMonthly = Remove-OldBackups -Root $monthlyRoot -Keep $KeepMonthly -Pattern '^\d{4}-\d{2}_.+$'
    } catch {
        Write-Log ("保留策略执行失败（本次备份仍有效）: {0}" -f $_.Exception.Message) 'WARN'
    }
    # 清理 2 天前的残留临时目录
    foreach ($oldTmp in @(Get-ChildItem -Path $tmpRoot -Directory -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-2) })) {
        Remove-Item -Path $oldTmp.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }

    $totalSeconds = [math]::Round($stopwatch.Elapsed.TotalSeconds, 2)
    $result = [ordered]@{
        ok             = $true
        backup_dir     = $finalDir
        db_bytes       = [long]$dbInfo.bytes
        db_integrity   = [string]$dbInfo.integrity
        counts         = $dbInfo.counts
        uploads_files  = [int]$copiedUploadFiles
        uploads_bytes  = [long]$copiedUploadBytes
        code_zip_bytes = [long]$codeZipBytes
        manifest_files = $fileCount
        seconds        = $totalSeconds
        removed        = [ordered]@{ daily = $removedDaily; weekly = $removedWeekly; monthly = $removedMonthly }
    }
    Write-Log ("===== 备份成功：{0}（{1:N2}s）=====" -f $finalDir, $totalSeconds)
    if (-not $script:Quiet) {
        Write-Host ''
        Write-Host '备份完成' -ForegroundColor Green
        Write-Host ("  目录      : {0}" -f $finalDir)
        Write-Host ("  数据库    : {0:N0} B, integrity={1}, 行数={2}" -f $dbInfo.bytes, $dbInfo.integrity, (($dbInfo.counts | ConvertTo-Json -Compress)))
        Write-Host ("  上传文件  : {0} 个 / {1:N0} B" -f $copiedUploadFiles, $copiedUploadBytes)
        Write-Host ("  代码快照  : {0:N0} B" -f $result.code_zip_bytes)
        Write-Host ("  校验清单  : {0} 个文件 SHA256 通过" -f $fileCount)
        Write-Host ("  耗时      : {0:N2}s" -f $totalSeconds)
        Write-Host ("  保留删除  : daily={0}, weekly={1}, monthly={2}" -f $removedDaily, $removedWeekly, $removedMonthly)
    }
    $result | ConvertTo-Json -Depth 6 -Compress
}
catch {
    $exitCode = 1
    $message = $_.Exception.Message
    Write-Log ("备份失败: {0}" -f $message) 'ERROR'
    if ($_.ScriptStackTrace) { Write-Log ("调用栈: {0}" -f $_.ScriptStackTrace) 'ERROR' }
    if (-not $script:Quiet) {
        Write-Host ''
        Write-Host ("备份失败: {0}" -f $message) -ForegroundColor Red
        Write-Host ("日志: {0}" -f $script:LogFile)
    }
    # 保留失败现场便于排查（不删除 stage），但打上 FAILED 标记
    if ($stage -and (Test-Path $stage)) {
        try {
            $failedDir = Join-Path $tmpRoot ('FAILED-' + (Split-Path -Leaf $stage))
            Move-Item -Path $stage -Destination $failedDir -Force
            Write-Log ("失败现场已保留: {0}" -f $failedDir) 'WARN'
        } catch { }
    }
}
finally {
    if ($script:LockWriter) { try { $script:LockWriter.Dispose() } catch { } }
    if ($script:LockStream) { try { $script:LockStream.Dispose() } catch { } }
    if ($lockPath -and (Test-Path $lockPath)) { Remove-Item -Path $lockPath -Force -ErrorAction SilentlyContinue }
}

exit $exitCode

#requires -Version 5.1
<#
.SYNOPSIS
  付款进度管理系统 - 备份恢复脚本（Windows / PowerShell 5.1+）

.DESCRIPTION
  从 backup.ps1 生成的备份目录恢复数据（数据库 + uploads，可选代码快照）。

  行为：
  1) 校验 backup.json 与 manifest.sha256（任何文件被改动/缺失都会拒绝恢复）；
  2) 校验备份数据库完整性，并与 backup.json 记录的行数比对；
  3) 安全检查：默认拒绝覆盖"在线数据目录"或非空目录（需显式 -Force）；
  4) -Force 覆盖时，先把现场移动到 <TargetDir>\.pre-restore-<时间>，不直接删除；
  5) 恢复后再次校验数据库完整性/行数/uploads 文件数，输出 JSON 摘要。

  退出码：0 成功 / 1 校验或恢复失败 / 2 参数错误。

.EXAMPLE
  # 只校验备份，不改任何文件
  powershell -ExecutionPolicy Bypass -File scripts\restore.ps1 -BackupPath E:\PSBackup\daily\20261022-170000 -TargetDir X -VerifyOnly

.EXAMPLE
  # 恢复到临时目录（演练）
  powershell -ExecutionPolicy Bypass -File scripts\restore.ps1 -BackupPath E:\PSBackup\daily\20261022-170000 -TargetDir D:\RestoreTest

.EXAMPLE
  # 停服务后恢复到在线数据目录（先备份现场）
  powershell -ExecutionPolicy Bypass -File scripts\restore.ps1 -BackupPath E:\PSBackup\daily\... -TargetDir C:\ProgramData\PaymentSystem\data -Force
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupPath,
    [string]$TargetDir = '',
    [string]$DataDir = $env:PAYMENT_DATA_DIR,
    [string]$AppDir = '',
    [string]$PythonExe = 'python',
    [string]$DbName = 'payment_progress.db',
    [switch]$RestoreCode,
    [switch]$Force,
    [switch]$VerifyOnly,
    [switch]$SkipManifestVerify,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$script:Quiet = [bool]$Quiet
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = '[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    if (-not $script:Quiet) { Write-Host $line }
}

function Get-DirectoryStats {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return @{ Files = 0; Bytes = 0 } }
    $items = @(Get-ChildItem -Path $Path -Recurse -File -ErrorAction SilentlyContinue)
    $sum = 0
    foreach ($item in $items) { $sum += $item.Length }
    return @{ Files = $items.Count; Bytes = $sum }
}

function Test-Manifest {
    param([string]$Root)
    $manifestPath = Join-Path $Root 'manifest.sha256'
    if (-not (Test-Path $manifestPath)) { return @{ Ok = $false; Checked = 0; Errors = @('manifest.sha256 不存在') } }
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
    return @{ Ok = ($errors.Count -eq 0); Checked = $checked; Errors = $errors.ToArray() }
}

function Invoke-PythonTool {
    param([string[]]$Arguments, [string]$OutFile, [string]$ErrFile)
    & $PythonExe @Arguments 1> $OutFile 2> $ErrFile
    return $LASTEXITCODE
}

function Resolve-FullPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Get-LiveDataDirs {
    $dirs = New-Object System.Collections.Generic.List[string]
    if ($env:PAYMENT_DATA_DIR) { $dirs.Add((Resolve-FullPath $env:PAYMENT_DATA_DIR)) }
    $programDataData = Join-Path $env:ProgramData 'PaymentSystem\data'
    if (Test-Path $programDataData) { $dirs.Add((Resolve-FullPath $programDataData)) }
    if ($AppDir) { $dirs.Add((Resolve-FullPath (Join-Path $AppDir 'instance'))) }
    return $dirs
}

# ---------------- 参数与路径 ----------------
if (-not $AppDir) { $AppDir = Split-Path -Parent $PSScriptRoot }
$AppDir = [System.IO.Path]::GetFullPath($AppDir)
$BackupPath = Resolve-FullPath $BackupPath
$toolPath = Join-Path $AppDir 'scripts\sqlite_tool.py'

if (-not (Test-Path $BackupPath)) {
    Write-Host ("ERROR: 备份目录不存在: {0}" -f $BackupPath) -ForegroundColor Red
    exit 2
}
$backupJsonPath = Join-Path $BackupPath 'backup.json'
$backupDb = Join-Path $BackupPath ('db\' + $DbName)
if (-not (Test-Path $backupJsonPath)) {
    Write-Host ("ERROR: 不是有效的备份目录（缺少 backup.json）: {0}" -f $BackupPath) -ForegroundColor Red
    exit 2
}
if (-not (Test-Path $backupDb)) {
    Write-Host ("ERROR: 备份中缺少数据库文件: {0}" -f $backupDb) -ForegroundColor Red
    exit 2
}
$backupMeta = (Get-Content -Path $backupJsonPath -Raw -Encoding UTF8) | ConvertFrom-Json

if (-not $VerifyOnly -and -not $TargetDir) {
    Write-Host 'ERROR: 恢复必须指定 -TargetDir（只校验请加 -VerifyOnly）' -ForegroundColor Red
    exit 2
}

$exitCode = 0
$startedAt = Get-Date
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$tmpCheck = Join-Path ([System.IO.Path]::GetTempPath()) ('payment-restore-check-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmpCheck -Force | Out-Null

try {
    Write-Log '===== 开始备份校验 / 恢复 ====='
    Write-Log ("备份目录: {0}" -f $BackupPath)
    Write-Log ("备份时间: {0}（host={1}）" -f $backupMeta.created_at, $backupMeta.host)

    # 1) manifest 校验
    if (-not $SkipManifestVerify) {
        Write-Log '1/4 校验 manifest.sha256（备份是否被改动/缺失）...'
        $manifest = Test-Manifest -Root $BackupPath
        if (-not $manifest.Ok) {
            throw ("备份校验失败（清单不一致）: {0}" -f ($manifest.Errors -join '; '))
        }
        Write-Log ("    {0} 个文件哈希一致" -f $manifest.Checked)
    } else {
        Write-Log '1/4 已跳过 manifest 校验（-SkipManifestVerify）'
    }

    # 2) 备份数据库校验（完整性 + 行数/schema 与 backup.json 比对）
    Write-Log '2/4 校验备份数据库完整性与行数...'
    $verifyOut = Join-Path $tmpCheck 'verify-out.json'
    $verifyErr = Join-Path $tmpCheck 'verify-err.txt'
    $verifyCode = Invoke-PythonTool -Arguments @($toolPath, 'verify', '--db', $backupDb, '--expect-json', $backupJsonPath) -OutFile $verifyOut -ErrFile $verifyErr
    $verifyErrText = ''
    if (Test-Path $verifyErr) { $verifyErrText = (Get-Content -Path $verifyErr -Raw -Encoding UTF8) }
    $backupDbInfo = (Get-Content -Path $verifyOut -Raw -Encoding UTF8) | ConvertFrom-Json
    if ($verifyCode -ne 0) {
        $detail = $verifyErrText
        if ($backupDbInfo -and $backupDbInfo.mismatches) { $detail = ($backupDbInfo.mismatches -join '; ') }
        throw ("备份数据库校验失败: {0}" -f $detail)
    }
    Write-Log ("    integrity={0}，行数与元数据一致" -f $backupDbInfo.integrity)

    # uploads 清单比对
    $backupUploads = Join-Path $BackupPath 'uploads'
    $backupUploadStats = Get-DirectoryStats -Path $backupUploads
    $expectFiles = [int]$backupMeta.uploads.files
    $expectBytes = [long]$backupMeta.uploads.bytes
    if ($backupUploadStats.Files -ne $expectFiles -or $backupUploadStats.Bytes -ne $expectBytes) {
        throw ("uploads 与 backup.json 不一致：备份内 {0} 个/{1} B，元数据 {2} 个/{3} B" -f $backupUploadStats.Files, $backupUploadStats.Bytes, $expectFiles, $expectBytes)
    }
    Write-Log ("    uploads {0} 个文件 / {1:N0} B 一致" -f $backupUploadStats.Files, $backupUploadStats.Bytes)

    if ($VerifyOnly) {
        $totalSeconds = [math]::Round($stopwatch.Elapsed.TotalSeconds, 2)
        $summary = [ordered]@{
            ok            = $true
            mode          = 'verify-only'
            backup_dir    = $BackupPath
            backup_time   = $backupMeta.created_at
            db_bytes      = [long]$backupDbInfo.bytes
            db_integrity  = [string]$backupDbInfo.integrity
            counts        = $backupDbInfo.counts
            uploads_files = $backupUploadStats.Files
            uploads_bytes = $backupUploadStats.Bytes
            seconds       = $totalSeconds
        }
        Write-Log ("===== 校验通过（{0:N2}s）=====" -f $totalSeconds)
        $summary | ConvertTo-Json -Depth 6 -Compress
        exit 0
    }

    # 3) 安全检查 + 现场保护
    $TargetDir = Resolve-FullPath $TargetDir
    $liveDirs = Get-LiveDataDirs
    foreach ($live in $liveDirs) {
        if ([string]::Equals($TargetDir, $live, [System.StringComparison]::OrdinalIgnoreCase)) {
            if (-not $Force) {
                throw ("拒绝恢复到在线数据目录 {0}：请先停止服务，并加 -Force 明确确认（脚本会把现场移到 .pre-restore-*）" -f $TargetDir)
            }
            Write-Log ("警告：目标是当前在线数据目录 {0}，请确认服务已停止！" -f $TargetDir) 'WARN'
            break
        }
    }
    $targetHasData = (Test-Path (Join-Path $TargetDir $DbName)) -or (Test-Path (Join-Path $TargetDir 'uploads'))
    if ($targetHasData -and -not $Force) {
        throw ("目标目录已有数据 {0}：如确认覆盖请加 -Force（现场会先移动到 .pre-restore-*）" -f $TargetDir)
    }

    $preRestoreDir = $null
    if (Test-Path $TargetDir) {
        if ($targetHasData) {
            $preRestoreDir = Join-Path $TargetDir ('.pre-restore-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
            New-Item -ItemType Directory -Path $preRestoreDir -Force | Out-Null
            foreach ($name in @($DbName, 'uploads', 'code')) {
                $source = Join-Path $TargetDir $name
                if (Test-Path $source) {
                    Move-Item -Path $source -Destination (Join-Path $preRestoreDir $name) -Force
                }
            }
            Write-Log ("    现场已移动到 {0}" -f $preRestoreDir)
        }
    } else {
        New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
    }

    # 4) 恢复
    Write-Log '3/4 恢复数据库与 uploads...'
    Copy-Item -Path $backupDb -Destination (Join-Path $TargetDir $DbName) -Force
    foreach ($suffix in @('-wal', '-shm')) {
        $side = Join-Path $TargetDir ($DbName + $suffix)
        if (Test-Path $side) { Remove-Item -Path $side -Force }
    }
    $targetUploads = Join-Path $TargetDir 'uploads'
    New-Item -ItemType Directory -Path $targetUploads -Force | Out-Null
    if (Test-Path $backupUploads) {
        $null = robocopy $backupUploads $targetUploads /E /NFL /NDL /NJH /NJS /NP
        if ($LASTEXITCODE -ge 8) { throw ("恢复 uploads 失败（robocopy 退出码 {0}）" -f $LASTEXITCODE) }
    }

    $restoredCodeDir = $null
    if ($RestoreCode) {
        $codeZip = Join-Path $BackupPath 'code\source.zip'
        if (Test-Path $codeZip) {
            $restoredCodeDir = Join-Path $TargetDir 'code'
            New-Item -ItemType Directory -Path $restoredCodeDir -Force | Out-Null
            Expand-Archive -Path $codeZip -DestinationPath $restoredCodeDir -Force
            $baselineZip = Join-Path $BackupPath 'code\baseline-source.zip'
            if (Test-Path $baselineZip) {
                Copy-Item -Path $baselineZip -Destination (Join-Path $restoredCodeDir 'baseline-source.zip') -Force
            }
            Write-Log ("    代码快照已恢复至 {0}" -f $restoredCodeDir)
        } else {
            Write-Log '    备份中没有代码快照，跳过' 'WARN'
        }
    }

    # 5) 恢复后复核
    Write-Log '4/4 恢复后复核数据库与 uploads...'
    $postOut = Join-Path $tmpCheck 'post-out.json'
    $postErr = Join-Path $tmpCheck 'post-err.txt'
    $targetDb = Join-Path $TargetDir $DbName
    $postCode = Invoke-PythonTool -Arguments @($toolPath, 'verify', '--db', $targetDb, '--expect-json', $backupJsonPath) -OutFile $postOut -ErrFile $postErr
    $postErrText = ''
    if (Test-Path $postErr) { $postErrText = (Get-Content -Path $postErr -Raw -Encoding UTF8) }
    if ($postCode -ne 0) {
        $postInfoFail = $null
        if (Test-Path $postOut) {
            try { $postInfoFail = (Get-Content -Path $postOut -Raw -Encoding UTF8) | ConvertFrom-Json } catch { }
        }
        $detail = $postErrText
        if ($postInfoFail -and $postInfoFail.mismatches) { $detail = ($postInfoFail.mismatches -join '; ') }
        throw ("恢复后数据库校验失败: {0}" -f $detail)
    }
    $postInfo = (Get-Content -Path $postOut -Raw -Encoding UTF8) | ConvertFrom-Json
    $targetUploadStats = Get-DirectoryStats -Path $targetUploads
    if ($targetUploadStats.Files -ne $expectFiles -or $targetUploadStats.Bytes -ne $expectBytes) {
        throw ("恢复后 uploads 不一致：{0} 个/{1} B != {2} 个/{3} B" -f $targetUploadStats.Files, $targetUploadStats.Bytes, $expectFiles, $expectBytes)
    }

    $totalSeconds = [math]::Round($stopwatch.Elapsed.TotalSeconds, 2)
    $summary = [ordered]@{
        ok             = $true
        mode           = 'restore'
        backup_dir     = $BackupPath
        backup_time    = $backupMeta.created_at
        target_dir     = $TargetDir
        pre_restore    = $preRestoreDir
        db_bytes       = [long]$postInfo.bytes
        db_integrity   = [string]$postInfo.integrity
        counts         = $postInfo.counts
        uploads_files  = $targetUploadStats.Files
        uploads_bytes  = $targetUploadStats.Bytes
        code_restored  = [bool]$RestoreCode
        code_dir       = $restoredCodeDir
        seconds        = $totalSeconds
    }
    Write-Log ("===== 恢复成功：{0}（{1:N2}s）=====" -f $TargetDir, $totalSeconds)
    if (-not $script:Quiet) {
        Write-Host ''
        Write-Host '恢复完成' -ForegroundColor Green
        Write-Host ("  目标目录  : {0}" -f $TargetDir)
        Write-Host ("  数据库    : {0:N0} B, integrity={1}" -f $postInfo.bytes, $postInfo.integrity)
        Write-Host ("  行数      : {0}" -f (($postInfo.counts | ConvertTo-Json -Compress)))
        Write-Host ("  uploads   : {0} 个" -f $targetUploadStats.Files)
        if ($preRestoreDir) { Write-Host ("  原现场    : {0}" -f $preRestoreDir) }
        if ($RestoreCode) { Write-Host ("  代码      : {0}" -f $restoredCodeDir) }
        Write-Host ("  耗时      : {0:N2}s" -f $totalSeconds)
        Write-Host ''
        Write-Host '下一步：启动服务前请运行恢复冒烟校验：' -ForegroundColor Yellow
        Write-Host ("  python scripts\verify_restored.py --db `"{0}`"" -f $targetDb)
    }
    $summary | ConvertTo-Json -Depth 6 -Compress
}
catch {
    $exitCode = 1
    $message = $_.Exception.Message
    Write-Log ("恢复失败: {0}" -f $message) 'ERROR'
    if ($_.ScriptStackTrace) { Write-Log ("调用栈: {0}" -f $_.ScriptStackTrace) 'ERROR' }
    if (-not $script:Quiet) {
        Write-Host ''
        Write-Host ("恢复失败: {0}" -f $message) -ForegroundColor Red
    }
}
finally {
    if (Test-Path $tmpCheck) { Remove-Item -Path $tmpCheck -Recurse -Force -ErrorAction SilentlyContinue }
}

exit $exitCode

# -*- coding: utf-8 -*-
# Windows 本地 OCR（Windows.Media.Ocr，系统自带，无需额外安装）
# 用于识别图片型合同；zh-CN 系统优先使用简体中文语言包，失败时回退用户语言。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File ocr_win.ps1 -ImagePath "C:\path\img.png"
# 输出：识别文本（UTF-8）；失败时 stderr 给出根因并返回非 0：
#   2 = 未安装 OCR 语言包（stdout 输出 __NO_OCR_ENGINE__）
#   3 = 文件访问被拒 0x80070005（stdout 输出 __OCR_ACCESS_DENIED__）
#   1 = 其它错误
param([Parameter(Mandatory=$true)][string]$ImagePath)

$ErrorActionPreference = 'Stop'

function Get-RootCause($Exception) {
    $e = $Exception
    while ($e.InnerException) { $e = $e.InnerException }
    return $e
}

function Format-RootMessage($Exception) {
    $e = Get-RootCause $Exception
    return ("{0}: {1} (HResult=0x{2:X8})" -f $e.GetType().Name, $e.Message, $e.HResult)
}

try {
    # 统一以 UTF-8 输出，避免 Python 端按 GBK 解码失败
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
    Add-Type -AssemblyName System.Runtime.WindowsRuntime

    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                       $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

    function Await($WinRtTask, $ResultType) {
        $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($WinRtTask))
        $netTask.Wait(-1) | Out-Null
        $netTask.Result
    }

    [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null

    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

    $engine = $null
    foreach ($langName in @('zh-CN', 'zh-Hans', 'zh-Hans-CN')) {
        try {
            $lang = New-Object Windows.Globalization.Language $langName
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
            if ($engine) { break }
        } catch { }
    }
    if (-not $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
    if (-not $engine) {
        Write-Output "__NO_OCR_ENGINE__"
        [Console]::Error.WriteLine("No OCR engine / language pack installed (zh-CN not available)")
        exit 2
    }

    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @($result.Lines | ForEach-Object { $_.Text })
    $lines -join "`n"
}
catch {
    $rootMessage = Format-RootMessage $_.Exception
    [Console]::Error.WriteLine($rootMessage)
    if ($rootMessage -match '0x80070005|Access is denied|拒绝访问') {
        Write-Output "__OCR_ACCESS_DENIED__"
        exit 3
    }
    exit 1
}

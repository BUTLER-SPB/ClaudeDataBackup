<#
.SYNOPSIS
  Установка ClaudeDataBackup (форк BUTLER-SPB) на Windows: venv через uv, проверка окружения,
  опционально — папка бэкапа и ежедневная задача в Планировщике (без прав администратора).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup-win.ps1
  powershell -ExecutionPolicy Bypass -File scripts\setup-win.ps1 -BackupDir D:\Backup\Claude -ScheduleDaily -At 03:30
  powershell -ExecutionPolicy Bypass -File scripts\setup-win.ps1 -RemoveSchedule
#>
[CmdletBinding()]
param(
    [string]$BackupDir = "",
    [switch]$ScheduleDaily,
    [string]$At = "03:30",
    [switch]$RemoveSchedule,
    [string]$PythonVersion = "3.12"
)

$ErrorActionPreference = "Stop"
$TaskName = "ClaudeDataBackup Incremental"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Write-Step($msg) { Write-Host "[setup] $msg" -ForegroundColor Cyan }

if ($RemoveSchedule) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Step "Задача '$TaskName' удалена"
    } else {
        Write-Step "Задача '$TaskName' не найдена"
    }
    exit 0
}

# 1. uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Step "uv не найден — устанавливаю (astral.sh)"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv не установился. Поставь вручную: winget install astral-sh.uv"
    }
}

# 2. venv + зависимости
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Step "Создаю .venv на Python $PythonVersion"
    uv venv --python $PythonVersion .venv
    if ($LASTEXITCODE -ne 0) { throw "uv venv завершился с кодом $LASTEXITCODE" }
}
Write-Step "Устанавливаю пакет и зависимости"
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "uv pip install завершился с кодом $LASTEXITCODE" }

$Cli = Join-Path $Root ".venv\Scripts\claude-data-backup.exe"
$env:PYTHONUTF8 = "1"

# 3. Тесты
Write-Step "Тесты"
& .venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Тесты упали" }

# 4. Папка бэкапа
if ($BackupDir) {
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    & $Cli --set-backup-dir $BackupDir
}

# 5. Диагностика (без сети)
Write-Step "Диагностика источников"
& $Cli list
& $Cli list --json | Out-File -Encoding utf8 (Join-Path $Root "diag-list.json")
Write-Step "JSON-диагностика: $Root\diag-list.json"

# 6. Планировщик: ежедневный инкрементальный бэкап под текущим пользователем
if ($ScheduleDaily) {
    $LogDir = Join-Path $env:USERPROFILE ".claude-data-backup\logs"
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $Cmd = "`$env:PYTHONUTF8='1'; & '$Cli' --incremental --json *> '$LogDir\scheduled-last.json'"
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$Cmd`"" `
        -WorkingDirectory $Root
    $Trigger = New-ScheduledTaskTrigger -Daily -At $At
    $Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -RunOnlyIfNetworkAvailable
    $Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
        -Settings $Settings -Principal $Principal `
        -Description "ClaudeDataBackup: ежедневный инкрементальный бэкап (Mode A+B+C)" -Force | Out-Null
    Write-Step "Задача '$TaskName' создана: ежедневно в $At (пропущенный запуск выполнится при входе)"
    Write-Step "Ручной запуск: Start-ScheduledTask -TaskName '$TaskName'"
}

Write-Step "Готово. Первый бэкап: & '$Cli' --incremental"

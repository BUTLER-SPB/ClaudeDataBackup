<#
  ClaudeDataBackup (BUTLER-SPB fork) - one-line installer for Windows, no admin rights.

  Install / update (run in PowerShell):
    irm https://raw.githubusercontent.com/BUTLER-SPB/ClaudeDataBackup/main/scripts/install.ps1 | iex

  What it does:
    1. installs uv (Python package manager) for the current user if missing;
    2. downloads the app from GitHub into %LOCALAPPDATA%\ClaudeDataBackup;
    3. creates a private Python 3.12 environment and installs the app;
    4. creates "Claude Backup" shortcuts (Start menu + Desktop) and starts the app.
  Re-running the same line updates the app. Backups and settings are kept.

  Options (when running the file directly):
    -Ref <branch>   install another branch (default: main)
    -NoLaunch       do not start the app after install
    -Uninstall      remove app, shortcuts and the daily task (backups are kept)

  This file is intentionally ASCII-only: it must work both via "irm | iex"
  and via "powershell -File" on Windows PowerShell 5.1.
#>
param(
    [string]$Ref = $(if ($env:CDB_REF) { $env:CDB_REF } else { "main" }),
    [string]$Repo = "BUTLER-SPB/ClaudeDataBackup",
    [string]$SourceDir = "",
    [switch]$NoLaunch,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Base     = Join-Path $env:LOCALAPPDATA "ClaudeDataBackup"
$AppDir   = Join-Path $Base "app"
$VenvDir  = Join-Path $Base "venv"
$Py       = Join-Path $VenvDir "Scripts\python.exe"
# GUI-subsystem launchers (no console window). Note: venv\Scripts\pythonw.exe
# created by uv is a console launcher, so it is not used here.
$AppExe   = Join-Path $VenvDir "Scripts\claude-backup.exe"
$RunExe   = Join-Path $VenvDir "Scripts\claude-backup-run.exe"
$TaskName = "ClaudeDataBackup Incremental"
$LinkName = "Claude Backup.lnk"
$StartLnk = Join-Path ([Environment]::GetFolderPath("Programs")) $LinkName
$DeskLnk  = Join-Path ([Environment]::GetFolderPath("Desktop")) $LinkName

function Step($m) { Write-Host "[ClaudeBackup] $m" -ForegroundColor Cyan }

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item $StartLnk, $DeskLnk -Force -ErrorAction SilentlyContinue
    Remove-Item $Base -Recurse -Force -ErrorAction SilentlyContinue
    Step "Removed. Backups and %USERPROFILE%\.claude-data-backup settings were kept."
    return
}

# 1. uv
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    $local = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $local) { $uv = Get-Item $local }
}
if (-not $uv) {
    Step "Installing uv (per-user, from astral.sh)..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $local = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (-not (Test-Path $local)) { throw "uv was not installed. Try: winget install astral-sh.uv" }
    $uv = Get-Item $local
}
$UvExe = if ($uv.Source) { $uv.Source } else { $uv.FullName }

# 2. Source: GitHub archive (default) or a local checkout (-SourceDir, for testing)
New-Item -ItemType Directory -Force -Path $Base | Out-Null
if ($SourceDir) {
    Step "Copying source from $SourceDir..."
    if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force }
    New-Item -ItemType Directory -Path $AppDir | Out-Null
    foreach ($item in @("pyproject.toml", "README.md", "src")) {
        Copy-Item (Join-Path $SourceDir $item) $AppDir -Recurse
    }
} else {
    Step "Downloading $Repo ($Ref)..."
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("cdb-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp "src.zip"
        Invoke-WebRequest -UseBasicParsing -Uri "https://codeload.github.com/$Repo/zip/refs/heads/$Ref" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
        if (-not $src) { throw "Archive is empty" }
        if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force }
        Move-Item $src.FullName $AppDir
    } finally {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# 3. Python environment.
# Created with the standard "python -m venv": its Scripts\pythonw.exe is a GUI
# launcher. A venv made by "uv venv" has a console pythonw.exe, which opens a
# black console window behind the app. Older installs (no marker) are rebuilt.
$Marker = Join-Path $VenvDir ".stdlib-venv"
if ((Test-Path $VenvDir) -and -not (Test-Path $Marker)) {
    Step "Rebuilding the Python environment (old format)..."
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($VenvDir, [StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force -ErrorAction SilentlyContinue
    Remove-Item $VenvDir -Recurse -Force
}
if (-not (Test-Path $Py)) {
    Step "Creating Python 3.12 environment..."
    & $UvExe python install 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv python install failed ($LASTEXITCODE)" }
    $BasePy = (& $UvExe python find 3.12 | Select-Object -First 1)
    if (-not $BasePy -or -not (Test-Path $BasePy)) { throw "Python 3.12 not found after install" }
    & $BasePy -m venv --without-pip $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed ($LASTEXITCODE)" }
    Set-Content -Path $Marker -Value "created by install.ps1" -Encoding ascii
}
Step "Installing the app..."
& $UvExe pip install --python $Py --reinstall-package claude-data-backup $AppDir
if ($LASTEXITCODE -ne 0) { throw "uv pip install failed ($LASTEXITCODE)" }

# 4. Shortcuts
$icon = & $Py -c "import claude_data_backup, pathlib; print(pathlib.Path(claude_data_backup.__file__).with_name('app-icon.ico'))"
$shell = New-Object -ComObject WScript.Shell
foreach ($lnk in @($StartLnk, $DeskLnk)) {
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $AppExe
    $s.Arguments = ""
    $s.WorkingDirectory = $env:USERPROFILE
    if ($icon -and (Test-Path $icon)) { $s.IconLocation = $icon }
    $s.Description = "Claude Backup"
    $s.Save()
}

# If a daily task exists (older install), point it at this install.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    $a = New-ScheduledTaskAction -Execute $RunExe -WorkingDirectory $env:USERPROFILE
    Set-ScheduledTask -TaskName $TaskName -Action $a | Out-Null
    Step "Daily task updated to use this install."
}

Step "Done. Shortcut 'Claude Backup' is in the Start menu and on the Desktop."
if (-not $NoLaunch) { Start-Process -FilePath $AppExe -WorkingDirectory $env:USERPROFILE }

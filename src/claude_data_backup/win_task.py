"""Ежедневный бэкап через Планировщик заданий Windows (fork BUTLER-SPB).

Задача создаётся под текущим пользователем (LogonType Interactive, RunLevel
Limited) — права администратора не нужны. Пропущенный запуск (ПК был выключен)
выполняется при следующей возможности (StartWhenAvailable).
Запускается pythonw.exe -m claude_data_backup.scheduled_run — без окна консоли.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

TASK_NAME = "ClaudeDataBackup Incremental"
DEFAULT_TIME = "03:30"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _ps(script: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Выполнить PowerShell-скрипт через -EncodedCommand (без проблем с кавычками).
    Вывод принудительно в UTF-8: иначе кириллица в путях (C:\\Users\\Иван) ломается."""
    script = "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n" + script
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", enc],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=_NO_WINDOW,
    )


def _q(s: str) -> str:
    """Строка в одинарных кавычках PowerShell. PowerShell считает кавычками и
    типографские ‘ ’ ‚ ‛ — их тоже удваиваем."""
    return "'" + re.sub("['‘’‚‛]", lambda m: m.group() * 2, str(s)) + "'"


def pythonw_path() -> Path:
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return cand if cand.is_file() else exe


def task_command() -> tuple[Path, str]:
    """Что запускает задача: оконный лаунчер claude-backup-run.exe (без консоли).
    pythonw.exe из venv, созданного uv, — консольный лаунчер (мигает чёрное окно),
    поэтому он только запасной вариант."""
    run_exe = Path(sys.prefix) / "Scripts" / "claude-backup-run.exe"
    if run_exe.is_file():
        return run_exe, ""
    return pythonw_path(), "-m claude_data_backup.scheduled_run"


def valid_time(hhmm: str) -> bool:
    try:
        h, m = hhmm.strip().split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59 and len(m) == 2
    except (ValueError, AttributeError):
        return False


def build_install_script(at: str, exe: Path, args: str, workdir: Path) -> str:
    arg = f" -Argument {_q(args)}" if args else ""
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$a = New-ScheduledTaskAction -Execute {_q(exe)}{arg} -WorkingDirectory {_q(workdir)}",
        f"$t = New-ScheduledTaskTrigger -Daily -At {_q(at)}",
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)",
        "$p = New-ScheduledTaskPrincipal -UserId \"$env:USERDOMAIN\\$env:USERNAME\" "
        "-LogonType Interactive -RunLevel Limited",
        f"Register-ScheduledTask -TaskName {_q(TASK_NAME)} -Action $a -Trigger $t "
        "-Settings $s -Principal $p "
        "-Description 'ClaudeDataBackup: ежедневный инкрементальный бэкап' -Force | Out-Null",
    ])


def install(at: str = DEFAULT_TIME) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Только Windows"
    if not valid_time(at):
        return False, f"Неверное время: {at!r} (нужно ЧЧ:ММ)"
    exe, args = task_command()
    r = _ps(build_install_script(at, exe, args, Path.home()))
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[-500:]
    return True, ""


def remove() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Только Windows"
    r = _ps(f"Unregister-ScheduledTask -TaskName {_q(TASK_NAME)} -Confirm:$false "
            "-ErrorAction SilentlyContinue")
    return r.returncode == 0, (r.stderr or "").strip()[-500:]


def run_now() -> tuple[bool, str]:
    r = _ps(f"Start-ScheduledTask -TaskName {_q(TASK_NAME)}")
    return r.returncode == 0, (r.stderr or "").strip()[-500:]


_QUERY = """
$t = Get-ScheduledTask -TaskName %s -ErrorAction SilentlyContinue
if (-not $t) { '{"exists": false}'; exit 0 }
$i = $t | Get-ScheduledTaskInfo
$fmt = 'yyyy-MM-dd HH:mm'
$at = ''
if ($t.Triggers.Count -gt 0 -and $t.Triggers[0].StartBoundary) {
  $at = ([datetime]$t.Triggers[0].StartBoundary).ToString('HH:mm') }
$last = ''
if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $last = $i.LastRunTime.ToString($fmt) }
$next = ''
if ($i.NextRunTime) { $next = $i.NextRunTime.ToString($fmt) }
[pscustomobject]@{ exists = $true; state = [string]$t.State; at = $at;
  next_run = $next; last_run = $last; last_result = [int64]$i.LastTaskResult;
  execute = [string]$t.Actions[0].Execute; arguments = [string]$t.Actions[0].Arguments
} | ConvertTo-Json -Compress
"""


def query() -> dict:
    if sys.platform != "win32":
        return {"exists": False, "error": "Только Windows"}
    try:
        r = _ps(_QUERY % _q(TASK_NAME), timeout=30)
        return json.loads(r.stdout.strip() or '{"exists": false}')
    except Exception as e:
        return {"exists": False, "error": str(e)}


def is_ours(info: dict) -> bool:
    """Задача запускает текущую установку (а не старый setup-win.ps1 / другую копию)."""
    exe, args = task_command()
    return bool(info.get("exists")) and \
        str(exe).lower() == str(info.get("execute", "")).lower() and \
        str(args) == str(info.get("arguments", "") or "")

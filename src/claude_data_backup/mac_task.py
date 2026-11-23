"""Ежедневный бэкап на macOS через launchd (LaunchAgent пользователя, без sudo).

Тот же интерфейс, что у win_task: install / remove / run_now / query / is_ours.
Если Mac спал во время запуска, launchd выполнит задачу после пробуждения;
если был выключен — пропущенный запуск не выполняется (следующий — по расписанию).
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .win_task import DEFAULT_TIME, valid_time  # noqa: F401  (общие для платформ)

LABEL = "com.butler.claudebackup"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def program_args() -> list[str]:
    """Интерпретатор venv (не resolve(): симлинк venv/bin/python3 указывает на venv)."""
    py = Path(sys.prefix) / "bin" / "python3"
    if not py.exists():
        py = Path(sys.executable)
    return [str(py), "-m", "claude_data_backup.scheduled_run"]


def build_plist(at: str, args: list[str], home: Path, log_dir: Path) -> bytes:
    h, m = (int(x) for x in at.split(":"))
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": args,
        "StartCalendarInterval": {"Hour": h, "Minute": m},
        "WorkingDirectory": str(home),
        "EnvironmentVariables": {"PYTHONUTF8": "1", "LANG": "ru_RU.UTF-8"},
        "StandardOutPath": str(log_dir / "launchd.log"),
        "StandardErrorPath": str(log_dir / "launchd.log"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
    })


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(at: str = DEFAULT_TIME) -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "Только macOS"
    if not valid_time(at):
        return False, f"Неверное время: {at!r} (нужно ЧЧ:ММ)"
    from .config import CONFIG_DIR
    log_dir = CONFIG_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", _domain(), str(p))          # старая версия, если была
    p.write_bytes(build_plist(at, program_args(), Path.home(), log_dir))
    r = _launchctl("bootstrap", _domain(), str(p))
    if r.returncode != 0:
        # старые macOS: load -w
        r2 = _launchctl("load", "-w", str(p))
        if r2.returncode != 0:
            return False, (r.stderr or r2.stderr or "launchctl error").strip()[-500:]
    return True, ""


def remove() -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "Только macOS"
    p = plist_path()
    _launchctl("bootout", _domain(), str(p))
    p.unlink(missing_ok=True)
    return True, ""


def run_now() -> tuple[bool, str]:
    r = _launchctl("kickstart", f"{_domain()}/{LABEL}")
    return r.returncode == 0, (r.stderr or "").strip()


def next_run(at: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    h, m = (int(x) for x in at.split(":"))
    cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return cand if cand > now else cand + timedelta(days=1)


def query() -> dict:
    p = plist_path()
    if not p.is_file():
        return {"exists": False}
    try:
        d = plistlib.loads(p.read_bytes())
    except Exception as e:
        return {"exists": True, "error": str(e), "execute": "", "arguments": ""}
    sci = d.get("StartCalendarInterval") or {}
    at = f"{int(sci.get('Hour', 0)):02d}:{int(sci.get('Minute', 0)):02d}"
    args = list(d.get("ProgramArguments") or [])
    info = {
        "exists": True,
        "state": "Ready",
        "at": at,
        "next_run": next_run(at).strftime("%Y-%m-%d %H:%M"),
        "execute": args[0] if args else "",
        "arguments": " ".join(args[1:]),
    }
    # Последний ночной запуск — из истории бэкапов
    try:
        from . import config as cfg, history
        for rec in history.read(cfg.get_backup_dir(), 50):
            if rec.get("trigger") == "schedule":
                info["last_run"] = rec.get("time", "")[:16]
                info["last_result"] = rec.get("exit_code", 0)
                break
    except Exception:
        pass
    return info


def is_ours(info: dict) -> bool:
    args = program_args()
    return bool(info.get("exists")) and info.get("execute") == args[0] \
        and info.get("arguments") == " ".join(args[1:])

"""Закрыть Claude Desktop на несколько секунд, прочитать sessionKey, запустить снова.

Нужно потому, что запущенный Claude Desktop держит файл Cookies с монопольной
блокировкой (WinError 32) — прочитать ключ можно только пока он закрыт.
Вызывается из окна приложения только после явного подтверждения пользователя.
Завершаются только процессы Claude Desktop (Electron: рядом с exe есть resources\\),
без /T — дерево процессов не убивается, отдельный claude.exe от Claude Code CLI
не трогается. Сеансы вкладки Code внутри Desktop при закрытии Desktop прерываются
(об этом предупреждает диалог).
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import cookies, paths

_NO_WINDOW = 0x08000000


def relaunch_command(exe: str, cmdline: str = "") -> list[str] | str | None:
    """Команда повторного запуска.

    MSIX — через AppsFolder (иначе сборка стартует без package identity и с другим
    профилем; --user-data-dir пакет задаёт сам). Обычная установка — исходной
    командной строкой главного процесса (сохраняет --user-data-dir и прочие ключи).
    """
    if not exe:
        return None
    p = Path(exe)
    if "\\windowsapps\\" in str(p).lower():
        pkg_dir = next((d for d in p.parents if d.parent.name.lower() == "windowsapps"), None)
        if pkg_dir is None:
            return None
        m = re.match(r"^([^_]+)_.*__([a-z0-9]+)$", pkg_dir.name, re.IGNORECASE)
        if not m:
            return None
        family = f"{m.group(1)}_{m.group(2)}"
        app_id = "App"
        manifest = pkg_dir / "AppxManifest.xml"
        try:
            mm = re.search(r'<Application\s[^>]*Id="([^"]+)"',
                           manifest.read_text(encoding="utf-8", errors="replace"))
            if mm:
                app_id = mm.group(1)
        except OSError:
            pass
        return ["explorer.exe", f"shell:AppsFolder\\{family}!{app_id}"]
    if cmdline.strip():
        return cmdline.strip()
    return [str(p)]


def _kill(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, creationflags=_NO_WINDOW, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        pass


def close_read_relaunch(log: Callable[[str], None] = print,
                        wait_s: float = 20.0) -> dict:
    """Возвращает {"ok", "error", "was_running", "relaunched"}. Ключ не возвращается."""
    procs = paths.list_desktop_processes()
    main = [p for p in procs if p.is_main] or procs
    cmd = relaunch_command(main[0].exe, main[0].cmdline) if main else None
    res = {"ok": False, "error": None, "was_running": bool(procs), "relaunched": False}

    try:
        if procs:
            log("Закрываю Claude Desktop…")
            for p in main + [p for p in procs if p not in main]:
                _kill(p.pid)

        deadline = time.monotonic() + wait_s
        sk, err = None, None
        while True:
            sk, err = cookies.read_session_key_from_desktop()
            if sk or err != "locked" or time.monotonic() > deadline:
                break
            time.sleep(1.0)

        if sk:
            res["ok"] = cookies.store_session_key(sk)
            if res["ok"]:
                log("Ключ прочитан и сохранён (зашифрован под вашей учётной записью Windows).")
            else:
                res["error"] = "Не удалось сохранить ключ"
        else:
            res["error"] = {"locked": "Файл Cookies всё ещё занят",
                            "not_found": "В Claude Desktop нет входа в аккаунт claude.ai"
                            }.get(err or "", err or "Ключ не найден")
            log(f"Ключ не прочитан: {res['error']}")
    except Exception as e:  # Claude всё равно запускаем снова
        res["error"] = f"Ошибка: {e}"
    finally:
        if procs and cmd:
            try:
                subprocess.Popen(cmd, creationflags=_NO_WINDOW, close_fds=True)
                res["relaunched"] = True
                log("Claude Desktop запущен снова.")
            except OSError as e:
                log(f"Не удалось запустить Claude Desktop: {e}. Откройте его вручную.")
        paths.reset_desktop_root_cache()
    return res

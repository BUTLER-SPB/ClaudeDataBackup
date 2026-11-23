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
from .log import get_logger

_log = get_logger(__name__)
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


def _kill(pid: int, wait_ms: int = 5000) -> None:
    """TerminateProcess + ожидание выхода (WinAPI, без внешнего taskkill)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    PROCESS_TERMINATE, SYNCHRONIZE = 0x0001, 0x00100000
    h = k32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if not h:
        return  # уже завершился или нет доступа
    try:
        k32.TerminateProcess(h, 1)
        k32.WaitForSingleObject(h, wait_ms)
    finally:
        k32.CloseHandle(h)


def current_relaunch_command() -> list[str] | str | None:
    procs = paths.list_desktop_processes()
    main = [p for p in procs if p.is_main] or procs
    return relaunch_command(main[0].exe, main[0].cmdline) if main else None


def launch(cmd: list[str] | str | None) -> bool:
    if not cmd:
        return False
    try:
        subprocess.Popen(cmd, creationflags=_NO_WINDOW, close_fds=True)
        return True
    except OSError as e:
        _log.warning("Не удалось запустить Claude Desktop: %s", e)
        return False


def close_read_relaunch(log: Callable[[str], None] = print,
                        wait_s: float = 20.0) -> dict:
    """Возвращает {"ok", "error", "was_running", "relaunched"}. Ключ не возвращается.
    Каждый шаг пишется в app.log — чтобы было видно, где остановилось."""
    def step(msg: str) -> None:
        _log.info("[read-key] %s", msg)
        log(msg)

    procs = paths.list_desktop_processes()
    main = [p for p in procs if p.is_main] or procs
    cmd = relaunch_command(main[0].exe, main[0].cmdline) if main else None
    res = {"ok": False, "error": None, "was_running": bool(procs), "relaunched": False}
    _log.info("[read-key] процессов Desktop: %d, перезапуск: %s", len(procs), cmd)

    try:
        if procs:
            step("Закрываю Claude Desktop…")
            for p in main + [p for p in procs if p not in main]:
                _kill(p.pid)
            _log.info("[read-key] taskkill выполнен")

        deadline = time.monotonic() + wait_s
        sk, err = None, None
        while True:
            sk, err = cookies.read_session_key_from_desktop()
            _log.info("[read-key] чтение Cookies: %s", "ok" if sk else err)
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
        _log.error("[read-key] ошибка", exc_info=True)
        res["error"] = f"Ошибка: {e}"
    finally:
        if procs and cmd:
            res["relaunched"] = launch(cmd)
            step("Claude Desktop запущен снова." if res["relaunched"]
                 else "Не удалось запустить Claude Desktop — откройте его вручную.")
        paths.reset_desktop_root_cache()
    return res


def main() -> int:
    """Отдельный процесс для окна приложения: результат — одна строка JSON в stdout.
    Окно ждёт его с таймаутом и при зависании завершает процесс и запускает Claude."""
    import json
    import os
    import sys
    import threading
    from .log import setup_logging
    setup_logging()
    # Сторожевой таймер: процесс не может висеть дольше 75 с (окно ждёт 90 с
    # и само запустит Claude, если его нет)
    wd = threading.Timer(75, lambda: (_log.error("[read-key] watchdog: выход"), os._exit(3)))
    wd.daemon = True
    wd.start()
    res = close_read_relaunch(log=lambda m: None)
    sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

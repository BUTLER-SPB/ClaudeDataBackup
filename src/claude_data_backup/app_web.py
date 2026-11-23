"""Окно «Бэкап Claude» на HTML (pywebview): Edge WebView2 на Windows, WebKit на macOS.

Интерфейс — src/claude_data_backup/web/index.html, логика — класс Api ниже
(методы вызываются из JS как window.pywebview.api.<метод>(), события из Python
приходят в window.onBackend). Бэкап и чтение ключа выполняются отдельными
процессами — окно не может из-за них зависнуть.
Если WebView недоступен, запускается запасное окно app_ru (customtkinter).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import __version__
from . import config as cfg
from . import history
from . import manifest as mf
from . import win_task
from .log import get_logger, log_path, setup_logging

log = get_logger(__name__)

WEB_DIR = Path(__file__).with_name("web")
IS_WIN = sys.platform == "win32"
_NO_WINDOW = 0x08000000 if IS_WIN else 0
READ_KEY_TIMEOUT_S = 90


def _python_console() -> str:
    """Интерпретатор для дочерних процессов (без окна консоли: CREATE_NO_WINDOW)."""
    scripts = Path(sys.prefix) / ("Scripts" if IS_WIN else "bin")
    for name in (("python.exe",) if IS_WIN else ("python3", "python")):
        if (scripts / name).is_file():
            return str(scripts / name)
    return sys.executable


def _child_env(**extra: str) -> dict:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    env.update(extra)
    return env


def _open(path: Path) -> None:
    if IS_WIN:
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _compact_prep(r: dict) -> dict:
    """То, что окно показывает сразу при открытии (без повторной проверки)."""
    d, k, c = r.get("desktop", {}), r.get("key", {}), r.get("claude_code", {})
    on = k.get("online", {})
    return {
        "time": r.get("time"),
        "desktop": {"found": d.get("found"), "profile": d.get("profile"),
                    "running": d.get("running")},
        "key": {"found": k.get("found"), "source": k.get("source"),
                "locked": k.get("locked"), "error": k.get("error"),
                "saved_at": (k.get("stored") or {}).get("saved_at")},
        "online": {"checked": on.get("checked"), "ok": on.get("ok"),
                   "orgs": on.get("orgs"), "org_names": on.get("org_names", []),
                   "error": on.get("error")},
        "code": {"found": c.get("found"), "dir": c.get("projects_dir"),
                 "sessions": (c.get("sessions") or {}).get("real", 0)},
    }


class Api:
    def __init__(self) -> None:
        self._window = None
        self._busy = threading.Lock()

    # ── события в JS ────────────────────────────────────────
    def _emit(self, kind: str, **data) -> None:
        if not self._window:
            return
        payload = json.dumps({"type": kind, **data}, ensure_ascii=False)
        try:
            self._window.evaluate_js(f"window.onBackend && window.onBackend({payload})")
        except Exception:
            log.debug("evaluate_js failed", exc_info=True)

    # ── состояние ───────────────────────────────────────────
    def get_state(self) -> dict:
        bdir = cfg.get_backup_dir()
        conf = cfg.load_config()
        summary = {"conversations": 0, "sessions": 0, "last_backup": None}
        if (bdir / "manifest.json").is_file():
            man = mf.load_manifest(bdir)
            s = mf.summary(man)
            summary = {"conversations": s.get("conversation_count", 0),
                       "sessions": s.get("session_count", 0),
                       "last_backup": man.get("last_backup_time") or None}
        return {
            "version": __version__,
            "platform": sys.platform,
            "backup_dir": str(bdir),
            "has_backup": (bdir / "manifest.json").is_file(),
            "has_viewer": (bdir / "index.html").is_file(),
            "first_run": not conf.get("prepared_at"),
            "schedule_auto_done": bool(conf.get("schedule_auto_done")),
            "prep": conf.get("last_prep"),
            "summary": summary,
            "history": history.read(bdir, 60),
            "busy": self._busy.locked(),
            "schedule_supported": IS_WIN,
        }

    def get_task(self) -> dict:
        if not IS_WIN:
            return {"exists": False, "supported": False}
        info = win_task.query()
        info["ours"] = win_task.is_ours(info)
        info["supported"] = True
        return info

    # ── подготовка и ключ ───────────────────────────────────
    def prepare(self) -> dict:
        from .prepare import run_prepare
        try:
            r = _compact_prep(run_prepare())
        except Exception as e:
            log.error("prepare failed", exc_info=True)
            return {"error": str(e)}
        conf = cfg.load_config()
        conf["last_prep"] = r
        cfg.save_config(conf)
        return r

    def read_key(self) -> dict:
        """Закрыть Claude Desktop, прочитать ключ, запустить Claude снова.
        Выполняется отдельным процессом с таймаутом; если он не уложился —
        процесс останавливается, а Claude запускается отсюда."""
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "Подождите, идёт другая операция"}
        from . import paths
        from .desktop_control import current_relaunch_command, launch
        res: dict = {"ok": False}
        try:
            cmd = current_relaunch_command()
            log.info("read_key: helper start, relaunch=%s", cmd)
            try:
                p = subprocess.run(
                    [_python_console(), "-m", "claude_data_backup.desktop_control"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=READ_KEY_TIMEOUT_S, env=_child_env(), creationflags=_NO_WINDOW)
                lines = [ln for ln in p.stdout.splitlines() if ln.strip().startswith("{")]
                res = json.loads(lines[-1]) if lines else {
                    "ok": False, "error": (p.stderr or "нет ответа").strip()[-300:]}
            except subprocess.TimeoutExpired:
                log.error("read_key: helper timeout")
                res = {"ok": False, "error": "Чтение ключа не уложилось в "
                       f"{READ_KEY_TIMEOUT_S} с и было остановлено. Подробности — в журнале."}
            except Exception as e:
                log.error("read_key failed", exc_info=True)
                res = {"ok": False, "error": str(e)}
            if cmd and not paths.list_desktop_processes():
                res["relaunched"] = launch(cmd)
            log.info("read_key: result ok=%s error=%s", res.get("ok"), res.get("error"))
        finally:
            self._busy.release()
        return res

    # ── бэкап ───────────────────────────────────────────────
    def backup(self) -> dict:
        if not self._busy.acquire(blocking=False):
            return {"started": False, "error": "Подождите, идёт другая операция"}
        threading.Thread(target=self._run_backup, daemon=True).start()
        return {"started": True}

    def _run_backup(self) -> None:
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        code = 1
        try:
            p = subprocess.Popen(
                [_python_console(), "-m", "claude_data_backup.main", "--incremental"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace",
                env=_child_env(CDB_TRIGGER="manual"), creationflags=_NO_WINDOW)
            assert p.stdout is not None
            for line in p.stdout:
                if line.strip():
                    self._emit("log", line=line.rstrip())
            code = p.wait()
        except Exception as e:
            log.error("backup failed", exc_info=True)
            self._emit("log", line=f"Не удалось запустить бэкап: {e}")
        finally:
            self._busy.release()
        last = history.read(cfg.get_backup_dir(), 1)
        rec = last[0] if last and last[0].get("time", "") >= started else None
        self._emit("done", exit_code=(rec or {}).get("exit_code", code), record=rec)

    # ── папка ───────────────────────────────────────────────
    def choose_dir(self) -> dict:
        import webview
        cur = cfg.get_backup_dir()
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) \
            or getattr(webview, "FOLDER_DIALOG", 20)
        picked = self._window.create_file_dialog(
            kind, directory=str(cur if cur.exists() else Path.home()))
        if not picked:
            return {"changed": False}
        p = Path(picked[0] if isinstance(picked, (list, tuple)) else picked)
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            return {"changed": False, "error": f"Нет записи в {p}: {e}"}
        cfg.set_backup_dir(p)
        return {"changed": True, "path": str(p), "old": str(cur)}

    def open_dir(self) -> None:
        d = cfg.get_backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        _open(d)

    def open_viewer(self) -> dict:
        idx = cfg.get_backup_dir() / "index.html"
        if not idx.is_file():
            return {"ok": False}
        _open(idx)
        return {"ok": True}

    def open_log(self) -> None:
        _open(log_path())

    # ── расписание ──────────────────────────────────────────
    def set_schedule(self, enabled: bool, at: str = win_task.DEFAULT_TIME) -> dict:
        if not IS_WIN:
            return {"ok": False, "error": "Расписание пока только для Windows"}
        if enabled and not win_task.valid_time(at):
            return {"ok": False, "error": "Время в формате ЧЧ:ММ, например 03:30"}
        ok, err = win_task.install(at) if enabled else win_task.remove()
        conf = cfg.load_config()
        conf["schedule_auto_done"] = True
        cfg.save_config(conf)
        return {"ok": ok, "error": err, "task": self.get_task()}

    def ensure_default_schedule(self) -> dict:
        """Первый запуск: создать ежедневную задачу, если её ещё нет."""
        conf = cfg.load_config()
        if conf.get("schedule_auto_done") or not IS_WIN:
            return {"created": False, "task": self.get_task()}
        info = self.get_task()
        created = False
        if not info.get("ours"):
            created, _err = win_task.install(win_task.DEFAULT_TIME)
        conf["schedule_auto_done"] = True
        cfg.save_config(conf)
        return {"created": created, "task": self.get_task()}


APP_ID = "BUTLER.ClaudeBackup"


def _win_set_app_id() -> None:
    """Своя группа на панели задач (иначе окно считается «Python»)."""
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass


def _win_set_icon(title: str) -> None:
    """Иконка окна и панели задач (WinForms-окно pywebview берёт иконку python)."""
    if not IS_WIN:
        return
    ico = Path(__file__).with_name("app-icon.ico")
    if not ico.is_file():
        return
    try:
        import ctypes
        u32 = ctypes.windll.user32
        u32.FindWindowW.restype = ctypes.c_void_p
        u32.LoadImageW.restype = ctypes.c_void_p
        u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        hwnd = u32.FindWindowW(None, title)
        if not hwnd:
            return
        IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
        for kind, size in ((1, 256), (0, 32)):  # ICON_BIG, ICON_SMALL
            h = u32.LoadImageW(None, str(ico), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if h:
                u32.SendMessageW(hwnd, WM_SETICON, kind, h)
    except Exception:
        log.debug("set icon failed", exc_info=True)


def _background() -> str:
    try:
        import darkdetect
        return "#111214" if darkdetect.isDark() else "#F4F5F7"
    except Exception:
        return "#F4F5F7"


def main() -> None:
    setup_logging()
    log.info("=== ClaudeDataBackup app_web v%s ===", __version__)
    try:
        import webview
    except Exception:
        log.warning("pywebview недоступен — запасное окно", exc_info=True)
        from .app_ru import main as fallback
        fallback()
        return
    api = Api()
    _win_set_app_id()
    title = "Бэкап Claude"
    try:
        win = webview.create_window(
            title, url=(WEB_DIR / "index.html").as_uri(), js_api=api,
            width=1080, height=800, min_size=(760, 620),
            background_color=_background(), text_select=False)
        api._window = win
        win.events.shown += lambda: _win_set_icon(title)
        webview.start(debug=bool(os.environ.get("CDB_WEB_DEBUG")),
                      storage_path=str(cfg.CONFIG_DIR / "webview"))
    except Exception:
        log.error("WebView не запустился — запасное окно", exc_info=True)
        from .app_ru import main as fallback
        fallback()


if __name__ == "__main__":
    main()

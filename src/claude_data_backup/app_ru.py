"""Простое окно «Бэкап Claude» для сотрудников (fork BUTLER-SPB).

Четыре блока: Подготовка → Папка → Расписание → Бэкапы.
При первом запуске всё делается само: подготовка, ежедневное расписание,
первый бэкап. Бэкап выполняется отдельным процессом (тот же CLI, что и ночной
запуск), окно только показывает ход и историю.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from . import __version__
from . import config as cfg
from . import history
from . import manifest as mf
from . import win_task
from .log import get_logger, log_path, setup_logging

log = get_logger(__name__)

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

FONT = "Segoe UI" if sys.platform == "win32" else "sans-serif"
OK, WARN, BAD, IDLE = "#1a7f37", "#b35900", "#c62828", "#6b6b6b"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

TRIGGER_RU = {"manual": "вручную", "schedule": "по расписанию", "cli": "из консоли"}
EXIT_RU = {0: "Успешно", 4: "Частично", 3: "Нет ключа claude.ai", 1: "Ошибка", 2: "Ошибка"}
RESTART_WARNING = ("Несохранённый текст в окне Claude может пропасть, а сеансы во вкладке "
                   "Code приложения Claude будут прерваны. Отдельно запущенный Claude Code "
                   "(в терминале) не затрагивается.")


def _open(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _python_console() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("python.exe") if sys.platform == "win32" else exe
    return str(cand if cand.is_file() else exe)


class SimpleApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.ui: queue.Queue = queue.Queue()
        self.busy = False
        self.prep: dict | None = None
        root.title(f"Бэкап Claude — v{__version__}")
        root.geometry("860x820")
        root.minsize(760, 680)
        self._set_icon()
        self._build()
        self.root.after(100, self._pump)
        self.refresh_folder()
        self.refresh_history()
        self.refresh_task()
        if not cfg.load_config().get("prepared_at"):
            self.root.after(600, lambda: self.on_prepare(first_run=True))

    # ── служебное ───────────────────────────────────────────
    def _set_icon(self) -> None:
        ico = Path(__file__).with_name("app-icon.ico")
        try:
            if ico.is_file() and sys.platform == "win32":
                self.root.iconbitmap(default=str(ico))
        except Exception:
            pass

    def _pump(self) -> None:
        try:
            while True:
                try:
                    fn = self.ui.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn()
                except Exception as e:  # одна ошибка не должна заморозить окно
                    log.error("UI callback failed", exc_info=True)
                    self.busy = False
                    try:
                        self._set_busy(False)
                        self.logbox.configure(state="normal")
                        self.logbox.insert("end", f"Ошибка: {e}\n")
                        self.logbox.configure(state="disabled")
                    except Exception:
                        pass
        finally:
            self.root.after(100, self._pump)

    def later(self, fn) -> None:
        """Выполнить fn в UI-потоке."""
        self.ui.put(fn)

    def bg(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def log(self, msg: str) -> None:
        def _w():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", msg.rstrip() + "\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.later(_w)

    def _section(self, parent, title: str) -> ctk.CTkFrame:
        box = ctk.CTkFrame(parent, corner_radius=10)
        box.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(box, text=title, font=(FONT, 15, "bold")).pack(
            anchor="w", padx=12, pady=(8, 2))
        return box

    # ── интерфейс ───────────────────────────────────────────
    def _build(self) -> None:
        f = (FONT, 13)

        # 1. Подготовка
        s1 = self._section(self.root, "1. Подготовка")
        row = ctk.CTkFrame(s1, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 4))
        self.btn_prep = ctk.CTkButton(row, text="Проверить и подготовить", font=f,
                                      command=self.on_prepare, width=210)
        self.btn_prep.pack(side="left")
        self.btn_key = ctk.CTkButton(row, text="Закрыть Claude и прочитать ключ", font=f,
                                     fg_color=WARN, hover_color="#8a4500",
                                     command=self.on_read_key, width=250)
        self.prep_time = ctk.CTkLabel(row, text="", font=(FONT, 12), text_color=IDLE)
        self.prep_time.pack(side="right")
        self.st = {}
        for k, label in [("desktop", "Claude Desktop"), ("key", "Ключ claude.ai"),
                         ("account", "Аккаунт"), ("code", "Claude Code")]:
            r = ctk.CTkFrame(s1, fg_color="transparent")
            r.pack(fill="x", padx=12)
            ctk.CTkLabel(r, text=label + ":", font=f, width=130, anchor="w").pack(side="left")
            v = ctk.CTkLabel(r, text="не проверено", font=f, text_color=IDLE,
                             anchor="w", justify="left", wraplength=640)
            v.pack(side="left", fill="x")
            self.st[k] = v
        ctk.CTkLabel(s1, text="").pack(pady=0)

        # 2. Папка
        s2 = self._section(self.root, "2. Папка для бэкапов")
        row = ctk.CTkFrame(s2, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 10))
        self.dir_var = tkinter.StringVar()
        ctk.CTkEntry(row, textvariable=self.dir_var, font=f, state="readonly").pack(
            side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Изменить…", width=100, font=f,
                      command=self.on_change_dir).pack(side="left", padx=(8, 0))
        ctk.CTkButton(row, text="Открыть", width=90, font=f,
                      command=self.on_open_dir).pack(side="left", padx=(8, 0))

        # 3. Расписание
        s3 = self._section(self.root, "3. Расписание")
        row = ctk.CTkFrame(s3, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 4))
        self.sched_on = tkinter.BooleanVar(value=False)
        ctk.CTkCheckBox(row, text="Делать бэкап каждый день в", font=f,
                        variable=self.sched_on).pack(side="left")
        self.sched_time = tkinter.StringVar(value=win_task.DEFAULT_TIME)
        ctk.CTkEntry(row, textvariable=self.sched_time, width=64, font=f).pack(
            side="left", padx=6)
        ctk.CTkButton(row, text="Применить", width=100, font=f,
                      command=self.on_apply_schedule).pack(side="left", padx=(6, 0))
        self.sched_status = ctk.CTkLabel(s3, text="Проверяю…", font=(FONT, 12),
                                         text_color=IDLE, anchor="w", justify="left")
        self.sched_status.pack(fill="x", padx=12, pady=(0, 10))

        # 4. Бэкапы
        s4 = self._section(self.root, "4. Бэкапы")
        row = ctk.CTkFrame(s4, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 6))
        self.btn_run = ctk.CTkButton(row, text="Сделать бэкап сейчас", font=(FONT, 13, "bold"),
                                     command=self.on_backup, width=200)
        self.btn_run.pack(side="left")
        ctk.CTkButton(row, text="Открыть просмотр чатов", font=f, width=190,
                      command=self.on_open_viewer).pack(side="left", padx=(8, 0))
        ctk.CTkButton(row, text="Журнал", font=f, width=80, fg_color="gray55",
                      command=lambda: _open(log_path())).pack(side="right")
        self.progress = ctk.CTkProgressBar(s4, mode="indeterminate")
        self.summary = ctk.CTkLabel(s4, text="", font=(FONT, 12), text_color=IDLE, anchor="w")
        self.summary.pack(fill="x", padx=12)

        cols = ("time", "trigger", "result", "a", "b", "c", "dur")
        heads = ("Дата", "Запуск", "Результат", "claude.ai", "Кэш Desktop",
                 "Claude Code", "Время")
        widths = (150, 120, 160, 90, 110, 110, 70)
        # ttk не масштабируется вместе с customtkinter — подгоняем вручную
        try:
            scale = ctk.ScalingTracker.get_window_scaling(self.root)
        except Exception:
            scale = 1.0
        style = ttk.Style()
        style.configure("Treeview", font=(FONT, 12), rowheight=int(22 * scale))
        style.configure("Treeview.Heading", font=(FONT, 12, "bold"))
        tv_wrap = ctk.CTkFrame(s4, fg_color="transparent")
        tv_wrap.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        self.tree = ttk.Treeview(tv_wrap, columns=cols, show="headings", height=7)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=int(w * scale), anchor="w")
        sb = ttk.Scrollbar(tv_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("ok", foreground=OK)
        self.tree.tag_configure("warn", foreground=WARN)
        self.tree.tag_configure("bad", foreground=BAD)

        self.logbox = ctk.CTkTextbox(self.root, height=110, font=("Consolas", 11))
        self.logbox.pack(fill="both", expand=True, padx=14, pady=10)
        self.logbox.configure(state="disabled")

    # ── 1. Подготовка ───────────────────────────────────────
    def on_prepare(self, first_run: bool = False, then=None) -> None:
        """then — вызвать после завершения подготовки (цепочка первого запуска)."""
        if self.busy:
            return
        self._set_busy(True, "Подготовка…")
        self.log("Подготовка: ищу Claude Desktop, ключ, Claude Code…")

        def work():
            from .prepare import run_prepare
            try:
                r = run_prepare()
            except Exception as e:  # не роняем окно
                log.error("prepare failed", exc_info=True)
                self.log(f"Ошибка подготовки: {e}")
                self.later(lambda: self._set_busy(False))
                if then:
                    self.later(then)
                return
            self.later(lambda: self._show_prep(r, first_run, then))
        self.bg(work)

    def _show_prep(self, r: dict, first_run: bool, then=None) -> None:
        self.prep = r
        self._set_busy(False)
        self.prep_time.configure(text=f"проверено {r['time'][11:16]}")
        d, k, c = r["desktop"], r["key"], r["claude_code"]

        if d["found"]:
            cache = "кэш есть" if d["has_cache"] else "кэша нет"
            self._st("desktop", OK, f"найден — {d['profile']} ({cache})")
        else:
            self._st("desktop", WARN, "не найден (чаты claude.ai сохраняться не будут, "
                                      "если Claude Desktop не установлен)")

        src = {"desktop": "прочитан из Claude Desktop", "stored": "сохранён ранее",
               "env": "из переменной CLAUDE_SESSION_KEY"}.get(k.get("source") or "")
        need_restart = False
        if k["found"]:
            saved = k.get("stored", {}).get("saved_at")
            self._st("key", OK, src + (f" ({saved})" if k["source"] == "stored" and saved else ""))
        elif k.get("locked"):
            need_restart = True
            self._st("key", WARN, "Claude Desktop открыт и не даёт прочитать ключ — "
                                  "нажмите «Закрыть Claude и прочитать ключ»")
        elif d["found"]:
            self._st("key", BAD, "в Claude Desktop нет входа в аккаунт — войдите и повторите")
        else:
            self._st("key", IDLE, "—")

        on = k.get("online", {})
        if on.get("ok"):
            names = ", ".join(n for n in on.get("org_names", []) if n)
            self._st("account", OK, f"вход подтверждён, организаций: {on.get('orgs', 0)}"
                     + (f" ({names})" if names else ""))
        elif on.get("checked"):
            need_restart = need_restart or (k.get("source") == "stored" and bool(d["found"]))
            self._st("account", BAD, on.get("error", "ошибка"))
        else:
            self._st("account", IDLE, "не проверялся (нет ключа)")

        if c["found"]:
            self._st("code", OK, f"сессий: {c['sessions'].get('real', 0)} — {c['projects_dir']}")
        else:
            self._st("code", IDLE, "не используется на этом ПК")

        if need_restart:
            self.btn_key.pack(side="left", padx=(8, 0))
        else:
            self.btn_key.pack_forget()
        self.refresh_folder()
        self.log("Подготовка завершена.")

        if first_run:
            self._first_run_continue(need_restart)
        elif then:
            then()

    def _first_run_continue(self, need_restart: bool) -> None:
        """Первый запуск: ключ (с согласия) → расписание → первый бэкап."""
        if need_restart and messagebox.askyesno(
                "Ключ claude.ai",
                "Чтобы сохранять чаты claude.ai, нужно на несколько секунд закрыть "
                "Claude Desktop и сразу открыть снова.\n\n" + RESTART_WARNING +
                "\n\nЗакрыть сейчас?"):
            self.on_read_key(then=self._first_run_finish, confirmed=True)
            return
        self._first_run_finish()

    def _first_run_finish(self) -> None:
        conf = cfg.load_config()
        if not conf.get("schedule_auto_done"):
            conf["schedule_auto_done"] = True
            cfg.save_config(conf)

            def work():
                info = win_task.query()
                if not info.get("exists") or not win_task.is_ours(info):
                    ok, err = win_task.install(win_task.DEFAULT_TIME)
                    self.log("Расписание: ежедневно в " + win_task.DEFAULT_TIME
                             if ok else f"Расписание не создано: {err}")
                self.later(self.refresh_task)
            self.bg(work)
        if not (cfg.get_backup_dir() / "manifest.json").is_file():
            self.on_backup()

    def on_read_key(self, then=None, confirmed: bool = False) -> None:
        if self.busy:
            return
        if not confirmed and not messagebox.askyesno(
                "Закрыть Claude Desktop?",
                "Claude Desktop будет закрыт на несколько секунд и открыт снова.\n\n"
                + RESTART_WARNING + "\n\nПродолжить?"):
            return
        self._set_busy(True, "Читаю ключ…")

        def work():
            try:
                from .desktop_control import close_read_relaunch
                res = close_read_relaunch(log=self.log)
            except Exception as e:
                log.error("close_read_relaunch failed", exc_info=True)
                res = {"ok": False, "error": str(e)}

            def done():
                self._set_busy(False)
                if not res.get("ok"):
                    messagebox.showwarning("Ключ не прочитан", res.get("error") or "Ошибка")
                # Повторная подготовка; then — только после её завершения
                self.on_prepare(then=then)
            self.later(done)
        self.bg(work)

    def _st(self, key: str, color: str, text: str) -> None:
        mark = {OK: "✔", WARN: "!", BAD: "✖"}.get(color, "•")
        self.st[key].configure(text=f"{mark} {text}", text_color=color)

    # ── 2. Папка ─────────────────────────────────────────────
    def refresh_folder(self) -> None:
        self.dir_var.set(str(cfg.get_backup_dir()))

    def on_change_dir(self) -> None:
        cur = cfg.get_backup_dir()
        new = filedialog.askdirectory(title="Папка для бэкапов",
                                      initialdir=str(cur if cur.exists() else Path.home()))
        if not new:
            return
        p = Path(new)
        try:
            p.mkdir(parents=True, exist_ok=True)
            (p / ".write-test").write_text("ok", encoding="utf-8")
            (p / ".write-test").unlink()
        except OSError as e:
            messagebox.showerror("Папка недоступна", f"Нет записи в {p}:\n{e}")
            return
        cfg.set_backup_dir(p)
        self.refresh_folder()
        self.refresh_history()
        self.log(f"Папка бэкапов: {p}. Старые бэкапы остаются в {cur}.")

    def on_open_dir(self) -> None:
        d = cfg.get_backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        _open(d)

    # ── 3. Расписание ───────────────────────────────────────
    def refresh_task(self) -> None:
        def work():
            info = win_task.query()
            self.later(lambda: self._show_task(info))
        self.bg(work)

    def _show_task(self, info: dict) -> None:
        if not info.get("exists"):
            self.sched_on.set(False)
            self.sched_status.configure(text="Расписание не задано — бэкап только вручную.",
                                        text_color=WARN)
            return
        self.sched_on.set(True)
        if info.get("at"):
            self.sched_time.set(info["at"])
        parts = [f"Следующий запуск: {info.get('next_run') or '—'}"]
        if info.get("last_run"):
            res = info.get("last_result", 0)
            parts.append(f"последний: {info['last_run']} "
                         f"({EXIT_RU.get(res, 'код ' + str(res)).lower()})")
        if not win_task.is_ours(info):
            parts.append("задача создана другой установкой — нажмите «Применить»")
        self.sched_status.configure(text=" · ".join(parts),
                                    text_color=OK if win_task.is_ours(info) else WARN)

    def on_apply_schedule(self) -> None:
        on, at = self.sched_on.get(), self.sched_time.get().strip()
        if on and not win_task.valid_time(at):
            messagebox.showerror("Время", "Укажите время в формате ЧЧ:ММ, например 03:30")
            return
        self.sched_status.configure(text="Сохраняю…", text_color=IDLE)

        def work():
            ok, err = win_task.install(at) if on else win_task.remove()
            self.log(("Расписание: ежедневно в " + at if on else "Расписание отключено")
                     if ok else f"Ошибка расписания: {err}")
            self.later(self.refresh_task)
        self.bg(work)

    # ── 4. Бэкапы ────────────────────────────────────────────
    def on_backup(self) -> None:
        if self.busy:
            return
        self._set_busy(True, "Идёт бэкап…")
        self.log("Бэкап запущен…")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1", CDB_TRIGGER="manual")
        from datetime import datetime
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def work():
            code = 1
            try:
                p = subprocess.Popen(
                    [_python_console(), "-m", "claude_data_backup.main", "--incremental"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", env=env, creationflags=_NO_WINDOW)
                assert p.stdout is not None
                for line in p.stdout:
                    if line.strip():
                        self.log(line)
                code = p.wait()
            except Exception as e:
                self.log(f"Не удалось запустить бэкап: {e}")

            def done():
                self._set_busy(False)
                self.refresh_history()
                # Запись истории — только если её сделал именно этот прогон
                last = history.read(cfg.get_backup_dir(), 1)
                ec = last[0].get("exit_code", code) \
                    if last and last[0].get("time", "") >= started else code
                self.log(f"Бэкап завершён: {EXIT_RU.get(ec, 'код ' + str(ec))}")
            self.later(done)
        self.bg(work)

    def on_open_viewer(self) -> None:
        idx = cfg.get_backup_dir() / "index.html"
        if idx.is_file():
            _open(idx)
        else:
            messagebox.showinfo("Нет бэкапа", "Сначала сделайте бэкап.")

    def refresh_history(self) -> None:
        bdir = cfg.get_backup_dir()
        self.tree.delete(*self.tree.get_children())
        for rec in history.read(bdir, 200):
            ec = rec.get("exit_code", 1)
            a, b, c = rec.get("mode_a", {}), rec.get("mode_b", {}), rec.get("mode_c", {})

            def cell(m: dict, total_key: str, new_key: str) -> str:
                if m.get("status") != "ok":
                    return "—"
                return f"{m.get(total_key, 0)} (+{m.get(new_key, 0)})"
            dur = rec.get("duration_s", 0)
            self.tree.insert("", "end", tags=("ok" if ec == 0 else "warn" if ec in (3, 4) else "bad",),
                             values=(rec.get("time", ""),
                                     TRIGGER_RU.get(rec.get("trigger"), rec.get("trigger", "")),
                                     EXIT_RU.get(ec, f"код {ec}"),
                                     cell(a, "count", "new"),
                                     cell(b, "cached_total", "new_unique_to_b"),
                                     f"+{c.get('new', 0)}" if c.get("status") == "ok" else "—",
                                     f"{int(dur // 60)}:{int(dur % 60):02d}"))
        if (bdir / "manifest.json").is_file():
            s = mf.summary(mf.load_manifest(bdir))
            last = mf.load_manifest(bdir).get("last_backup_time", "")
            self.summary.configure(
                text=f"В бэкапе: чатов claude.ai — {s.get('conversation_count', 0)}, "
                     f"сессий Claude Code — {s.get('session_count', 0)}. "
                     f"Последнее обновление: {last or '—'}")
        else:
            self.summary.configure(text="Бэкапов в этой папке ещё нет.")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_prep, self.btn_run, self.btn_key):
            b.configure(state=state)
        if busy:
            self.progress.pack(fill="x", padx=12, pady=(0, 6), before=self.summary)
            self.progress.start()
            self.btn_run.configure(text=text or "Подождите…")
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.btn_run.configure(text="Сделать бэкап сейчас")


def main() -> None:
    setup_logging()
    log.info("=== ClaudeDataBackup app_ru v%s ===", __version__)
    root = ctk.CTk()
    SimpleApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

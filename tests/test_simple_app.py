"""Тесты автопоиска профиля, истории, расписания и перезапуска Desktop (fork BUTLER-SPB)."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from claude_data_backup import desktop_control, history, paths, win_task


def _make_profile(d: Path, cookies_age_s: float = 0, cache: bool = True) -> Path:
    (d / "Network").mkdir(parents=True)
    (d / "Local State").write_text("{}", encoding="utf-8")
    ck = d / "Network" / "Cookies"
    ck.write_bytes(b"x")
    t = time.time() - cookies_age_s
    os.utime(ck, (t, t))
    if cache:
        (d / "Cache" / "Cache_Data").mkdir(parents=True)
    return d


@pytest.fixture
def fake_win(tmp_path, monkeypatch):
    roaming, local = tmp_path / "Roaming", tmp_path / "Local"
    roaming.mkdir()
    local.mkdir()
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(paths, "detect_platform", lambda: "win")
    monkeypatch.setattr(paths, "_probe_running_profile_dirs", lambda: [])
    monkeypatch.setattr(paths, "_configured_profile_dir", lambda: None)
    paths.reset_desktop_root_cache()
    yield roaming, local
    paths.reset_desktop_root_cache()


def test_finds_msix_claude_data_profile(fake_win):
    _, local = fake_win
    prof = _make_profile(local / "Claude-Data" / "claude_abc123")
    # старый MSIX-каталог без куки (как после переезда профиля)
    (local / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude").mkdir(parents=True)
    assert paths._claude_desktop_root() == prof
    assert paths.claude_desktop_cookies_path() == prof / "Network" / "Cookies"
    assert paths.claude_desktop_cache_dir() == prof / "Cache" / "Cache_Data"


def test_finds_classic_and_old_msix(fake_win):
    roaming, local = fake_win
    old_msix = _make_profile(local / "Packages" / "Claude_x" / "LocalCache" / "Roaming" / "Claude",
                             cookies_age_s=3600)
    classic = _make_profile(roaming / "Claude", cookies_age_s=7200)
    found = [p.path for p in paths.discover_desktop_profiles()]
    assert found[0] == old_msix          # самый свежий Cookies
    assert classic in found


def test_running_process_wins(fake_win, monkeypatch):
    roaming, local = fake_win
    _make_profile(roaming / "Claude")                       # свежий
    running = _make_profile(local / "Claude-3p", cookies_age_s=86400)
    monkeypatch.setattr(paths, "_probe_running_profile_dirs", lambda: [running])
    paths.reset_desktop_root_cache()
    assert paths._claude_desktop_root() == running


def test_unusable_profile_loses(fake_win, monkeypatch):
    roaming, local = fake_win
    broken = local / "Claude-Data" / "claude_z"
    broken.mkdir(parents=True)
    (broken / "Local State").write_text("{}", encoding="utf-8")   # без Cookies
    monkeypatch.setattr(paths, "_probe_running_profile_dirs", lambda: [broken])
    good = _make_profile(roaming / "Claude")
    paths.reset_desktop_root_cache()
    assert paths._claude_desktop_root() == good


def test_nothing_found_falls_back_to_appdata(fake_win):
    roaming, _ = fake_win
    assert paths._claude_desktop_root() == roaming / "Claude"
    assert not paths.claude_desktop_installed()


def test_claude_config_dir_env(tmp_path, monkeypatch):
    (tmp_path / "cc" / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    assert paths.claude_cli_projects_dir_optional() == tmp_path / "cc" / "projects"


def test_is_desktop_exe_excludes_cli(tmp_path):
    desk = tmp_path / "app"
    (desk / "resources").mkdir(parents=True)
    assert paths._is_desktop_exe(str(desk / "claude.exe"))
    cli = tmp_path / ".local" / "bin"
    cli.mkdir(parents=True)
    assert not paths._is_desktop_exe(str(cli / "claude.exe"))


def test_history_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv(history.TRIGGER_ENV, "schedule")
    started = datetime.now() - timedelta(seconds=5)
    stats = {"mode_a": {"status": "ok", "count": 10, "new": 2, "updated": 1},
             "mode_b": {"status": "skipped", "reason": "x"},
             "mode_c": {"status": "ok", "new": 3, "real": 50}}
    history.append(tmp_path, history.make_record(stats, 0, started))
    history.append(tmp_path, history.make_record(None, 1, started, error="boom"))
    (tmp_path / history.HISTORY_FILE).open("a", encoding="utf-8").write("not json\n")
    recs = history.read(tmp_path)
    assert len(recs) == 2
    assert recs[0]["exit_code"] == 1 and recs[0]["error"] == "boom"
    assert recs[1]["trigger"] == "schedule"
    assert recs[1]["mode_a"]["new"] == 2 and recs[1]["mode_c"]["new"] == 3
    assert recs[1]["duration_s"] >= 5


def test_win_task_script_and_time():
    s = win_task.build_install_script("03:30", Path(r"C:\Users\O'Neil\venv\pythonw.exe"),
                                      Path(r"C:\Users\O'Neil"))
    assert "'C:\\Users\\O''Neil\\venv\\pythonw.exe'" in s
    assert "claude_data_backup.scheduled_run" in s
    assert "-Daily -At '03:30'" in s
    assert "-RunLevel Limited" in s
    assert win_task.valid_time("03:30") and win_task.valid_time("23:59")
    assert not win_task.valid_time("24:00") and not win_task.valid_time("3:5") \
        and not win_task.valid_time("abc")


def test_ps_quote_typographic_apostrophes():
    assert win_task._q("a'b’c") == "'a''b’’c'"


def test_backup_lock_is_exclusive(tmp_path):
    from claude_data_backup import main as cli
    with cli._backup_lock(tmp_path):
        with pytest.raises(cli.BackupBusy):
            with cli._backup_lock(tmp_path):
                pass
    with cli._backup_lock(tmp_path):   # после освобождения снова доступна
        pass


def test_logged_out_desktop_does_not_use_stored_key(monkeypatch):
    from claude_data_backup import cookies
    forgotten = []
    monkeypatch.delenv(cookies.SESSION_KEY_ENV, raising=False)
    monkeypatch.setattr(cookies, "_read_cookie_db", lambda: ([("claude.ai", "other", b"x")], 24))
    monkeypatch.setattr(cookies, "load_stored_session_key", lambda: "sk-ant-old")
    monkeypatch.setattr(cookies, "forget_stored_session_key", lambda: forgotten.append(1))
    assert cookies.get_session_key() is None
    assert forgotten
    assert cookies.read_session_key_from_desktop() == (None, "not_found")


def test_locked_cookies_fall_back_to_stored_key(monkeypatch):
    import sqlite3
    from claude_data_backup import cookies

    def locked():
        raise sqlite3.OperationalError("Cookies DB заблокирован ([WinError 32] ...)")
    monkeypatch.delenv(cookies.SESSION_KEY_ENV, raising=False)
    monkeypatch.setattr(cookies, "_read_cookie_db", locked)
    monkeypatch.setattr(cookies, "load_stored_session_key", lambda: "sk-ant-stored")
    assert cookies.get_session_key() == "sk-ant-stored"
    assert cookies.read_session_key_from_desktop() == (None, "locked")


def test_relaunch_command_msix(tmp_path):
    pkg = tmp_path / "WindowsApps" / "Claude_6.1.0.0_x64__pzs8sxrjxfjjc"
    (pkg / "app").mkdir(parents=True)
    (pkg / "AppxManifest.xml").write_text(
        '<Package><Applications><Application Id="ClaudeApp" Executable="app\\claude.exe"/>'
        '</Applications></Package>', encoding="utf-8")
    cmd = desktop_control.relaunch_command(str(pkg / "app" / "claude.exe"))
    assert cmd == ["explorer.exe", "shell:AppsFolder\\Claude_pzs8sxrjxfjjc!ClaudeApp"]


def test_cli_session_not_rewritten_when_unchanged(tmp_path):
    from claude_data_backup import manifest as mf
    sid = "86a39895-1111-2222-3333-444455556666"
    d = tmp_path / "claude-code" / "real" / "CLAUDE"
    d.mkdir(parents=True)
    (d / "2026-08-18__найди проект__86a39895.md").write_text("x", encoding="utf-8")
    m = mf._empty_manifest()
    mf.register_cli_session(m, sid, {"last_ts": "T1", "file": "claude-code/real/CLAUDE"})
    assert not mf.needs_session_update(m, sid, "T1", backup_dir=tmp_path)
    assert mf.needs_session_update(m, sid, "T2", backup_dir=tmp_path)
    (d / "2026-08-18__найди проект__86a39895.md").unlink()
    assert mf.needs_session_update(m, sid, "T1", backup_dir=tmp_path)


def test_relaunch_command_classic_keeps_cmdline():
    exe = r"C:\Users\u\AppData\Local\AnthropicClaude\claude.exe"
    cl = f'"{exe}" --user-data-dir="C:\\Users\\u\\AppData\\Local\\Claude-3p"'
    assert desktop_control.relaunch_command(exe, cl) == cl
    assert desktop_control.relaunch_command(exe) == [exe]
    assert desktop_control.relaunch_command("") is None

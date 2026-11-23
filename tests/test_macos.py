"""macOS-части, проверяемые на любой ОС: launchd plist, разбор ps, Связка ключей."""
from __future__ import annotations

import plistlib
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from claude_data_backup import cookies, mac_task, paths


def test_build_plist():
    raw = mac_task.build_plist("03:05", ["/u/Library/Application Support/CDB/venv/bin/python3",
                                          "-m", "claude_data_backup.scheduled_run"],
                               Path("/Users/u"), Path("/Users/u/.claude-data-backup/logs"))
    d = plistlib.loads(raw)
    assert d["Label"] == mac_task.LABEL
    assert d["StartCalendarInterval"] == {"Hour": 3, "Minute": 5}
    assert d["ProgramArguments"][0].endswith("venv/bin/python3")   # пробел в пути — ок
    assert d["ProgramArguments"][1:] == ["-m", "claude_data_backup.scheduled_run"]


def test_next_run():
    now = datetime(2026, 11, 23, 10, 0)
    assert mac_task.next_run("03:30", now) == datetime(2026, 11, 24, 3, 30)
    assert mac_task.next_run("23:00", now) == datetime(2026, 11, 23, 23, 0)


def test_query_and_is_ours(tmp_path, monkeypatch):
    monkeypatch.setattr(mac_task, "plist_path", lambda: tmp_path / "a.plist")
    assert mac_task.query() == {"exists": False}
    args = mac_task.program_args()
    (tmp_path / "a.plist").write_bytes(mac_task.build_plist("04:15", args, tmp_path, tmp_path))
    info = mac_task.query()
    assert info["exists"] and info["at"] == "04:15" and mac_task.is_ours(info)
    (tmp_path / "a.plist").write_bytes(mac_task.build_plist("04:15", ["/other/python"], tmp_path, tmp_path))
    assert not mac_task.is_ours(mac_task.query())


HELPER = ("/Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app/"
          "Contents/MacOS/Claude Helper (Renderer)")
PS_COMM = f"""\
  501   501 /Applications/Claude.app/Contents/MacOS/Claude
  502   501 {HELPER}
  503   501 /Users/u/.local/bin/claude
  504   502 /Applications/Claude.app/Contents/MacOS/Claude
  505   501 /usr/bin/python3
"""
PS_CMD = f"""\
  501 /Applications/Claude.app/Contents/MacOS/Claude
  502 {HELPER} --type=renderer --user-data-dir=/Users/u/Library/Application Support/Claude --lang=ru
  503 /Users/u/.local/bin/claude --print
  504 /Applications/Claude.app/Contents/MacOS/Claude
  505 /usr/bin/python3 /tmp/x.py /Applications/Claude.app/Contents/MacOS/Claude
"""


def test_parse_mac_ps():
    procs = paths.parse_mac_ps(PS_COMM, PS_CMD, uid=501)
    pids = [p.pid for p in procs]
    assert 501 in pids and 502 in pids
    assert 503 not in pids          # Claude Code CLI
    assert 504 not in pids          # другой пользователь
    assert 505 not in pids          # путь к Claude.app только в аргументах
    main = [p for p in procs if p.is_main and p.pid == 501][0]
    assert main.exe == "/Applications/Claude.app/Contents/MacOS/Claude"
    helper = [p for p in procs if p.pid == 502][0]
    assert not helper.is_main and helper.exe == HELPER


def test_user_data_dir_with_space_on_mac():
    cmd = PS_CMD.splitlines()[1]
    assert paths.user_data_dir_from_cmdline(cmd, "mac") == \
        Path("/Users/u/Library/Application Support/Claude")
    win = r'"C:\A\Claude.exe" --user-data-dir=C:\Users\M\Claude-Data\claude_x /prefetch:4'
    assert paths.user_data_dir_from_cmdline(win, "win") == Path(r"C:\Users\M\Claude-Data\claude_x")
    winq = r'x --user-data-dir="C:\Users\Иван Петров\Claude" --type=gpu'
    assert paths.user_data_dir_from_cmdline(winq, "win") == Path(r"C:\Users\Иван Петров\Claude")


def test_keychain_timeout_stops_immediately(monkeypatch):
    calls = []

    def run(cmd, **k):
        calls.append(cmd[-1])
        raise subprocess.TimeoutExpired(cmd, 120)
    monkeypatch.setattr(cookies.subprocess, "run", run)
    with pytest.raises(cookies.KeychainAccessError):
        cookies._mac_find_safe_storage_key()
    assert len(calls) == 1


def test_keychain_localized_not_found(monkeypatch):
    # стderr на другом языке, код 44 → «записи нет», а не «доступ запрещён»
    monkeypatch.setattr(cookies.subprocess, "run",
                        lambda *a, **k: _R(44, err="Объект не найден".encode()))
    assert cookies._mac_find_safe_storage_key() is None


class _R:
    def __init__(self, rc, out=b"", err=b""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_keychain_not_found_returns_none(monkeypatch):
    monkeypatch.setattr(cookies.subprocess, "run",
                        lambda *a, **k: _R(44, err=b"The specified item could not be found in the keychain."))
    assert cookies._mac_find_safe_storage_key() is None


def test_keychain_denied_raises(monkeypatch):
    def run(cmd, **k):
        svc = cmd[-1]
        if svc == "Claude Safe Storage":
            return _R(51, err=b"User canceled the operation.")
        return _R(44, err=b"could not be found")
    monkeypatch.setattr(cookies.subprocess, "run", run)
    with pytest.raises(cookies.KeychainAccessError):
        cookies._mac_find_safe_storage_key()


def test_keychain_ok_derives_key(monkeypatch):
    monkeypatch.setattr(cookies.subprocess, "run", lambda *a, **k: _R(0, out=b"secretpass\n"))
    key = cookies._mac_find_safe_storage_key()
    assert isinstance(key, bytes) and len(key) == 16


def test_read_key_maps_keychain_error(monkeypatch):
    def boom():
        raise cookies.KeychainAccessError("denied")
    monkeypatch.setattr(cookies, "_session_key_from_cookie_db", boom)
    assert cookies.read_session_key_from_desktop() == (None, "keychain")


def test_mac_decrypt_roundtrip():
    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Hash import SHA1
    import hashlib
    key = PBKDF2("pw", b"saltysalt", dkLen=16, count=1003, hmac_hash_module=SHA1)
    host, value = ".claude.ai", b"sk-ant-sid01-abc"
    pt = hashlib.sha256(host.encode()).digest() + value
    pad = 16 - len(pt) % 16
    enc = b"v10" + AES.new(key, AES.MODE_CBC, b" " * 16).encrypt(pt + bytes([pad]) * pad)
    assert cookies._mac_decrypt(enc, key, host, 24) == value.decode()

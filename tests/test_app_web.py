"""Тесты Python-части окна app_web (без запуска WebView)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claude_data_backup import app_web, config as cfg, history


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    bdir = tmp_path / "backup"
    cfg.set_backup_dir(bdir)
    return bdir


def test_get_state_empty(tmp_config):
    s = app_web.Api().get_state()
    assert s["first_run"] is True
    assert s["has_backup"] is False and s["history"] == []
    assert s["backup_dir"] == str(tmp_config)
    json.dumps(s)  # всё должно сериализоваться для JS


def test_get_state_with_history(tmp_config):
    from datetime import datetime
    tmp_config.mkdir(parents=True)
    history.append(tmp_config, history.make_record(
        {"mode_c": {"status": "ok", "new": 2, "real": 2}}, 0, datetime.now()))
    s = app_web.Api().get_state()
    assert len(s["history"]) == 1 and s["history"][0]["exit_code"] == 0


def test_compact_prep_has_no_secrets():
    r = {"time": "t", "desktop": {"found": True, "profile": "p", "candidates": [1, 2]},
         "key": {"found": True, "source": "desktop", "stored": {"saved_at": "x"}},
         "claude_code": {"found": True, "projects_dir": "d", "sessions": {"real": 5}}}
    c = app_web._compact_prep(r)
    assert c["code"]["sessions"] == 5 and "candidates" not in c["desktop"]
    assert c["key"]["saved_at"] == "x"


def test_read_key_timeout_relaunches_and_releases(monkeypatch, tmp_config):
    from claude_data_backup import desktop_control, paths
    launched = []
    monkeypatch.setattr(desktop_control, "current_relaunch_command", lambda: ["claude.exe"])
    monkeypatch.setattr(desktop_control, "launch", lambda cmd: launched.append(cmd) or True)
    monkeypatch.setattr(paths, "list_desktop_processes", lambda: [])   # Claude закрыт

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=90)
    monkeypatch.setattr(app_web.subprocess, "run", hang)
    api = app_web.Api()
    res = api.read_key()
    assert res["ok"] is False and "остановлено" in res["error"]
    assert launched == [["claude.exe"]] and res["relaunched"] is True
    assert not api._busy.locked()            # окно не остаётся «занятым»


def test_read_key_parses_helper_json(monkeypatch, tmp_config):
    from claude_data_backup import desktop_control, paths
    monkeypatch.setattr(desktop_control, "current_relaunch_command", lambda: None)
    monkeypatch.setattr(paths, "list_desktop_processes", lambda: [])

    class R:
        stdout = 'noise\n{"ok": true, "error": null, "relaunched": true}\n'
        stderr = ""
    monkeypatch.setattr(app_web.subprocess, "run", lambda *a, **k: R())
    assert app_web.Api().read_key()["ok"] is True


def test_busy_blocks_parallel_operations(tmp_config):
    api = app_web.Api()
    api._busy.acquire()
    try:
        assert api.backup()["started"] is False
        assert api.read_key()["ok"] is False
    finally:
        api._busy.release()


def test_web_assets_packaged():
    idx = app_web.WEB_DIR / "index.html"
    html = idx.read_text(encoding="utf-8")
    assert "pywebviewready" in html and "onBackend" in html
    assert "http://" not in html and "https://" not in html   # без внешних ресурсов

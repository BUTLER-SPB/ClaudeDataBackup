"""Mode C: jsonl в бэкап только копией, починка сессий-ссылок (v0.6.1)."""
from __future__ import annotations

import os
import time

from claude_data_backup import renderer, session_guard


def _src(tmp_path, text="a\n"):
    d = tmp_path / "projects" / "p"
    d.mkdir(parents=True)
    f = d / "s.jsonl"
    f.write_text(text, encoding="utf-8")
    return f


def test_copy_new_is_regular_file(tmp_path):
    src = _src(tmp_path)
    dst = tmp_path / "b.jsonl"
    assert renderer.copy_if_changed(src, dst) is True
    assert dst.read_text(encoding="utf-8") == "a\n"
    assert os.stat(src).st_nlink == 1 and not os.path.samefile(src, dst)
    assert not (tmp_path / "b.jsonl.cdb-tmp").exists()


def test_old_hardlink_is_replaced_by_copy(tmp_path):
    src = _src(tmp_path)
    dst = tmp_path / "b.jsonl"
    os.link(src, dst)
    assert renderer.copy_if_changed(src, dst) is True
    assert not os.path.samefile(src, dst)
    assert os.stat(src).st_nlink == 1
    assert dst.read_text(encoding="utf-8") == "a\n"


def test_unchanged_skipped_appended_copied(tmp_path):
    src = _src(tmp_path)
    dst = tmp_path / "b.jsonl"
    renderer.copy_if_changed(src, dst)
    m = dst.stat().st_mtime_ns
    assert renderer.copy_if_changed(src, dst) is False
    assert dst.stat().st_mtime_ns == m
    with open(src, "a", encoding="utf-8") as f:
        f.write("b\n")
    assert renderer.copy_if_changed(src, dst) is True
    assert dst.read_text(encoding="utf-8") == "a\nb\n"


def test_guard_repairs_old_hardlink(tmp_path):
    src = _src(tmp_path)
    link = tmp_path / "backup.jsonl"
    os.link(src, link)
    old = time.time() - 3600
    os.utime(src, (old, old))
    r = session_guard.repair_sessions(tmp_path / "projects")
    assert r.checked == 1 and len(r.fixed) == 1
    assert os.stat(src).st_nlink == 1 and not os.path.samefile(src, link)
    assert src.read_text(encoding="utf-8") == "a\n"
    assert abs(src.stat().st_mtime - old) < 2     # время изменения сохранено


def test_guard_leaves_recent_session(tmp_path):
    src = _src(tmp_path)
    os.link(src, tmp_path / "backup.jsonl")
    r = session_guard.repair_sessions(tmp_path / "projects")
    assert len(r.recent) == 1 and not r.fixed
    assert os.stat(src).st_nlink == 2               # активную сессию не трогаем


def test_guard_placeholder_detected(tmp_path, monkeypatch):
    src = _src(tmp_path)
    old = time.time() - 3600
    os.utime(src, (old, old))
    seen = []

    def fake_problem(p):
        seen.append(p)
        return "placeholder" if len(seen) == 1 else None
    monkeypatch.setattr(session_guard, "problem", fake_problem)
    r = session_guard.repair_sessions(tmp_path / "projects")
    assert len(r.fixed) == 1 and r.to_dict()["found"] == 1


def test_guard_clean_tree(tmp_path):
    _src(tmp_path)
    r = session_guard.repair_sessions(tmp_path / "projects")
    assert r.checked == 1 and r.found == 0


def test_truncated_last_line_is_ignored(tmp_path):
    from claude_data_backup import cli_exporter
    f = _src(tmp_path, '{"type":"user","sessionId":"abc","message":{"content":"hi"}}\n{"type":"assi')
    s = cli_exporter.parse_session(f, "real", "p")
    assert s is None or s.total_events == 1

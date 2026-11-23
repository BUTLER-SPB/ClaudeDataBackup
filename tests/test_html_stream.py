"""Потоковая запись index.html: тот же результат, атомарность при сбое."""
from __future__ import annotations

import base64
import os

import pytest

from claude_data_backup import html_viewer


def test_streamed_equals_plain_base64(tmp_path, monkeypatch):
    monkeypatch.setattr(html_viewer, "_B64_CHUNK", 3 * 7)   # много мелких кусков
    data = os.urandom(1000) + "привет 👋".encode("utf-8")
    out = tmp_path / "index.html"
    html_viewer.write_html_streamed(out, "<h>", data, "</t>")
    assert out.read_bytes() == b"<h>" + base64.b64encode(data) + b"</t>"
    assert not (tmp_path / "index.html.tmp").exists()


def test_failure_keeps_old_file(tmp_path, monkeypatch):
    out = tmp_path / "index.html"
    out.write_text("OLD", encoding="utf-8")

    def boom(_chunk):
        raise MemoryError()
    monkeypatch.setattr(html_viewer.base64, "b64encode", boom)
    with pytest.raises(MemoryError):
        html_viewer.write_html_streamed(out, "<h>", b"abc", "</t>")
    assert out.read_text(encoding="utf-8") == "OLD"
    assert not (tmp_path / "index.html.tmp").exists()


def test_mode_a_error_is_not_success():
    from claude_data_backup import main as cli
    ok_c = {"status": "ok", "new": 1}
    st = {"mode_a": {"status": "error", "reason": "403"}, "mode_b": {"status": "ok"}, "mode_c": ok_c}
    assert cli._determine_exit_code(st, "abc") == cli.EXIT_PARTIAL
    st["mode_a"] = {"status": "skipped", "reason": "No organization under account"}
    assert cli._determine_exit_code(st, "abc") == cli.EXIT_PARTIAL
    st["mode_a"] = {"status": "ok", "count": 3}
    assert cli._determine_exit_code(st, "abc") == cli.EXIT_SUCCESS
    assert cli._determine_exit_code({"mode_c": ok_c}, "c") == cli.EXIT_SUCCESS


def test_probe_result_reused(monkeypatch):
    from claude_data_backup import api_fetcher
    calls = []
    f = api_fetcher.ApiFetcher.__new__(api_fetcher.ApiFetcher)
    monkeypatch.setattr(f, "_get", lambda path, retries=3: calls.append(path) or [{"uuid": "o1"}],
                        raising=False)
    assert f.probe() is True
    assert f.list_organizations() == [{"uuid": "o1"}]
    assert calls == ["/organizations"]       # без второго запроса


def test_generate_html_end_to_end(tmp_path):
    out = html_viewer.generate_html(tmp_path)
    txt = out.read_text(encoding="utf-8")
    assert "__DATA_PLACEHOLDER__" not in txt and "__I18N_PLACEHOLDER__" not in txt
    assert "__SIDEBAR_SUBTITLE__" not in txt

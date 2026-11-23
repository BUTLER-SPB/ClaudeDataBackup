"""Тесты доработок форка: cookie-префикс, пагинация, stale-copy, мульти-org, знания проектов."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from Crypto.Cipher import AES

from claude_data_backup import api_fetcher, cookies, main as cli
from claude_data_backup.api_fetcher import ApiError, ApiFetcher


# ---------- cookies ----------

def _gcm_encrypt(key: bytes, plaintext: bytes, prefix: bytes = b"v10") -> bytes:
    nonce = os.urandom(12)
    c = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = c.encrypt_and_digest(plaintext)
    return prefix + nonce + ct + tag


def test_win_decrypt_strips_host_hash_v24():
    key = os.urandom(32)
    host = ".claude.ai"
    value = b"sk-ant-sid02-abcdef"
    enc = _gcm_encrypt(key, hashlib.sha256(host.encode()).digest() + value)
    assert cookies._win_decrypt(enc, key, host, 24) == value.decode()


def test_win_decrypt_hash_detected_even_if_version_unknown():
    key = os.urandom(32)
    host = "claude.ai"
    value = b"sk-ant-sid02-xyz"
    enc = _gcm_encrypt(key, hashlib.sha256(host.encode()).digest() + value)
    assert cookies._win_decrypt(enc, key, host, 0) == value.decode()


def test_win_decrypt_old_db_no_prefix():
    key = os.urandom(32)
    value = b"sk-ant-sid01-" + b"a" * 60  # длиннее 32 байт, не должен резаться
    enc = _gcm_encrypt(key, value)
    assert cookies._win_decrypt(enc, key, ".claude.ai", 23) == value.decode()


def test_win_decrypt_v20_returns_none():
    assert cookies._win_decrypt(b"v20" + b"\x00" * 40, os.urandom(32), ".claude.ai", 24) is None


def test_env_override(monkeypatch):
    monkeypatch.setenv(cookies.SESSION_KEY_ENV, "  sk-ant-sid02-env  ")
    assert cookies.get_session_key() == "sk-ant-sid02-env"
    assert cookies.describe_cookie_state()["has_session_key"] is True


# ---------- api_fetcher ----------

def _meta(i: int, updated: str = "2026-01-01") -> dict:
    return {"uuid": f"u{i}", "name": f"c{i}", "updated_at": updated}


def test_pagination_real(monkeypatch):
    items = [_meta(i) for i in range(250)]
    calls = []

    def fake_get(self, path, retries=3):
        calls.append(path)
        off = int(path.split("offset=")[1])
        return items[off:off + api_fetcher.CONVERSATIONS_PAGE_SIZE]

    monkeypatch.setattr(ApiFetcher, "_get", fake_get)
    f = ApiFetcher("sk")
    assert len(f.list_conversations("org")) == 250
    assert len(calls) == 2


def test_pagination_ignored_by_server(monkeypatch):
    items = [_meta(i) for i in range(250)]
    calls = []

    def fake_get(self, path, retries=3):
        calls.append(path)
        return items  # всегда весь список

    monkeypatch.setattr(ApiFetcher, "_get", fake_get)
    assert len(ApiFetcher("sk").list_conversations("org")) == 250
    assert len(calls) == 2  # вторая страница без новых uuid -> стоп


def test_pagination_fallback_without_params(monkeypatch):
    def fake_get(self, path, retries=3):
        if "limit=" in path:
            raise ApiError("400")
        return [_meta(1), _meta(2)]

    monkeypatch.setattr(ApiFetcher, "_get", fake_get)
    assert [c["uuid"] for c in ApiFetcher("sk").list_conversations("org")] == ["u1", "u2"]


def test_stream_all_refetches_stale_disk_copy(monkeypatch, tmp_path):
    (tmp_path / "u1.json").write_text(json.dumps({"uuid": "u1", "updated_at": "OLD"}))
    (tmp_path / "u2.json").write_text(json.dumps({"uuid": "u2", "updated_at": "SAME"}))
    fetched = []
    monkeypatch.setattr(ApiFetcher, "list_conversations",
                        lambda self, org: [_meta(1, "NEW"), _meta(2, "SAME")])

    def fake_fetch(self, org, uuid):
        fetched.append(uuid)
        return {"uuid": uuid, "updated_at": "NEW", "chat_messages": []}

    monkeypatch.setattr(ApiFetcher, "fetch_conversation", fake_fetch)
    f = ApiFetcher("sk", sleep_between=0)
    out = {c["uuid"]: c for c in f.stream_all("org", save_dir=tmp_path, skip_map={"u1": "OLD"})}
    assert fetched == ["u1"]
    assert out["u1"]["updated_at"] == "NEW"
    assert out["u2"]["updated_at"] == "SAME"
    assert json.loads((tmp_path / "u1.json").read_text())["updated_at"] == "NEW"


def test_orgs_filter_without_chat(monkeypatch):
    monkeypatch.setattr(ApiFetcher, "list_organizations", lambda self: [
        {"uuid": "o1", "capabilities": ["chat", "claude_pro"]},
        {"uuid": "o2", "capabilities": ["api"]},
        {"uuid": "o3"},
    ])
    assert [o["uuid"] for o in ApiFetcher("sk").list_organizations_for_backup()] == ["o1", "o3"]


# ---------- main: мульти-org + знания проектов ----------

class FakeFetcher:
    def __init__(self, sk, sleep_between=0.5):
        pass

    def probe(self):
        return True

    def list_organizations_for_backup(self):
        return [{"uuid": "org-aaaa1111", "name": "Personal"},
                {"uuid": "org-bbbb2222", "name": "Team"}]

    def stream_all(self, org_uuid, save_dir=None, progress=None, skip_map=None):
        yield {"uuid": f"{org_uuid}-c1", "updated_at": "x", "chat_messages": []}

    def list_projects(self, org_uuid):
        return [{"uuid": f"{org_uuid}-p1", "name": "CRM"}] if org_uuid.endswith("2222") else []

    def get_project(self, org_uuid, p_uuid):
        return {"uuid": p_uuid, "name": "CRM", "prompt_template": "Отвечай по-русски"}

    def list_project_docs(self, org_uuid, p_uuid):
        return [{"uuid": "d1", "file_name": "arch.md", "content": "# Arch"},
                {"uuid": "d2", "file_name": "arch.md", "content": "# Dup"}]


def test_mode_a_core_all_orgs_and_knowledge(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "ApiFetcher", FakeFetcher)
    monkeypatch.setattr(cli.cookies, "get_session_key", lambda: "sk")
    convs, stats, new, upd = cli._run_mode_a_core(tmp_path, lambda m: None, skip_map={})
    assert set(convs) == {"org-aaaa1111-c1", "org-bbbb2222-c1"}
    assert stats["org_uuids"] == ["org-aaaa1111", "org-bbbb2222"]
    assert stats["project_knowledge"] == {"projects": 1, "docs": 2}
    p_dir = tmp_path / "project-knowledge" / "Team" / "CRM"
    assert (p_dir / "INSTRUCTIONS.md").read_text(encoding="utf-8") == "Отвечай по-русски"
    assert sorted(x.name for x in (p_dir / "docs").iterdir()) == ["arch.md", "d2_arch.md"]
    assert new == 2 and upd == 0

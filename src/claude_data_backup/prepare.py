"""«Подготовка»: найти всё, что нужно для бэкапа, без действий пользователя.

- профиль Claude Desktop (все известные варианты путей, запущенный процесс);
- ключ claude.ai (куки Desktop → сохранённый ключ), проверка входа онлайн;
- кэш Desktop (Mode B), сессии Claude Code (Mode C);
- папка бэкапа (создаётся, проверяется запись).
Найденный профиль сохраняется в config.json (desktop_profile_dir).
Сам ключ никогда не возвращается и не пишется в лог.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import cli_exporter, cookies, paths
from . import config as cfg


def _check_writable(d: Path) -> tuple[bool, str]:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, ""
    except OSError as e:
        return False, str(e)


def _verify_online(sk: str) -> dict:
    from .api_fetcher import ApiError, ApiFetcher
    try:
        f = ApiFetcher(sk, sleep_between=0)
        if not f.probe():
            return {"checked": True, "ok": False, "error": "Ключ не принят claude.ai (выход из аккаунта или ключ устарел)"}
        orgs = f.list_organizations()
        return {"checked": True, "ok": True, "orgs": len(orgs),
                "org_names": [o.get("name", "") for o in orgs][:5]}
    except ApiError as e:
        return {"checked": True, "ok": False, "error": str(e)}
    except Exception as e:  # сеть, прокси и т.п.
        return {"checked": True, "ok": False, "error": f"Нет связи с claude.ai: {e}"}


def run_prepare(verify_online: bool = True) -> dict:
    paths.reset_desktop_root_cache()
    r: dict = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # 1. Профиль Claude Desktop
    profiles = paths.discover_desktop_profiles()
    best = profiles[0] if profiles and profiles[0].usable else None
    r["desktop"] = {
        "found": best is not None,
        "profile": str(best.path) if best else None,
        "source": best.source if best else None,
        "has_cache": bool(best and best.has_cache),
        "candidates": [p.to_dict() for p in profiles],
        "running": bool(paths.list_desktop_processes()),
    }
    if best:
        conf = cfg.load_config()
        conf["desktop_profile_dir"] = str(best.path)
        cfg.save_config(conf)

    # 2. Ключ claude.ai
    key = {"source": None, "locked": False, "error": None}
    sk = cookies.session_key_from_env()
    if sk:
        key["source"] = "env"
    elif best:
        sk, err = cookies.read_session_key_from_desktop()
        if sk:
            cookies.store_session_key(sk)
            key["source"] = "desktop"
        else:
            key["locked"] = err == "locked"
            key["error"] = err
            if err == "not_found":   # вышли из аккаунта — старый ключ не используем
                cookies.forget_stored_session_key()
    if not sk and key["error"] != "not_found":
        sk = cookies.load_stored_session_key()
        if sk:
            key["source"] = "stored"
    key["stored"] = cookies.stored_key_info()
    key["found"] = sk is not None
    key["online"] = _verify_online(sk) if (sk and verify_online) else {"checked": False}
    r["key"] = key

    # 3. Claude Code
    proj = paths.claude_cli_projects_dir_optional()
    r["claude_code"] = {
        "found": proj is not None,
        "projects_dir": str(proj) if proj else None,
        "sessions": cli_exporter.count_sessions() if proj else {"real": 0},
    }

    # 4. Папка бэкапа
    bdir = cfg.get_backup_dir()
    ok, err = _check_writable(bdir)
    r["backup_dir"] = {"path": str(bdir), "writable": ok, "error": err}

    conf = cfg.load_config()
    conf["prepared_at"] = r["time"]
    cfg.save_config(conf)
    return r


if __name__ == "__main__":
    import json
    print(json.dumps(run_prepare(), ensure_ascii=False, indent=2))

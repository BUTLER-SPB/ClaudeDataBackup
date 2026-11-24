"""История прогонов бэкапа (fork BUTLER-SPB).

Каждый инкрементальный прогон (ручной из окна, ночной по расписанию, CLI)
дописывает одну строку JSON в <backup_dir>/history.jsonl. Окно приложения
показывает эту историю, чтобы сотрудник видел, что бэкапы реально создаются.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

HISTORY_FILE = "history.jsonl"
TRIGGER_ENV = "CDB_TRIGGER"   # manual | schedule | cli (по умолчанию)


def history_path(backup_dir: Path) -> Path:
    return Path(backup_dir) / HISTORY_FILE


def current_trigger() -> str:
    return (os.environ.get(TRIGGER_ENV) or "cli").strip() or "cli"


def make_record(stats: dict | None, exit_code: int, started: datetime,
                error: str | None = None) -> dict:
    stats = stats or {}
    a = stats.get("mode_a") or {}
    b = stats.get("mode_b") or {}
    c = stats.get("mode_c") or {}
    finished = datetime.now()
    rec = {
        "time": started.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": round((finished - started).total_seconds(), 1),
        "trigger": current_trigger(),
        "exit_code": exit_code,
        "mode_a": {"status": a.get("status", ""), "count": a.get("count", 0),
                   "new": a.get("new", 0), "updated": a.get("updated", 0),
                   "reason": a.get("reason", "")},
        "mode_b": {"status": b.get("status", ""), "cached_total": b.get("cached_total", 0),
                   "new_unique_to_b": b.get("new_unique_to_b", 0),
                   "reason": b.get("reason", "")},
        "mode_c": {"status": c.get("status", ""), "new": c.get("new", 0),
                   "real": c.get("real", 0), "reason": c.get("reason", "")},
    }
    v = stats.get("viewer") or {}
    if v.get("status") == "error":
        rec["viewer"] = {"status": "error", "reason": v.get("reason", "")}
    if error:
        rec["error"] = error[-2000:]
    return rec


def append(backup_dir: Path, record: dict) -> None:
    p = history_path(backup_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # история — вспомогательная, бэкап из-за неё не падает


def read(backup_dir: Path, limit: int = 100) -> list[dict]:
    """Последние записи, новые первыми. Битые строки пропускаются."""
    p = history_path(backup_dir)
    if not p.is_file():
        return []
    out: list[dict] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out

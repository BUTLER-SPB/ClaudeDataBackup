"""Проверка и починка исходных сессий Claude Code (fork BUTLER-SPB, v0.6.1).

До v0.6.1 Mode C клал jsonl в бэкап hardlink-ом. Если папка бэкапа лежит в
облачной папке (Яндекс.Диск, OneDrive), облако превращает файл в плейсхолдер
(reparse point), а у hardlink одна запись файла на все имена — плейсхолдером
становится и оригинал в ~/.claude/projects. Claude Desktop такую сессию не
открывает: «looks like a link or a placeholder, not a regular file».

Перед бэкапом ищем такие файлы (reparse point или больше одного имени) и
пересоздаём обычной копией: копия во временный файл рядом + os.replace.
Свежие файлы (активная сессия может писаться прямо сейчас) не трогаем —
их связь с бэкапом рвёт copy_if_changed, а починка — на следующем прогоне.
"""
from __future__ import annotations

import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .renderer import atomic_copy

MIN_AGE_S = 600   # не трогать файлы, изменённые за последние 10 минут

# Ссылки, которые пользователь мог сделать сам, — не наш случай, не трогаем
_LINK_TAGS = {0xA000000C, 0xA0000003}   # IO_REPARSE_TAG_SYMLINK, MOUNT_POINT


@dataclass
class GuardResult:
    checked: int = 0
    fixed: list[str] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def found(self) -> int:
        return len(self.fixed) + len(self.recent) + len(self.failed)

    def to_dict(self) -> dict:
        return {"checked": self.checked, "found": self.found, "fixed": len(self.fixed),
                "recent": len(self.recent), "failed": len(self.failed)}


def problem(p: Path) -> str | None:
    """'placeholder' | 'hardlink' | None."""
    st = os.lstat(p)
    if sys.platform == "win32":
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            if getattr(st, "st_reparse_tag", 0) in _LINK_TAGS:
                return None
            return "placeholder"
    elif stat.S_ISLNK(st.st_mode):
        return None
    if st.st_nlink > 1:
        return "hardlink"
    return None


def repair_sessions(projects_dir: Path, min_age_s: int = MIN_AGE_S,
                    now: float | None = None) -> GuardResult:
    now = time.time() if now is None else now
    r = GuardResult()
    for p in projects_dir.rglob("*.jsonl"):
        try:
            r.checked += 1
            kind = problem(p)
            if kind is None:
                continue
            if now - os.lstat(p).st_mtime < min_age_s:
                r.recent.append(str(p))
                continue
            atomic_copy(p, p)
            if problem(p) is None:
                r.fixed.append(str(p))
            else:
                r.failed.append(str(p))
        except OSError:
            r.failed.append(str(p))
    return r

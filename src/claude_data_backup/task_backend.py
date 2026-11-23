"""Выбор механизма расписания по платформе: Планировщик Windows или launchd."""
from __future__ import annotations

import sys


def get():
    """Модуль с install/remove/run_now/query/is_ours/valid_time/DEFAULT_TIME или None."""
    if sys.platform == "win32":
        from . import win_task
        return win_task
    if sys.platform == "darwin":
        from . import mac_task
        return mac_task
    return None

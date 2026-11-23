"""Точка входа для ночного запуска из Планировщика (pythonw, без консоли).

Вывод CLI пишется в ~/.claude-data-backup/logs/scheduled-last.log,
результат прогона — в history.jsonl папки бэкапа (это делает main.run_incremental).
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    os.environ["CDB_TRIGGER"] = "schedule"
    os.environ.setdefault("PYTHONUTF8", "1")
    from .config import CONFIG_DIR
    log_dir = CONFIG_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out = open(log_dir / "scheduled-last.log", "w", encoding="utf-8", buffering=1)
    sys.stdout = out
    sys.stderr = out
    sys.argv = ["claude-data-backup", "--incremental"]
    code = 0
    try:
        from .main import main as cli_main
        cli_main()
    except SystemExit as e:
        code = int(e.code or 0) if isinstance(e.code, int) or e.code is None else 1
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        print(f"\n[scheduled_run] exit_code={code}")
        out.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())

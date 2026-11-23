"""CLI 入口 —— 编排 Mode A/B/C，合并结果，写 Markdown + JSON/JSONL 输出。

用法：
    claude-data-backup --output ~/Desktop/my-export              # 一次性导出
    claude-data-backup --incremental                              # 增量备份
    claude-data-backup --incremental --mode a                     # 只增量 Mode A
    claude-data-backup --set-backup-dir ~/my-backup               # 修改备份目录
    claude-data-backup --mode bc                                  # 只跑 Mode B 和 Mode C
"""
from __future__ import annotations
import argparse
import json
import sys
import traceback
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from . import paths
from . import cookies
from . import cache_extractor
from . import cli_exporter
from . import renderer
from . import config as cfg
from . import manifest as mf
from . import history
from .i18n import t as _, init_language, save_language_preference
from .log import setup_logging, get_logger
from .html_viewer import generate_html as generate_html_viewer
from .api_fetcher import ApiFetcher, ApiError
from .file_extractor import extract_all_files

log = get_logger(__name__)

# ── 退出码常量 ──
EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_INVALID_ARGS = 2
EXIT_AUTH_FAILURE = 3
EXIT_PARTIAL = 4

CLI_INDEX_ROWS = "_index_rows.json"   # накопленные строки claude-code/00_index.md

_SUBCOMMANDS = {"capabilities", "list", "status"}


def _emit_json(data: dict, file=sys.stdout) -> None:
    """将 data 作为 JSON 输出到指定流。"""
    json.dump(data, file, ensure_ascii=False, indent=2)
    print(file=file)


def _make_json_logger() -> tuple[Callable[[str], None], list[str]]:
    """返回 (logger_fn, messages)。logger 追加到 list 而非 print。"""
    messages: list[str] = []

    def _log(msg: str) -> None:
        messages.append(msg)

    return _log, messages


def _default_output_dir() -> Path:
    date = datetime.now().strftime("%Y-%m-%d")
    return Path.home() / "Desktop" / f"ClaudeDataBackup-{date}"


def _emit(msg: str, verbose: bool, always: bool = False) -> None:
    if verbose or always:
        print(msg, flush=True)


# ---------- 三模式执行 ----------

def _resolve_session_key(logger: Callable[[str], None]) -> str | None:
    sk = cookies.get_session_key()
    if not sk:
        logger(_("main.mode_a_no_key"))
    return sk


def _backup_project_knowledge(fetcher: ApiFetcher, org: dict, out_root: Path,
                              projects: list[dict],
                              logger: Callable[[str], None]) -> dict:
    """Сохранить инструкции и документы знаний проектов организации.

    Структура: <out_root>/project-knowledge/<org>/<project>/
        project.json, INSTRUCTIONS.md, docs/<file_name>
    Ошибки отдельных проектов не прерывают бэкап.
    """
    org_uuid = org["uuid"]
    org_dir = out_root / "project-knowledge" / renderer.safe_name(
        org.get("name") or org_uuid[:8], 60)
    n_projects = n_docs = 0
    for p in projects:
        p_uuid = p.get("uuid")
        if not p_uuid:
            continue
        p_dir = org_dir / renderer.safe_name(p.get("name") or p_uuid[:8], 80)
        try:
            details = fetcher.get_project(org_uuid, p_uuid) or p
            docs = fetcher.list_project_docs(org_uuid, p_uuid)
        except ApiError as e:
            log.warning("Проект %s: %s", p_uuid[:8], e)
            logger(_("main.mode_a_project_fail", name=p.get("name", p_uuid[:8]), error=str(e)))
            continue
        (p_dir / "docs").mkdir(parents=True, exist_ok=True)
        (p_dir / "project.json").write_text(
            json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
        instructions = details.get("prompt_template") or ""
        if instructions:
            (p_dir / "INSTRUCTIONS.md").write_text(instructions, encoding="utf-8")
        (p_dir / "docs.json").write_text(
            json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
        used: set[str] = set()
        for d in docs:
            content = d.get("content")
            if content is None:
                continue
            fname = renderer.safe_name(d.get("file_name") or d.get("uuid", "doc"), 120)
            if fname in used:
                fname = f"{(d.get('uuid') or 'dup')[:8]}_{fname}"
            used.add(fname)
            (p_dir / "docs" / fname).write_text(content, encoding="utf-8")
            n_docs += 1
        n_projects += 1
    return {"projects": n_projects, "docs": n_docs}


def _run_mode_a_core(out_root: Path, logger: Callable[[str], None],
                     skip_map: dict[str, str] | None = None
                     ) -> tuple[dict[str, dict], dict, int, int]:
    """Общая логика Mode A для полного и инкрементального режима.

    Обходит ВСЕ организации с чатами. Возвращает (uuid→conv, stats, new, updated).
    """
    logger(_("main.mode_a_trying"))
    sk = _resolve_session_key(logger)
    if not sk:
        return {}, {"status": "skipped", "reason": _("main.mode_a_skipped_no_key")}, 0, 0

    fetcher = ApiFetcher(sk)
    logger(_("main.mode_a_checking"))
    if not fetcher.probe():
        logger(_("main.mode_a_invalid"))
        return {}, {"status": "skipped", "reason": _("main.mode_a_skipped_invalid")}, 0, 0

    try:
        orgs = fetcher.list_organizations_for_backup()
    except ApiError as e:
        logger(_("main.mode_a_fetch_error", error=str(e)))
        orgs = []
    if not orgs:
        logger(_("main.mode_a_no_org"))
        return {}, {"status": "skipped", "reason": _("main.mode_a_skipped_no_org")}, 0, 0
    logger(_("main.mode_a_orgs", n=len(orgs)))

    raw_dir = out_root / "_raw" / "mode_a"
    raw_dir.mkdir(parents=True, exist_ok=True)
    convs: dict[str, dict] = {}
    new_count = updated_count = 0
    errors: list[str] = []
    all_projects: list[dict] = []
    knowledge = {"projects": 0, "docs": 0}

    def progress(idx, total, name):
        logger(f"[Mode A] {idx+1}/{total}: {name[:50]}")

    for org in orgs:
        org_uuid = org["uuid"]
        logger(_("main.mode_a_login_ok", org=f"{org.get('name', '')} ({org_uuid[:8]})"))
        try:
            for conv in fetcher.stream_all(org_uuid, save_dir=raw_dir,
                                           progress=progress, skip_map=skip_map):
                uuid = conv.get("uuid")
                if not uuid:
                    continue
                convs[uuid] = conv
                if skip_map is not None and uuid in skip_map:
                    updated_count += 1
                else:
                    new_count += 1
        except ApiError as e:
            errors.append(f"{org_uuid[:8]}: {e}")
            logger(_("main.mode_a_fetch_error", error=str(e)))

        # Проекты: метаданные + знания
        try:
            projects = fetcher.list_projects(org_uuid)
        except ApiError:
            projects = []
        for p in projects:
            p.setdefault("_org_uuid", org_uuid)
        all_projects.extend(projects)
        if projects:
            k = _backup_project_knowledge(fetcher, org, out_root, projects, logger)
            knowledge["projects"] += k["projects"]
            knowledge["docs"] += k["docs"]

    (raw_dir / "_projects.json").write_text(
        json.dumps(all_projects, ensure_ascii=False, indent=2), encoding="utf-8")
    logger(_("main.mode_a_projects", projects=knowledge["projects"], docs=knowledge["docs"]))

    stats = {"status": "ok",
             "count": len(convs),
             "org_uuid": orgs[0]["uuid"],
             "org_uuids": [o["uuid"] for o in orgs],
             "project_knowledge": knowledge}
    if errors:
        stats["partial"] = True
        stats["errors"] = errors
    return convs, stats, new_count, updated_count


def run_mode_a(output_root: Path, logger: Callable[[str], None]) -> tuple[dict[str, dict], dict]:
    """返回 (uuid → conversation, stats)。"""
    convs, stats, _new, _upd = _run_mode_a_core(output_root, logger)
    return convs, stats


def run_mode_b(logger: Callable[[str], None]) -> tuple[dict[str, dict], dict]:
    logger(_("main.mode_b_scanning"))
    try:
        cache_dir = paths.claude_desktop_cache_dir()
    except FileNotFoundError as e:
        logger(_("main.mode_b_no_dir", error=str(e)))
        return {}, {"status": "skipped", "reason": str(e)}

    def progress(idx, total):
        if idx == 0 or idx == total or idx % 500 == 0:
            logger(_("main.mode_b_progress", idx=idx, total=total))

    convs = cache_extractor.extract_conversations(cache_dir, progress=progress)
    logger(_("main.mode_b_result", count=len(convs)))
    return convs, {"status": "ok", "cached_total": len(convs)}


def run_mode_c(logger: Callable[[str], None]) -> tuple[list[cli_exporter.SessionData], dict]:
    projects_dir = paths.claude_cli_projects_dir_optional()
    if projects_dir is None:
        logger(_("main.mode_c_no_dir"))
        return [], {"status": "skipped", "reason": _("main.mode_c_skipped_no_dir")}

    counts = cli_exporter.count_sessions()
    logger(_("main.mode_c_prepare", real=counts['real'],
             observer=counts['observer'], test=counts['skipped_test']))

    sessions: list[cli_exporter.SessionData] = []
    for idx, s in enumerate(cli_exporter.iter_sessions(skip_observer=True)):
        sessions.append(s)
        if (idx + 1) % 50 == 0:
            logger(_("main.mode_c_parsing", idx=idx + 1))
    logger(_("main.mode_c_total", count=len(sessions)))
    return sessions, {"status": "ok",
                      "real": sum(1 for s in sessions if s.category == "real")}


# ---------- 写出 ----------

def write_desktop_conversations(out_root: Path,
                                 combined: dict[str, tuple[str, dict]],
                                 logger: Callable[[str], None]) -> list[dict]:
    """combined: uuid → (source_label, conv)。source_label 例如 "在线 API（完整）"。"""
    base = out_root / "desktop-conversations"
    (base / "projects").mkdir(parents=True, exist_ok=True)
    (base / "unassigned").mkdir(exist_ok=True)

    index_rows: list[dict] = []

    for uuid, (source_label, conv) in combined.items():
        name = conv.get("name") or _("renderer.unnamed")
        date = renderer.iso_to_date(conv.get("created_at"))
        project = (conv.get("project") or {}).get("name") if conv.get("project") else None

        proj_dir = (base / "projects" / renderer.safe_name(project, 60)
                    if project else base / "unassigned")
        proj_dir.mkdir(parents=True, exist_ok=True)

        stem = f"{date}__{renderer.safe_name(name, 80)}"
        md_path = proj_dir / f"{stem}.md"
        json_path = proj_dir / f"{stem}.json"
        # 避免同名：附上 uuid 前缀
        if md_path.exists():
            stem = f"{stem}__{uuid[:8]}"
            md_path = proj_dir / f"{stem}.md"
            json_path = proj_dir / f"{stem}.json"

        md_path.write_text(renderer.render_desktop_conversation(conv, source_label), encoding="utf-8")
        json_path.write_text(json.dumps(conv, ensure_ascii=False, indent=2), encoding="utf-8")

        index_rows.append({
            "date": date, "name": name, "project": project or "-",
            "messages": len(conv.get("chat_messages", [])),
            "model": conv.get("model", ""),
            "source": source_label,
            "path": str(md_path.relative_to(base)),
        })

    # 写索引
    index_rows.sort(key=lambda r: r["date"], reverse=True)
    idx_lines = [
        f"# {_('renderer.desktop_index_title')}",
        "",
        _("renderer.desktop_index_count", count=len(index_rows)),
        "",
        f"| {_('renderer.columns_date')} | {_('renderer.columns_title')} | {_('renderer.columns_project')} | {_('renderer.columns_messages')} | {_('renderer.columns_model')} | {_('renderer.columns_source')} | {_('renderer.columns_file')} |",
        "|---|---|---|--:|---|---|---|",
    ]
    for r in index_rows:
        idx_lines.append(
            f"| {r['date']} | {r['name']} | {r['project']} | {r['messages']} | "
            f"`{r['model']}` | {r['source']} | [{_('renderer.open')}]({r['path']}) |"
        )
    (base / "00_index.md").write_text("\n".join(idx_lines), encoding="utf-8")
    logger(_("main.write_desktop", count=len(index_rows)))
    return index_rows


def write_cli_sessions(out_root: Path,
                        sessions: list[cli_exporter.SessionData],
                        logger: Callable[[str], None]) -> int:
    base = out_root / "claude-code"
    real_dir = base / "real"
    real_dir.mkdir(parents=True, exist_ok=True)

    # Строки индекса копятся между инкрементальными прогонами (fork BUTLER-SPB):
    # иначе 00_index.md содержал бы только сессии последнего прогона.
    rows_file = base / CLI_INDEX_ROWS
    try:
        all_rows: dict[str, dict] = json.loads(rows_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        all_rows = {}

    for s in sessions:
        proj_out = real_dir / renderer.safe_name(s.project, 80)
        proj_out.mkdir(parents=True, exist_ok=True)
        date = renderer.iso_to_date(s.first_ts)
        stem = f"{date}__{renderer.safe_name(s.title, 80)}__{s.session_id[:8]}"
        md_path = proj_out / f"{stem}.md"
        jsonl_path = proj_out / f"{stem}.jsonl"
        md_path.write_text(renderer.render_cli_session(s), encoding="utf-8")
        renderer.hardlink_or_copy(s.source_path, jsonl_path)

        all_rows[s.session_id] = {
            "date": date, "project": s.project, "title": s.title,
            "user_turns": s.user_turns, "asst_turns": s.assistant_turns,
            "events": s.total_events, "model": s.model,
            "path": str(md_path.relative_to(base)),
        }

    rows_file.write_text(json.dumps(all_rows, ensure_ascii=False), encoding="utf-8")
    rows = sorted(all_rows.values(), key=lambda r: (r["project"], r["date"]), reverse=True)
    lines = [
        f"# {_('renderer.cli_index_title')}",
        "",
        _("renderer.cli_index_count", count=len(rows)),
        _("renderer.cli_index_desc"),
        "",
        f"| {_('renderer.columns_date')} | {_('renderer.columns_project')} | {_('renderer.columns_title')} | User/Asst Turns | Events | {_('renderer.columns_model')} | {_('renderer.columns_file')} |",
        "|---|---|---|--:|--:|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['date']} | {r['project']} | {r['title'][:60]} | "
            f"{r['user_turns']}/{r['asst_turns']} | {r['events']} | "
            f"`{r['model']}` | [{_('renderer.open')}]({r['path']}) |"
        )
    (base / "00_index.md").write_text("\n".join(lines), encoding="utf-8")
    logger(_("main.write_cli", count=len(sessions)))
    return len(sessions)


# ---------- 主入口 ----------

def run(output_dir: Path, modes: str, logger: Callable[[str], None]) -> dict:
    """主流程。返回 stats dict。"""
    log.info("run() 开始: output=%s, modes=%s", output_dir, modes)
    output_dir.mkdir(parents=True, exist_ok=True)
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stats: dict = {
        "version": __version__,
        "run_time": when,
        "source_platform": paths.detect_platform(),
        "output_dir": str(output_dir),
    }

    a_convs: dict[str, dict] = {}
    b_convs: dict[str, dict] = {}
    c_sessions: list[cli_exporter.SessionData] = []

    if "a" in modes:
        a_convs, stats["mode_a"] = run_mode_a(output_dir, logger)
    else:
        stats["mode_a"] = {"status": "skipped", "reason": _("main.mode_a_skipped_user")}

    if "b" in modes:
        b_convs, stats["mode_b"] = run_mode_b(logger)
    else:
        stats["mode_b"] = {"status": "skipped", "reason": _("main.mode_b_skipped_user")}

    if "c" in modes:
        c_sessions, stats["mode_c"] = run_mode_c(logger)
    else:
        stats["mode_c"] = {"status": "skipped", "reason": _("main.mode_c_skipped_user")}

    # 合并 A + B：A 优先
    combined: dict[str, tuple[str, dict]] = {}
    for uuid, conv in a_convs.items():
        combined[uuid] = (_("main.combined_source_api"), conv)
    new_from_b = 0
    for uuid, conv in b_convs.items():
        if uuid not in combined:
            combined[uuid] = (_("main.combined_source_cache"), conv)
            new_from_b += 1
    stats["mode_b"]["new_unique_to_b"] = new_from_b

    # 写出
    if combined:
        write_desktop_conversations(output_dir, combined, logger)
    if c_sessions:
        write_cli_sessions(output_dir, c_sessions, logger)

    # 提取文件附件
    sk = cookies.get_session_key()
    file_map = extract_all_files(output_dir, session_key=sk, logger=logger)

    # 顶层索引 + STATS
    (output_dir / "INDEX.md").write_text(
        renderer.render_top_index(stats, when), encoding="utf-8"
    )
    (output_dir / "STATS.md").write_text(
        renderer.render_stats_report(stats, when), encoding="utf-8"
    )
    generate_html_viewer(output_dir, logger, file_map=file_map)
    logger(_("main.complete_output", dir=str(output_dir)))
    return stats


# ---------- 增量备份 ----------

def _incremental_mode_a(backup_dir: Path, manifest: dict,
                         logger: Callable[[str], None]) -> tuple[dict[str, dict], dict]:
    """增量 Mode A：只抓新的或更新的对话。返回 (uuid→conv, stats)。"""
    skip_map: dict[str, str] = {}
    for uuid, meta in manifest["conversations"].items():
        if meta.get("source") in ("online_api", "cache+api"):
            skip_map[uuid] = meta.get("updated_at", "")

    convs, stats, new_count, updated_count = _run_mode_a_core(backup_dir, logger, skip_map)
    if stats.get("status") == "ok":
        logger(_("main.mode_a_new_count", new=new_count, updated=updated_count))
        stats["new"] = new_count
        stats["updated"] = updated_count
    return convs, stats


def _incremental_mode_b(manifest: dict,
                         logger: Callable[[str], None]) -> tuple[dict[str, dict], dict]:
    """增量 Mode B：扫描缓存，只返回 manifest 中没有的对话。"""
    logger(_("main.mode_b_scanning"))
    try:
        cache_dir = paths.claude_desktop_cache_dir()
    except FileNotFoundError as e:
        logger(_("main.mode_b_no_dir", error=str(e)))
        return {}, {"status": "skipped", "reason": str(e)}

    def progress(idx, total):
        if idx == 0 or idx == total or idx % 500 == 0:
            logger(_("main.mode_b_progress", idx=idx, total=total))

    all_convs = cache_extractor.extract_conversations(cache_dir, progress=progress)

    # 只保留 manifest 中没有的（或者 source 是 cache 的可以被 api 覆盖）
    new_convs: dict[str, dict] = {}
    for uuid, conv in all_convs.items():
        existing = manifest["conversations"].get(uuid)
        if not existing:
            new_convs[uuid] = conv
        elif existing.get("source") == "cache" and existing.get("updated_at") != conv.get("updated_at", ""):
            new_convs[uuid] = conv

    logger(_("main.mode_b_new", total=len(all_convs), new=len(new_convs)))
    return new_convs, {"status": "ok", "cached_total": len(all_convs),
                        "new": len(new_convs)}


def _incremental_mode_c(backup_dir: Path, manifest: dict,
                         logger: Callable[[str], None]) -> tuple[list[cli_exporter.SessionData], dict]:
    """增量 Mode C：扫描会话，只返回 manifest 中没有的。"""
    projects_dir = paths.claude_cli_projects_dir_optional()
    if projects_dir is None:
        logger(_("main.mode_c_no_dir"))
        log.info(_("main.mode_c_no_dir"))
        return [], {"status": "skipped", "reason": _("main.mode_c_skipped_no_dir")}

    counts = cli_exporter.count_sessions()
    msg = _("main.mode_c_prepare", real=counts['real'],
            observer=counts['observer'], test=counts['skipped_test'])
    logger(msg)
    log.info(msg)

    # Нет накопленного индекса (первый прогон новой версии) — переписать всё один раз,
    # чтобы 00_index.md стал полным.
    rebuild = not (backup_dir / "claude-code" / CLI_INDEX_ROWS).is_file()
    new_sessions: list[cli_exporter.SessionData] = []
    for s in cli_exporter.iter_sessions(skip_observer=True):
        if not rebuild and not mf.needs_session_update(
                manifest, s.session_id, s.last_ts or "", backup_dir=backup_dir):
            continue
        new_sessions.append(s)

    msg = _("main.mode_c_new", count=len(new_sessions))
    logger(msg)
    log.info(msg)
    return new_sessions, {"status": "ok", "new": len(new_sessions),
                           "real": sum(1 for s in new_sessions if s.category == "real")}


class BackupBusy(RuntimeError):
    """В этой папке уже идёт другой бэкап (окно, расписание или консоль)."""


@contextmanager
def _backup_lock(backup_dir: Path):
    """Межпроцессная блокировка папки бэкапа. Снимается ОС при завершении процесса."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    f = open(backup_dir / ".backup.lock", "a+b")
    try:
        try:
            if sys.platform == "win32":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise BackupBusy("Бэкап в эту папку уже выполняется другим процессом") from e
        yield
    finally:
        f.close()


def run_incremental(backup_dir: Path, modes: str,
                    logger: Callable[[str], None]) -> dict:
    with _backup_lock(backup_dir):
        return _run_incremental(backup_dir, modes, logger)


def _run_incremental(backup_dir: Path, modes: str,
                     logger: Callable[[str], None]) -> dict:
    """增量备份主流程。

    1. 加载 manifest
    2. 按 modes 执行各模式，只抓新的/更新的
    3. 写出到 backup_dir（覆盖更新的文件，新增新文件）
    4. 更新 manifest
    """
    log.info("run_incremental() 开始: dir=%s, modes=%s", backup_dir, modes)
    started = datetime.now()
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest = mf.load_manifest(backup_dir)
    when = started.strftime("%Y-%m-%d %H:%M:%S")
    stats: dict = {
        "version": __version__,
        "run_time": when,
        "mode": "incremental",
        "source_platform": paths.detect_platform(),
        "backup_dir": str(backup_dir),
    }

    a_convs: dict[str, dict] = {}
    b_convs: dict[str, dict] = {}
    c_sessions: list[cli_exporter.SessionData] = []

    if "a" in modes:
        a_convs, stats["mode_a"] = _incremental_mode_a(backup_dir, manifest, logger)
    else:
        stats["mode_a"] = {"status": "skipped", "reason": _("main.mode_a_skipped_user")}

    if "b" in modes:
        b_convs, stats["mode_b"] = _incremental_mode_b(manifest, logger)
    else:
        stats["mode_b"] = {"status": "skipped", "reason": _("main.mode_b_skipped_user")}

    if "c" in modes:
        c_sessions, stats["mode_c"] = _incremental_mode_c(backup_dir, manifest, logger)
    else:
        stats["mode_c"] = {"status": "skipped", "reason": _("main.mode_c_skipped_user")}

    # 合并 A + B：A 优先
    api_label = _("main.combined_source_api")
    cache_label = _("main.combined_source_cache")
    combined: dict[str, tuple[str, dict]] = {}
    for uuid, conv in a_convs.items():
        combined[uuid] = (api_label, conv)
    new_from_b = 0
    for uuid, conv in b_convs.items():
        if uuid not in combined:
            combined[uuid] = (cache_label, conv)
            new_from_b += 1
    stats["mode_b"]["new_unique_to_b"] = new_from_b

    # 写出
    if combined:
        write_desktop_conversations(backup_dir, combined, logger)
        # 注册到 manifest
        for uuid, (source_label, conv) in combined.items():
            mf.register_conversation(manifest, uuid, {
                "title": conv.get("name", ""),
                "updated_at": conv.get("updated_at", ""),
                "message_count": len(conv.get("chat_messages", [])),
                "model": conv.get("model", ""),
                "project": (conv.get("project") or {}).get("name") if conv.get("project") else None,
                "source": "online_api" if source_label == api_label else "cache",
                "file": f"desktop-conversations/{uuid}",
            })

    if c_sessions:
        write_cli_sessions(backup_dir, c_sessions, logger)
        for s in c_sessions:
            mf.register_cli_session(manifest, s.session_id, {
                "title": s.title,
                "project": s.project,
                "category": s.category,
                "first_ts": s.first_ts or "",
                "last_ts": s.last_ts or "",
                "source": "cli_log",
                # тот же safe_name, что и в write_cli_sessions
                "file": f"claude-code/{s.category}/{renderer.safe_name(s.project, 80)}",
            })

    # 保存 manifest
    mf.save_manifest(backup_dir, manifest)

    # 提取文件附件
    sk = cookies.get_session_key()
    file_map = extract_all_files(backup_dir, session_key=sk, logger=logger)

    # 顶层索引
    (backup_dir / "INDEX.md").write_text(
        renderer.render_top_index(stats, when), encoding="utf-8"
    )
    (backup_dir / "STATS.md").write_text(
        renderer.render_stats_report(stats, when), encoding="utf-8"
    )

    # 更新配置中的 last_run
    s = mf.summary(manifest)
    cfg.update_last_run({
        "mode_a": stats["mode_a"].get("count", 0),
        "mode_b": stats["mode_b"].get("cached_total", 0),
        "mode_c": stats["mode_c"].get("real", 0),
        "conversation_total": s["conversation_count"],
        "session_total": s["session_count"],
    })

    generate_html_viewer(backup_dir, logger, file_map=file_map)
    logger(_("main.complete_incremental", dir=str(backup_dir)))
    history.append(backup_dir, history.make_record(
        stats, _determine_exit_code(stats, modes), started))
    return stats


def _handle_schedule(action: str, args):
    """处理 --schedule 子命令。"""
    from .scheduler import (
        ScheduleConfig,
        schedule_config_path,
        get_next_run_time,
    )
    from .autobackup_daemon import is_daemon_running, read_status

    if action == "install":
        config = ScheduleConfig.load(schedule_config_path())
        if not config.enabled:
            print(_("schedule.skipped_disabled"))
            print(_("cli.schedule_hint_enable"))
            return

        if paths.detect_platform() == "mac":
            from .scheduler_mac import install as plat_install
            ok = plat_install(config, config.time_trigger.interval_minutes)
        elif paths.detect_platform() == "win":
            from .scheduler_win import install as plat_install
            ok = plat_install(config, config.time_trigger.interval_minutes)
        else:
            print(_("cli.schedule_unsupported_platform"))
            return

        if ok:
            print(_("schedule.installed", path=str(schedule_config_path())))
        else:
            print(_("schedule.install_failed", error="see log"))

    elif action == "uninstall":
        if paths.detect_platform() == "mac":
            from .scheduler_mac import uninstall as plat_uninstall
            plat_uninstall()
        elif paths.detect_platform() == "win":
            from .scheduler_win import uninstall as plat_uninstall
            plat_uninstall()
        print(_("schedule.uninstalled"))

    elif action == "status":
        st: dict = {"platform": paths.detect_platform()}
        if paths.detect_platform() == "mac":
            from .scheduler_mac import status as plat_status
            st = plat_status()
        elif paths.detect_platform() == "win":
            from .scheduler_win import status as plat_status
            st = plat_status()

        config = ScheduleConfig.load(schedule_config_path())
        next_run = get_next_run_time(config)
        from datetime import datetime
        print(f"  平台: {st.get('platform', '?')}")
        print(f"  启用: {config.enabled}")
        print(f"  daemon 已安装: {st.get('daemon_installed', False)}")
        print(f"  daemon 运行中: {st.get('daemon_running', False)}")
        print(f"  定时触发: {config.time_trigger.type} / {config.time_trigger.interval_minutes}min")
        print(f"  Claude 关闭触发: {config.condition_triggers.on_claude_close}")
        print(f"  Claude 启动触发: {config.condition_triggers.on_claude_start}")
        print(f"  最小间隔: {config.min_interval_minutes}m")
        if next_run:
            print(_("schedule.next_run",
                     time=datetime.fromtimestamp(next_run).strftime("%Y-%m-%d %H:%M")))
        st_file = read_status()
        if st_file:
            print(f"\n  [daemon 状态]")
            print(f"  上次检查: {st_file.get('last_check', 'N/A')}")
            print(f"  上次备份: {st_file.get('last_backup', 'N/A')}")
            print(f"  Claude 运行中: {st_file.get('claude_running', 'N/A')}")

    elif action == "run":
        # 兼容旧版：直接跑 daemon
        from .autobackup_daemon import run_daemon
        run_daemon()


# ── Agent 子命令 ──────────────────────────────────────────

def cmd_capabilities(json_mode: bool) -> None:
    """返回工具能力描述。纯静态，无 I/O。"""
    data = {
        "version": __version__,
        "tool": "claude-data-backup",
        "modes": {
            "a": {
                "name": "Online API",
                "description": _("agent.mode_a_desc"),
                "requires": ["sessionKey"],
                "incremental": True,
                "output": "desktop-conversations/",
            },
            "b": {
                "name": "Cache Mining",
                "description": _("agent.mode_b_desc"),
                "requires": ["claude_desktop_installed"],
                "incremental": True,
                "output": "desktop-conversations/",
            },
            "c": {
                "name": "Claude Code CLI",
                "description": _("agent.mode_c_desc"),
                "requires": ["~/.claude/projects/"],
                "incremental": True,
                "output": "claude-code/",
            },
        },
        "subcommands": sorted(_SUBCOMMANDS),
        "flags": [
            "--output", "--incremental", "--mode", "--verbose",
            "--set-backup-dir", "--schedule", "--json", "--version",
        ],
        "exit_codes": {
            "0": _("exit.success"),
            "1": _("exit.error"),
            "2": _("exit.invalid_args"),
            "3": _("exit.auth_failure"),
            "4": _("exit.partial"),
        },
        "incremental_hint": _("agent.incremental_hint"),
        "config_path": "~/.claude-data-backup/config.json",
        "manifest_path": "<backup_dir>/manifest.json",
        "agents_doc": "AGENTS.md",
    }
    if json_mode:
        _emit_json(data)
    else:
        print(_("agent.capabilities_title"))
        print(f"  v{data['version']}")
        print()
        for key, m in data["modes"].items():
            print(f"  Mode {key}: {m['name']} — {m['description']}")
        print()
        print(_("agent.incremental_hint"))
        print()
        print(f"  {_('cli.subcommand_list')}: claude-data-backup list")
        print(f"  {_('cli.subcommand_status')}: claude-data-backup status")
        print(f"  {_('cli.subcommand_capabilities')}: claude-data-backup capabilities")
        print()
        print("  --json: " + _("cli.json_help"))


def cmd_list(json_mode: bool) -> None:
    """轻量本地发现，不调用网络 API。"""
    pr = paths.report()
    platform = pr.get("platform", "unknown")

    # Mode A: cookie 状态
    cookie_info = cookies.describe_cookie_state()
    mode_a_available = cookie_info.get("has_session_key", False)

    # Mode B: 缓存目录
    cache_dir_exists = pr.get("cache_dir_exists", False)

    # Mode C: Claude Code 会话
    cc_sessions = cli_exporter.count_sessions()
    projects_dir_exists = pr.get("cli_projects_dir_exists", False)

    # 配置
    backup_dir = cfg.get_backup_dir()
    sources = cfg.get_sources()

    data = {
        "platform": platform,
        "sources": {
            "mode_a": {
                "available": mode_a_available,
                "cookie_status": {
                    "cookies_readable": cookie_info.get("cookies_readable", False),
                    "cookies_count": cookie_info.get("cookies_count", 0),
                    "has_session_key": cookie_info.get("has_session_key", False),
                    "hosts_seen": cookie_info.get("hosts_seen", []),
                    "env_override": cookie_info.get("env_override", False),
                    "cookies_db_version": cookie_info.get("cookies_db_version", 0),
                    "session_key_formats": cookie_info.get("session_key_formats", []),
                    "error": cookie_info.get("error", ""),
                },
            },
            "mode_b": {
                "available": cache_dir_exists,
                "cache_dir": pr.get("cache_dir", ""),
                "cache_dir_exists": cache_dir_exists,
            },
            "mode_c": {
                "available": projects_dir_exists,
                "projects_dir": pr.get("cli_projects_dir", ""),
                "projects_dir_exists": projects_dir_exists,
                "sessions": cc_sessions,
            },
        },
        "config": {
            "backup_dir": str(backup_dir),
            "sources": sources,
        },
    }

    if json_mode:
        _emit_json(data)
    else:
        print(_("agent.list_title"))
        print(f"  {_('agent.platform')}: {platform}")
        print()
        # Mode A
        if mode_a_available:
            print(f"  {_('agent.mode_a_cookie_ok')}")
        else:
            print(f"  {_('agent.mode_a_cookie_none')}")
        # Mode B
        if cache_dir_exists:
            print(f"  {_('agent.mode_b_cache_ok')}")
        else:
            print(f"  {_('agent.mode_b_cache_none')}")
        # Mode C
        if projects_dir_exists:
            print(f"  {_('agent.mode_c_sessions', real=cc_sessions['real'], observer=cc_sessions['observer'])}")
        else:
            print(f"  {_('agent.mode_c_none')}")
        print()
        print(f"  {_('agent.backup_dir')}: {backup_dir}")


def cmd_status(json_mode: bool) -> None:
    """读取已有的备份状态。"""
    backup_dir = cfg.get_backup_dir()
    has_manifest = (backup_dir / "manifest.json").is_file()

    manifest_data = mf.load_manifest(backup_dir)
    summary = mf.summary(manifest_data)
    config = cfg.load_config()

    last_run = config.get("last_run_stats") or {}
    data = {
        "backup_dir": str(backup_dir),
        "manifest": {
            "version": manifest_data.get("version", 0),
            "last_backup_time": manifest_data.get("last_backup_time", ""),
            "conversation_count": summary.get("conversation_count", 0),
            "session_count": summary.get("session_count", 0),
            "real_session_count": summary.get("real_session_count", 0),
        },
        "last_run": {
            "time": config.get("last_run_time", ""),
            "mode_a_count": last_run.get("mode_a", 0),
            "mode_b_count": last_run.get("mode_b", 0),
            "mode_c_real": last_run.get("mode_c_real", 0),
        },
        "has_manifest": has_manifest,
    }

    if json_mode:
        _emit_json(data)
    else:
        print(_("agent.status_title"))
        print(f"  {_('agent.backup_dir')}: {backup_dir}")
        print()
        if has_manifest:
            print(f"  {_('agent.manifest_found', conv=summary['conversation_count'], sess=summary['session_count'])}")
            last_time = manifest_data.get("last_backup_time", "")
            if last_time:
                print(f"  {_('agent.last_backup', time=last_time)}")
        else:
            print(f"  {_('agent.manifest_none')}")


def main():
    init_language(cfg.get_backup_dir())
    setup_logging()
    log.info("=== ClaudeDataBackup CLI v%s 启动 ===", __version__)

    # Windows cmd 默认 GBK 编码，中文输出会乱码
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
        log.info("stdout 编码 %s，尝试切换 UTF-8", sys.stdout.encoding)
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception as e:
            log.warning("stdout 编码切换失败: %s", e)

    # ── 子命令 pre-scan 路由 ──
    if len(sys.argv) > 1 and sys.argv[1] in _SUBCOMMANDS:
        json_mode = "--json" in sys.argv
        cmd = sys.argv[1]
        try:
            if cmd == "capabilities":
                cmd_capabilities(json_mode)
            elif cmd == "list":
                cmd_list(json_mode)
            elif cmd == "status":
                cmd_status(json_mode)
        except Exception:
            log.critical("子命令 '%s' 执行失败", cmd, exc_info=True)
            if json_mode:
                _emit_json({"error": traceback.format_exc()}, file=sys.stderr)
            else:
                traceback.print_exc()
            sys.exit(EXIT_ERROR)
        sys.exit(EXIT_SUCCESS)

    # ── 原有 argparse 流程 ──
    ap = argparse.ArgumentParser(
        description=_("cli.description")
    )
    ap.add_argument("--output", type=Path, default=None,
                    help=_("cli.output_help"))
    ap.add_argument("--incremental", "-i", action="store_true",
                    help=_("cli.incremental_help"))
    ap.add_argument("--set-backup-dir", type=Path, default=None,
                    help=_("cli.set_dir_help"))
    ap.add_argument("--mode", default="abc",
                    help=_("cli.mode_help"))
    ap.add_argument("--verbose", "-v", action="store_true", help=_("cli.verbose_help"))
    ap.add_argument("--json", action="store_true", default=False,
                    help=_("cli.json_help"))
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--schedule", choices=["install", "uninstall", "status", "run"],
                    default=None, help=_("cli.schedule_help"))
    args = ap.parse_args()
    log.info("参数: output=%s, incremental=%s, mode=%s, set_backup_dir=%s, schedule=%s, json=%s",
             args.output, args.incremental, args.mode, args.set_backup_dir, args.schedule, args.json)

    # ── schedule 子命令 ──
    if args.schedule:
        _handle_schedule(args.schedule, args)
        return

    # 设置备份目录
    if args.set_backup_dir:
        cfg.set_backup_dir(args.set_backup_dir)
        print(_("cli.dir_set", dir=str(args.set_backup_dir)))
        if not args.incremental and args.output is None:
            return  # 只设置目录，不做备份

    modes = args.mode.lower()
    if not any(c in modes for c in "abc"):
        log.error("--mode 至少要包含 a/b/c 中的一个: %s", args.mode)
        print(_("cli.mode_error"), file=sys.stderr)
        sys.exit(EXIT_INVALID_ARGS)

    # ── JSON 模式：logger 收集到 list ──
    if args.json:
        logger, json_messages = _make_json_logger()
    else:
        json_messages = None

        def logger(msg):
            print(msg, flush=True)

    run_started = datetime.now()
    try:
        if args.incremental:
            backup_dir = cfg.get_backup_dir()
            log.info("增量备份目录: %s", backup_dir)
            stats = run_incremental(backup_dir, modes, logger)
            save_language_preference(backup_dir)
        else:
            out = args.output or _default_output_dir()
            log.info("一次性导出目录: %s", out)
            stats = run(out, modes, logger)
            save_language_preference(out)
    except BackupBusy as e:
        log.warning("%s", e)
        if args.json:
            _emit_json({"error": str(e)}, file=sys.stderr)
        else:
            print(str(e), file=sys.stderr)
        sys.exit(EXIT_ERROR)
    except Exception:
        log.critical("CLI 执行失败", exc_info=True)
        if args.incremental:
            history.append(cfg.get_backup_dir(), history.make_record(
                None, EXIT_ERROR, run_started, error=traceback.format_exc()))
        if args.json:
            _emit_json({"error": traceback.format_exc()}, file=sys.stderr)
        else:
            traceback.print_exc()
        sys.exit(EXIT_ERROR)

    # ── JSON 输出 ──
    if args.json:
        exit_code = _determine_exit_code(stats, modes)
        _emit_json({
            "status": "ok",
            "exit_code": exit_code,
            "stats": stats,
            "log": json_messages,
        })
        sys.exit(exit_code)

    # ── 人类可读汇总 ──
    log.info("CLI 执行完成: %s", stats)
    print("\n" + _("cli.summary_header"))
    if args.incremental:
        print(_("cli.mode_incremental"))
        print(_("cli.dir_label", dir=str(cfg.get_backup_dir())))
    else:
        print(_("cli.mode_full"))
    print(f"Mode A: {stats['mode_a'].get('status')}"
          f" ({stats['mode_a'].get('count', 0)})")
    print(f"Mode B: {stats['mode_b'].get('status')}"
          f" ({stats['mode_b'].get('cached_total', 0)})")
    print(f"Mode C: {stats['mode_c'].get('status')}"
          f" (real={stats['mode_c'].get('real', 0)})")
    if not args.incremental:
        print(_("cli.output_label", dir=str(args.output or _default_output_dir())))
    # Код возврата и без --json: по нему Планировщик показывает результат ночного запуска
    exit_code = _determine_exit_code(stats, modes)
    if exit_code:
        sys.exit(exit_code)


def _determine_exit_code(stats: dict, modes: str) -> int:
    """根据备份结果 stats 和选择的 modes 确定退出码。"""
    mode_a_auth_fail = False
    any_mode_ok = False

    if "a" in modes:
        a_status = stats.get("mode_a", {}).get("status", "")
        a_reason = stats.get("mode_a", {}).get("reason", "")
        # 检查 reason 是否是 i18n key 或已翻译的认证相关字符串
        reason_str = str(a_reason)
        is_auth_issue = ("no_key" in reason_str or "sessionKey" in reason_str
                         or "sessionkey" in reason_str.lower()
                         or "invalid" in reason_str)
        if a_status == "skipped" and is_auth_issue:
            mode_a_auth_fail = True
        elif a_status == "ok":
            any_mode_ok = True
    if "b" in modes:
        if stats.get("mode_b", {}).get("status") == "ok":
            any_mode_ok = True
    if "c" in modes:
        if stats.get("mode_c", {}).get("status") == "ok":
            any_mode_ok = True

    # 只选了 mode_a 且认证失败
    if mode_a_auth_fail and not any_mode_ok:
        return EXIT_AUTH_FAILURE

    # mode_a 认证失败但其他模式成功
    if mode_a_auth_fail and any_mode_ok:
        return EXIT_PARTIAL

    # Mode A прошёл, но часть организаций упала
    if "a" in modes and stats.get("mode_a", {}).get("partial"):
        return EXIT_PARTIAL

    return EXIT_SUCCESS


if __name__ == "__main__":
    main()

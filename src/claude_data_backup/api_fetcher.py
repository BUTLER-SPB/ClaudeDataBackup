"""Mode A —— 在线 API 全量抓取。

用 sessionKey 直接调 claude.ai 的私有 API。端点和响应结构见 docs/data-formats.md。

Fork changes (BUTLER-SPB):
- list_conversations: пагинация limit/offset с дедупликацией (безопасно, даже если
  сервер игнорирует параметры и всегда отдаёт полный список).
- stream_all: копия из save_dir переиспользуется только если её updated_at совпадает
  с удалённым — иначе обновлённые чаты навсегда «замерзали» в инкрементальном режиме.
- list_organizations_for_backup: все организации с capability "chat", а не только orgs[0].
- get_project / list_project_docs: бэкап знаний проектов (инструкции + документы).
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Callable, Iterator

import requests

from . import paths
from .i18n import t as _
from .log import get_logger

log = get_logger(__name__)

API_BASE = "https://claude.ai/api"
CONVERSATIONS_PAGE_SIZE = 200
CONVERSATIONS_MAX_PAGES = 500  # предохранитель от бесконечного цикла (100k чатов)


class ApiError(Exception):
    pass


class ApiFetcher:
    """单线程 claude.ai API 客户端，中断可恢复。"""

    def __init__(self, session_key: str, sleep_between: float = 0.5):
        self.session_key = session_key
        self.sleep_between = sleep_between
        self.sess = requests.Session()
        # 自动检测并应用系统代理
        proxy = paths.detect_system_proxy()
        if proxy:
            self.sess.proxies.update(proxy)
            log.info("Mode A: используется системный прокси %s", proxy)
        self.sess.headers.update({
            "User-Agent": paths.get_user_agent(),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": f"sessionKey={session_key}",
            "Referer": "https://claude.ai/chats",
        })

    # ---------- 内部 ----------

    def _get(self, path: str, retries: int = 3) -> dict | list:
        url = f"{API_BASE}{path}"
        backoff = 2.0
        for attempt in range(retries):
            try:
                resp = self.sess.get(url, timeout=30)
                if resp.status_code == 429:
                    time.sleep(min(backoff, 30))
                    backoff *= 2
                    continue
                if resp.status_code == 401 or resp.status_code == 403:
                    raise ApiError(_("api.auth_fail", code=resp.status_code))
                if resp.status_code >= 500:
                    time.sleep(min(backoff, 30))
                    backoff *= 2
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                if attempt == retries - 1:
                    raise ApiError(_("api.request_fail", url=url, error=str(e))) from e
                time.sleep(min(backoff, 30))
                backoff *= 2
        raise ApiError(_("api.max_retries", url=url))

    # ---------- 公共 API ----------

    def probe(self) -> bool:
        """快速判断 sessionKey 是否有效。"""
        try:
            r = self._get("/organizations", retries=1)
            return isinstance(r, list) and len(r) > 0
        except ApiError:
            return False

    def list_organizations(self) -> list[dict]:
        r = self._get("/organizations")
        return r if isinstance(r, list) else []

    def list_organizations_for_backup(self) -> list[dict]:
        """Все организации, в которых есть чаты.

        Организации без capability "chat" (например, чисто API/Console) пропускаются.
        Если поле capabilities отсутствует — организация берётся (лучше лишний запрос,
        чем потерянные чаты).
        """
        result = []
        for org in self.list_organizations():
            if not org.get("uuid"):
                continue
            caps = org.get("capabilities")
            if isinstance(caps, list) and caps and "chat" not in caps:
                log.info("Пропуск организации %s (%s): нет capability chat",
                         org.get("uuid", "")[:8], org.get("name", ""))
                continue
            result.append(org)
        return result

    def list_conversations(self, org_uuid: str) -> list[dict]:
        """只有 metadata（uuid / name / created_at / updated_at / project_uuid / ...），不含消息内容。

        Пагинация limit/offset. Если сервер параметры игнорирует и отдаёт весь список,
        вторая страница не принесёт новых uuid — цикл остановится.
        """
        seen: set[str] = set()
        out: list[dict] = []
        offset = 0
        base = f"/organizations/{org_uuid}/chat_conversations"
        for page_no in range(CONVERSATIONS_MAX_PAGES):
            try:
                r = self._get(f"{base}?limit={CONVERSATIONS_PAGE_SIZE}&offset={offset}")
            except ApiError:
                if page_no != 0:
                    raise
                # Сервер не принял limit/offset — старое поведение: весь список разом
                log.info("Пагинация не поддержана, запрашиваю полный список")
                r = self._get(base)
                page = r if isinstance(r, list) else []
                return [i for i in page if isinstance(i, dict) and i.get("uuid")]
            page = r if isinstance(r, list) else []
            new_items = 0
            for item in page:
                uuid = item.get("uuid") if isinstance(item, dict) else None
                if not uuid or uuid in seen:
                    continue
                seen.add(uuid)
                out.append(item)
                new_items += 1
            if len(page) < CONVERSATIONS_PAGE_SIZE or new_items == 0:
                break
            offset += len(page)
        log.info("Org %s: %d чатов в списке", org_uuid[:8], len(out))
        return out

    def list_projects(self, org_uuid: str) -> list[dict]:
        # 先尝试 projects_v2；不支持的账号用 projects
        try:
            r = self._get(f"/organizations/{org_uuid}/projects_v2")
            if isinstance(r, list):
                return r
            if isinstance(r, dict) and "projects" in r:
                return r["projects"]
        except ApiError:
            pass
        try:
            r = self._get(f"/organizations/{org_uuid}/projects")
            return r if isinstance(r, list) else []
        except ApiError:
            return []

    def get_project(self, org_uuid: str, project_uuid: str) -> dict:
        """Полные метаданные проекта (включая prompt_template — инструкции проекта)."""
        r = self._get(f"/organizations/{org_uuid}/projects/{project_uuid}", retries=2)
        return r if isinstance(r, dict) else {}

    def list_project_docs(self, org_uuid: str, project_uuid: str) -> list[dict]:
        """Документы знаний проекта (file_name + content)."""
        r = self._get(f"/organizations/{org_uuid}/projects/{project_uuid}/docs", retries=2)
        if isinstance(r, list):
            return r
        if isinstance(r, dict) and isinstance(r.get("docs"), list):
            return r["docs"]
        return []

    def fetch_conversation(self, org_uuid: str, conv_uuid: str) -> dict:
        """完整对话（含消息树）。"""
        path = (
            f"/organizations/{org_uuid}/chat_conversations/{conv_uuid}"
            "?tree=True&rendering_mode=messages&render_all_tools=true&consistency=eventual"
        )
        r = self._get(path)
        if not isinstance(r, dict):
            raise ApiError(_("api.fetch_error", type=type(r)))
        return r

    # ---------- 流式抓取全量 ----------

    def stream_all(self, org_uuid: str,
                   save_dir: Path | None = None,
                   progress: Callable[[int, int, str], None] | None = None,
                   skip_map: dict[str, str] | None = None) -> Iterator[dict]:
        """一次性抓全量对话。支持中断恢复和增量跳过。

        参数：
            save_dir: 中断恢复目录。已存在的 uuid 只有在磁盘副本的 updated_at
                      与远端一致时才从磁盘读取，否则重新抓取。
            skip_map: 增量备份用，{uuid: updated_at}。UUID 在 map 中且 updated_at
                      未变的对话会被跳过（不 yield 也不发 API 请求）。

        yield 每一条完整 conversation dict。
        """
        convs_meta = self.list_conversations(org_uuid)
        total = len(convs_meta)

        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)

        for idx, meta in enumerate(convs_meta):
            uuid = meta.get("uuid")
            if not uuid:
                continue
            remote_updated = meta.get("updated_at", "")
            if progress:
                progress(idx, total, meta.get("name") or _("renderer.unnamed"))

            # 增量跳过：已备份且未更新的对话直接跳过
            if skip_map and uuid in skip_map and skip_map[uuid] == remote_updated:
                continue

            # Resume: берём копию с диска только если она актуальна
            if save_dir:
                cached = self._load_fresh_copy(save_dir / f"{uuid}.json", remote_updated)
                if cached is not None:
                    yield cached
                    continue

            try:
                conv = self.fetch_conversation(org_uuid, uuid)
                if save_dir:
                    tmp = save_dir / f"{uuid}.json.tmp"
                    tmp.write_text(json.dumps(conv, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
                    tmp.replace(save_dir / f"{uuid}.json")
                yield conv
            except ApiError as e:
                log.warning("Mode A: не удалось скачать %s: %s", uuid, e)
                if save_dir:
                    failed_path = save_dir / "_failed.jsonl"
                    with open(failed_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"uuid": uuid, "org": org_uuid, "error": str(e)},
                                           ensure_ascii=False) + "\n")
                continue
            time.sleep(self.sleep_between)

        if progress:
            progress(total, total, "done")

    @staticmethod
    def _load_fresh_copy(path: Path, remote_updated: str) -> dict | None:
        """Вернуть сохранённый JSON, если его updated_at == remote_updated."""
        if not path.is_file():
            return None
        try:
            conv = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(conv, dict):
            return None
        if remote_updated and conv.get("updated_at", "") != remote_updated:
            return None
        return conv


if __name__ == "__main__":
    from .cookies import get_session_key
    sk = get_session_key()
    if not sk:
        print(_("api.no_key"))
        raise SystemExit(1)
    f = ApiFetcher(sk)
    if not f.probe():
        print(_("api.invalid_key"))
        raise SystemExit(1)
    orgs = f.list_organizations_for_backup()
    print(f"organizations: {len(orgs)}")
    for o in orgs:
        convs = f.list_conversations(o["uuid"])
        print(f"  {o.get('name', '')} ({o['uuid'][:8]}): {len(convs)} conversations")

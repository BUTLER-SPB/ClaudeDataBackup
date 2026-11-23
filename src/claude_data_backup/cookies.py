"""Cookie 解密 —— 从 Claude Desktop Cookies SQLite 库里拿到 sessionKey。

Mac 和 Win 走不同路径取 AES key（Keychain vs DPAPI + Local State）。
两个平台都用 Chromium 的标准加密格式解密 cookie value（v10/v11）。

Fork changes (xmaxzzz):
- Переопределение через переменную окружения CLAUDE_SESSION_KEY (fallback, если
  Desktop не установлен / расшифровка не работает / нужен другой аккаунт).
- 32-байтовый префикс — это не App-Bound Encryption, а SHA256(host_key), который
  Chromium добавляет к значению начиная с версии схемы Cookies DB 24. Теперь префикс
  снимается только при meta.version >= 24 и с проверкой хэша (Mac и Win одинаково).
- v20 (App-Bound Encryption) распознаётся явно с понятной ошибкой.
- Ошибки копирования заблокированного файла БД не роняют процесс.

设计原则：任何异常都捕获并返回 None，不打断主流程。
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2

from .i18n import t as _
from .paths import (
    claude_desktop_cookies_path,
    claude_desktop_local_state_path,
    detect_platform,
)
from .log import get_logger

log = get_logger(__name__)

CLAUDE_HOSTS = ("claude.ai", ".claude.ai", ".claude.com")
SESSION_KEY_NAME = "sessionKey"
SESSION_KEY_ENV = "CLAUDE_SESSION_KEY"
# Начиная с этой версии схемы Chromium добавляет SHA256(host_key) перед значением
HOST_HASH_DB_VERSION = 24

# macOS Keychain 服务名候选（Chromium-family 应用都叫 "<Name> Safe Storage"）
MAC_KEYCHAIN_SVCES = [
    "Claude Safe Storage",
    "Claude for Desktop Safe Storage",
    "Claude.ai Safe Storage",
    "Anthropic Claude Safe Storage",
]

_SQL = ("SELECT host_key, name, encrypted_value FROM cookies "
        "WHERE host_key LIKE '%claude.ai' OR host_key LIKE '%claude.com'")


# ---------- 低层 ----------

def _query_db(conn: sqlite3.Connection) -> tuple[list[tuple[str, str, bytes]], int]:
    rows = [(h, n, v) for h, n, v in conn.execute(_SQL).fetchall()]
    version = 0
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        if row:
            version = int(row[0])
    except (sqlite3.Error, ValueError, TypeError):
        pass
    return rows, version


def _read_cookie_db() -> tuple[list[tuple[str, str, bytes]], int]:
    """Прочитать cookie claude.ai и версию схемы БД.

    Windows: Claude Desktop держит файл открытым — читаем копию во временном файле.
    """
    db_path = claude_desktop_cookies_path()

    if detect_platform() == "win":
        fd, tmp_name = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            try:
                shutil.copy2(db_path, tmp_path)
            except PermissionError as e:
                raise sqlite3.OperationalError(
                    f"Cookies DB заблокирован ({e}). Закройте Claude Desktop "
                    f"или задайте {SESSION_KEY_ENV}"
                ) from e
            conn = sqlite3.connect(str(tmp_path))
            try:
                return _query_db(conn)
            finally:
                conn.close()
        finally:
            tmp_path.unlink(missing_ok=True)

    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return _query_db(conn)
    finally:
        conn.close()


def _read_encrypted_cookies() -> list[tuple[str, str, bytes]]:
    """Совместимость: list of (host_key, name, encrypted_value)."""
    return _read_cookie_db()[0]


def _strip_host_hash(plaintext: bytes, host_key: str, db_version: int) -> bytes:
    """Снять 32-байтовый SHA256(host_key) префикс (Chromium DB version >= 24)."""
    if len(plaintext) < 32:
        return plaintext
    # Совпадение хэша — надёжный признак независимо от версии (meta мог не прочитаться)
    if host_key and plaintext[:32] == hashlib.sha256(host_key.encode("utf-8")).digest():
        return plaintext[32:]
    if db_version < HOST_HASH_DB_VERSION:
        return plaintext
    # Версия говорит, что префикс должен быть, но хэш не совпал — всё равно режем,
    # это поведение upstream, которое было проверено на Windows.
    log.debug("host hash mismatch для %s, режу 32 байта по версии БД %d",
              host_key, db_version)
    return plaintext[32:]


def _plain_or_none(enc_value: bytes) -> str | None:
    try:
        return enc_value.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ---------- macOS ----------

def _mac_find_safe_storage_key() -> bytes | None:
    """依次试几个可能的 Keychain 服务名，返回 16 字节的 AES key。"""
    for svce in MAC_KEYCHAIN_SVCES:
        try:
            out = subprocess.check_output(
                ["security", "find-generic-password", "-w", "-s", svce],
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).decode().strip()
            if out:
                log.debug("Keychain 服务名 '%s' 匹配成功", svce)
                # PBKDF2 派生：saltysalt, 1003 迭代, 16 字节
                return PBKDF2(out, b"saltysalt", dkLen=16, count=1003)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            continue
    log.warning("所有 Keychain 服务名均未匹配: %s", MAC_KEYCHAIN_SVCES)
    return None


def _mac_decrypt(enc_value: bytes, key: bytes, host_key: str = "",
                 db_version: int = 0) -> str | None:
    """Mac Chromium cookie 解密：v10/v11 = AES-128-CBC, IV = 16 пробелов."""
    if not enc_value:
        return None
    if enc_value[:3] not in (b"v10", b"v11"):
        return _plain_or_none(enc_value)
    try:
        cipher = AES.new(key, AES.MODE_CBC, b" " * 16)
        decrypted = cipher.decrypt(enc_value[3:])
        pad_len = decrypted[-1]
        if 1 <= pad_len <= 16:
            decrypted = decrypted[:-pad_len]
        decrypted = _strip_host_hash(decrypted, host_key, db_version)
        return decrypted.decode("utf-8")
    except (ValueError, IndexError, UnicodeDecodeError):
        return None


# ---------- Windows ----------

def _win_load_master_key() -> bytes | None:
    """从 Local State 读 encrypted_key，DPAPI 解密得到 AES key。"""
    try:
        import win32crypt  # type: ignore
    except ImportError:
        log.warning("pywin32 не установлен — DPAPI недоступен")
        return None
    try:
        local_state = json.loads(
            claude_desktop_local_state_path().read_text(encoding="utf-8")
        )
        os_crypt = local_state.get("os_crypt", {})
        if os_crypt.get("app_bound_encrypted_key"):
            log.info("Local State содержит app_bound_encrypted_key (v20 возможен)")
        encrypted_key = base64.b64decode(os_crypt["encrypted_key"])
        if encrypted_key[:5] != b"DPAPI":
            log.warning("encrypted_key без префикса DPAPI")
            return None
        _, key = win32crypt.CryptUnprotectData(encrypted_key[5:], None, None, None, 0)
        return bytes(key)
    except Exception as e:  # pywintypes.error не наследует OSError
        log.warning("Не удалось получить master key: %s", e)
        return None


def _win_decrypt(enc_value: bytes, key: bytes, host_key: str = "",
                 db_version: int = HOST_HASH_DB_VERSION) -> str | None:
    """Win Chromium v10/v11 AES-256-GCM cookie 解密."""
    if not enc_value:
        return None
    if enc_value[:3] == b"v20":
        log.warning(_("cookies.v20_unsupported", env=SESSION_KEY_ENV))
        return None
    if enc_value[:3] not in (b"v10", b"v11"):
        return _plain_or_none(enc_value)
    try:
        nonce = enc_value[3:15]
        ciphertext = enc_value[15:-16]
        tag = enc_value[-16:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        plaintext = _strip_host_hash(plaintext, host_key, db_version)
        return plaintext.decode("utf-8")
    except (ValueError, IndexError, UnicodeDecodeError):
        return None


# ---------- 高层 API ----------

def _valid_looking(sk: str) -> bool:
    return bool(sk) and sk.isascii() and sk.isprintable() and " " not in sk


def session_key_from_env() -> str | None:
    sk = (os.environ.get(SESSION_KEY_ENV) or "").strip()
    return sk or None


def get_session_key() -> str | None:
    """拿到 claude.ai 的 sessionKey。拿不到返回 None，不抛异常。

    Порядок: CLAUDE_SESSION_KEY → cookie Claude Desktop.
    """
    env_sk = session_key_from_env()
    if env_sk:
        log.info(_("cookies.env_override", env=SESSION_KEY_ENV))
        return env_sk

    try:
        rows, db_version = _read_cookie_db()
    except (sqlite3.Error, OSError, RuntimeError) as e:
        log.warning(_("cookies.read_error", error=str(e)))
        return None

    target = [
        (h, n, v) for (h, n, v) in rows
        if n == SESSION_KEY_NAME and any(host in h for host in CLAUDE_HOSTS)
    ]
    if not target:
        log.debug(_("cookies.not_found"))
        return None
    log.debug("Cookies DB version=%d, sessionKey записей: %d", db_version, len(target))

    platform = detect_platform()

    if platform == "mac":
        key = _mac_find_safe_storage_key()
        if not key:
            return None
        for h, _n, enc in target:
            decrypted = _mac_decrypt(enc, key, h, db_version)
            if decrypted and _valid_looking(decrypted):
                log.info(_("cookies.mac_decrypt_ok", n=len(decrypted)))
                return decrypted
        log.warning(_("cookies.mac_decrypt_fail"))
        return None

    if platform == "win":
        key = _win_load_master_key()
        if not key:
            return None
        for h, _n, enc in target:
            decrypted = _win_decrypt(enc, key, h, db_version)
            if decrypted and _valid_looking(decrypted):
                log.info(_("cookies.win_decrypt_ok", n=len(decrypted)))
                return decrypted
        log.warning(_("cookies.win_decrypt_fail"))
        return None

    # Linux：Desktop не поддерживается, только CLAUDE_SESSION_KEY
    return None


def describe_cookie_state() -> dict:
    """诊断用：告诉 UI 当前 cookie 情况。不解密 sessionKey 也不返回内容。"""
    r: dict = {"platform": detect_platform(),
               "env_override": session_key_from_env() is not None}
    try:
        rows, db_version = _read_cookie_db()
        r["cookies_readable"] = True
        r["cookies_db_version"] = db_version
        r["cookies_count"] = len(rows)
        r["has_session_key"] = any(n == SESSION_KEY_NAME for _, n, _ in rows)
        r["session_key_formats"] = sorted({
            v[:3].decode("ascii", "replace") for _, n, v in rows
            if n == SESSION_KEY_NAME and v
        })
        r["hosts_seen"] = sorted({h for h, _, _ in rows})
    except (FileNotFoundError, RuntimeError) as e:
        r["cookies_readable"] = False
        r["has_session_key"] = False
        r["error"] = str(e)
    except (sqlite3.Error, OSError) as e:
        r["cookies_readable"] = False
        r["has_session_key"] = False
        r["error"] = f"sqlite error: {e}"
    if r["env_override"]:
        r["has_session_key"] = True
    return r


if __name__ == "__main__":
    import json as _json
    print("--- cookie state ---")
    print(_json.dumps(describe_cookie_state(), ensure_ascii=False, indent=2))
    sk = get_session_key()
    if sk:
        safe = sk.encode("ascii", errors="replace").decode("ascii")
        print(f"--- sessionKey: {safe[:12]}...{safe[-6:]} (len={len(sk)}) ---")
    else:
        print("--- sessionKey: None (未登录/解密失败/账号不可用) ---")

"""跨平台路径探测 + 系统代理检测 + Claude Desktop 版本检测。

所有路径函数都只做路径拼接 + 存在性检查，不读取文件内容。
代理检测函数读取系统设置，返回 requests 兼容的 proxies dict。
版本检测函数从已安装的 Claude Desktop 提取版本号，用于构造匹配的 User-Agent。
"""
from __future__ import annotations
import functools
import os, re, sys, subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Platform = Literal["mac", "win", "linux"]


def detect_platform() -> Platform:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform == "win32":
        return "win"
    return "linux"


def detect_system_proxy() -> dict[str, str]:
    """检测系统代理设置，返回 requests 兼容的 proxies dict。

    优先级：Windows 注册表 > macOS networksetup > 环境变量。
    返回示例：{"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
    无代理时返回空 dict。
    """
    p = detect_platform()

    # 1. Windows 注册表（Clash / V2Ray / Shadowsocks 等设的系统代理）
    if p == "win":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enabled:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                winreg.CloseKey(key)
                if server:
                    # server 可能是 "host:port" 或 "http=host:port;https=host:port"
                    if "=" in server or ";" in server:
                        proxies = {}
                        for part in server.split(";"):
                            if "=" in part:
                                scheme, addr = part.split("=", 1)
                                proxies[scheme.strip()] = f"http://{addr.strip()}"
                        if proxies:
                            return proxies
                    else:
                        proxy_url = f"http://{server}"
                        return {"http": proxy_url, "https": proxy_url}
            winreg.CloseKey(key)
        except Exception:
            pass

    # 2. macOS networksetup（系统偏好设置里的代理）
    if p == "mac":
        try:
            # 获取活跃网络服务名
            result = subprocess.run(
                ["networksetup", "-listallnetworkservices"],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.strip().split("\n")[1:]:
                service = line.strip()
                if service.startswith("*"):
                    continue
                # 检查 HTTP proxy
                proc = subprocess.run(
                    ["networksetup", "-getwebproxy", service],
                    capture_output=True, text=True, timeout=5)
                if "Enabled: Yes" in proc.stdout:
                    host = port = ""
                    for pline in proc.stdout.split("\n"):
                        if pline.startswith("Server:"):
                            host = pline.split(":", 1)[1].strip()
                        elif pline.startswith("Port:"):
                            port = pline.split(":", 1)[1].strip()
                    if host and port:
                        proxy_url = f"http://{host}:{port}"
                        return {"http": proxy_url, "https": proxy_url}
        except Exception:
            pass

    # 3. 环境变量（所有平台的 fallback）
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
                or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if env_proxy:
        return {"http": env_proxy, "https": env_proxy}

    return {}


# ──────────────────────────────────────────────
# Автопоиск профиля Claude Desktop (fork BUTLER-SPB)
#
# У разных пользователей профиль лежит в разных местах:
#   %APPDATA%\Claude                                   — классический установщик
#   %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude — MSIX (старые сборки)
#   %LOCALAPPDATA%\Claude-Data\claude_<family>         — MSIX (новые сборки, --user-data-dir)
#   %LOCALAPPDATA%\Claude-3p и пр.                      — другие варианты сборок
# Порядок выбора: профиль запущенного Claude (--user-data-dir) → сохранённый
# «Подготовкой» в config.json → самый свежий по Cookies среди найденных.
# ──────────────────────────────────────────────

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


@dataclass
class DesktopProfile:
    path: Path
    source: str                 # "process" | "config" | "scan"
    cookies_path: Path | None
    cookies_mtime: float
    has_local_state: bool
    has_cache: bool

    @property
    def usable(self) -> bool:
        return self.has_local_state and self.cookies_path is not None

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "source": self.source,
            "cookies_path": str(self.cookies_path) if self.cookies_path else None,
            "cookies_mtime": self.cookies_mtime,
            "has_local_state": self.has_local_state,
            "has_cache": self.has_cache,
            "usable": self.usable,
        }


def _profile_info(path: Path, source: str) -> DesktopProfile:
    cookies = None
    mtime = 0.0
    for c in (path / "Network" / "Cookies", path / "Cookies"):
        if c.is_file():
            cookies = c
            try:
                mtime = c.stat().st_mtime
            except OSError:
                pass
            break
    return DesktopProfile(
        path=path,
        source=source,
        cookies_path=cookies,
        cookies_mtime=mtime,
        has_local_state=(path / "Local State").is_file(),
        has_cache=(path / "Cache" / "Cache_Data").is_dir(),
    )


def _scan_profile_dirs() -> list[Path]:
    """Все каталоги, похожие на профиль Claude Desktop (есть «Local State»)."""
    p = detect_platform()
    roots: list[Path] = []
    if p == "win":
        for var in ("APPDATA", "LOCALAPPDATA"):
            v = os.environ.get(var)
            if v:
                roots.append(Path(v))
    elif p == "mac":
        roots.append(Path.home() / "Library" / "Application Support")
    else:
        roots.append(Path.home() / ".config")

    found: list[Path] = []

    def consider(d: Path) -> None:
        if (d / "Local State").is_file() and d not in found:
            found.append(d)

    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for d in entries:
            if not d.is_dir() or not d.name.lower().startswith("claude"):
                continue
            consider(d)
            # Claude-Data\claude_<family> и подобные — на уровень глубже
            try:
                for sub in d.iterdir():
                    if sub.is_dir():
                        consider(sub)
            except OSError:
                pass

    # MSIX: %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude*
    if p == "win":
        la = os.environ.get("LOCALAPPDATA")
        pkgs = Path(la) / "Packages" if la else None
        if pkgs and pkgs.is_dir():
            try:
                for d in pkgs.iterdir():
                    if not d.name.lower().startswith("claude"):
                        continue
                    roaming = d / "LocalCache" / "Roaming"
                    if roaming.is_dir():
                        for sub in roaming.iterdir():
                            if sub.is_dir() and sub.name.lower().startswith("claude"):
                                consider(sub)
            except OSError:
                pass
    return found


_RUNNING_CACHE: list[Path] | None = None


def _running_profile_dirs() -> list[Path]:
    """--user-data-dir запущенных процессов Claude Desktop (кэш на процесс)."""
    global _RUNNING_CACHE
    if _RUNNING_CACHE is None:
        _RUNNING_CACHE = _probe_running_profile_dirs()
    return list(_RUNNING_CACHE)


def _probe_running_profile_dirs() -> list[Path]:
    if detect_platform() not in ("win", "mac"):
        return []
    dirs: list[Path] = []
    for proc in list_desktop_processes():
        d = user_data_dir_from_cmdline(proc.cmdline, detect_platform())
        if d and d.is_dir() and d not in dirs:
            dirs.append(d)
    return dirs


def user_data_dir_from_cmdline(cmdline: str, platform: str) -> Path | None:
    """--user-data-dir из командной строки. Windows: значение без пробелов или в
    кавычках (дальше бывает « /prefetch:4»). macOS: ps выводит argv без кавычек,
    а путь «~/Library/Application Support/…» содержит пробел — берём до « --»."""
    if platform == "mac":
        m = re.search(r'--user-data-dir=(?:"([^"]+)"|(.+?))(?= --|$)', cmdline)
    else:
        m = re.search(r'--user-data-dir=(?:"([^"]+)"|(\S+))', cmdline)
    if not m:
        return None
    return Path((m.group(1) or m.group(2)).strip())


@dataclass
class ProcInfo:
    pid: int
    exe: str
    cmdline: str

    @property
    def is_main(self) -> bool:
        """Главный (browser) процесс Electron — без --type=."""
        return "--type=" not in self.cmdline


def _is_desktop_exe(exe: str) -> bool:
    """Claude Desktop — Electron: рядом с exe лежит resources\\. Так отсекаем
    одноимённый claude.exe от Claude Code CLI (~/.local/bin/claude.exe)."""
    if not exe:
        return False
    return (Path(exe).parent / "resources").is_dir()


def list_desktop_processes() -> list[ProcInfo]:
    """Процессы Claude Desktop текущего пользователя.
    Windows — WinAPI без PowerShell; macOS — ps (процессы внутри Claude*.app)."""
    p = detect_platform()
    try:
        if p == "win":
            return [x for x in _win_list_processes("claude.exe") if _is_desktop_exe(x.exe)]
        if p == "mac":
            return _mac_list_processes()
    except Exception:
        pass
    return []


_MAC_APP_RE = re.compile(r"/Claude[^/]*\.app/Contents/")


def _parse_ps_cols(output: str, ncols: int) -> list[list[str]]:
    rows = []
    for line in output.splitlines():
        parts = line.strip().split(None, ncols - 1)
        if len(parts) == ncols and parts[0].isdigit():
            rows.append(parts)
    return rows


def parse_mac_ps(comm_out: str, command_out: str, uid: int) -> list[ProcInfo]:
    """Процессы Claude*.app этого пользователя.

    comm_out — `ps -axww -o pid=,uid=,comm=` (comm = путь исполняемого файла):
    по нему решаем, что это Claude Desktop (а не процесс, у которого путь к
    Claude.app всего лишь встречается в аргументах, и не CLI Claude Code).
    command_out — `ps -axww -o pid=,command=`: полная строка для --type/--user-data-dir.
    """
    commands = {int(p): c for p, c in _parse_ps_cols(command_out, 2)}
    out: list[ProcInfo] = []
    for pid_s, uid_s, exe in _parse_ps_cols(comm_out, 3):
        if not uid_s.isdigit() or int(uid_s) != uid or not _MAC_APP_RE.search(exe):
            continue
        pid = int(pid_s)
        out.append(ProcInfo(pid=pid, exe=exe.strip(), cmdline=commands.get(pid, exe)))
    return out


def _mac_list_processes() -> list[ProcInfo]:
    def ps(cols: str) -> str:
        return subprocess.run(["ps", "-axww", "-o", cols], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10).stdout
    return parse_mac_ps(ps("pid=,uid=,comm="), ps("pid=,command="), os.getuid())


def _win_list_processes(image_name: str) -> list[ProcInfo]:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                    ("Buffer", ctypes.c_void_p)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong)]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ProcessCommandLineInformation = 60
    INVALID = wintypes.HANDLE(-1).value

    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == INVALID:
        return []
    pids: list[int] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == image_name.lower():
                pids.append(int(entry.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)

    out: list[ProcInfo] = []
    for pid in pids:
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            exe = buf.value if k32.QueryFullProcessImageNameW(
                h, 0, buf, ctypes.byref(size)) else ""
            need = ctypes.c_ulong(0)
            ntdll.NtQueryInformationProcess(h, ProcessCommandLineInformation,
                                            None, 0, ctypes.byref(need))
            cmdline = ""
            if need.value:
                raw = ctypes.create_string_buffer(need.value)
                st = ntdll.NtQueryInformationProcess(
                    h, ProcessCommandLineInformation, raw, need.value, ctypes.byref(need))
                if st >= 0:
                    us = UNICODE_STRING.from_buffer(raw)
                    if us.Buffer and us.Length:
                        cmdline = ctypes.wstring_at(us.Buffer, us.Length // 2)
            out.append(ProcInfo(pid=pid, exe=exe, cmdline=cmdline))
        finally:
            k32.CloseHandle(h)
    return out


def _configured_profile_dir() -> Path | None:
    try:
        from .config import load_config
        v = load_config().get("desktop_profile_dir")
    except Exception:
        return None
    return Path(v) if v else None


def discover_desktop_profiles() -> list[DesktopProfile]:
    """Все найденные профили, лучший — первым."""
    seen: dict[str, DesktopProfile] = {}
    ordered: list[DesktopProfile] = []

    def add(path: Path, source: str) -> None:
        key = str(path).lower()
        if key in seen:
            return
        info = _profile_info(path, source)
        seen[key] = info
        ordered.append(info)

    for d in _running_profile_dirs():
        add(d, "process")
    cfg_dir = _configured_profile_dir()
    if cfg_dir and cfg_dir.is_dir():
        add(cfg_dir, "config")
    for d in sorted(_scan_profile_dirs(),
                    key=lambda d: _profile_info(d, "scan").cookies_mtime,
                    reverse=True):
        add(d, "scan")

    priority = {"process": 0, "config": 1, "scan": 2}
    # Сначала годные (Cookies + Local State), внутри — по источнику и свежести
    ordered.sort(key=lambda p: (not p.usable, priority[p.source], -p.cookies_mtime))
    return ordered


_ROOT_CACHE: Path | None = None


def reset_desktop_root_cache() -> None:
    global _ROOT_CACHE, _RUNNING_CACHE
    _ROOT_CACHE = None
    _RUNNING_CACHE = None


def _legacy_default_root() -> Path:
    p = detect_platform()
    if p == "mac":
        return Path.home() / "Library" / "Application Support" / "Claude"
    if p == "win":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Claude"
        raise RuntimeError("Windows 上 %APPDATA% 和 %LOCALAPPDATA% 环境变量均为空")
    return Path.home() / ".config" / "Claude"


def _claude_desktop_root() -> Path:
    """Claude Desktop 应用数据根目录 (автопоиск, результат кэшируется на процесс)."""
    global _ROOT_CACHE
    if _ROOT_CACHE is not None:
        return _ROOT_CACHE
    profiles = discover_desktop_profiles()
    if profiles:
        _ROOT_CACHE = profiles[0].path
        return _ROOT_CACHE
    # 都没找到，返回标准路径（让调用方报错信息更清晰）
    return _legacy_default_root()


def claude_desktop_installed() -> bool:
    """Claude Desktop 是否已装。"""
    return _claude_desktop_root().is_dir()


def claude_desktop_cache_dir() -> Path:
    """Chromium Simple Cache 目录。"""
    d = _claude_desktop_root() / "Cache" / "Cache_Data"
    if not d.is_dir():
        raise FileNotFoundError(f"Claude Desktop 缓存目录不存在: {d}")
    return d


def claude_desktop_cookies_path() -> Path:
    """Cookies SQLite 文件。

    较新 Chromium 把 Cookies 移到了 `Network/Cookies`。两个位置都试。
    """
    root = _claude_desktop_root()
    candidates = [
        root / "Network" / "Cookies",
        root / "Cookies",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        f"没找到 Cookies 文件（试过: {', '.join(str(c) for c in candidates)}）"
    )


def claude_desktop_local_state_path() -> Path:
    """Local State JSON（Windows cookie 解密要读 os_crypt.encrypted_key）。"""
    p = _claude_desktop_root() / "Local State"
    if not p.is_file():
        raise FileNotFoundError(f"没找到 Local State 文件: {p}")
    return p


def _cli_projects_candidate() -> Path:
    """Каталог проектов Claude Code: учитывает CLAUDE_CONFIG_DIR, иначе ~/.claude."""
    env = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if env:
        d = Path(env).expanduser() / "projects"
        if d.is_dir():
            return d
    return Path.home() / ".claude" / "projects"


def claude_cli_projects_dir() -> Path:
    """~/.claude/projects/ —— Claude Code 本地会话日志目录。"""
    d = _cli_projects_candidate()
    if not d.is_dir():
        raise FileNotFoundError(f"Claude Code 项目目录不存在: {d}")
    return d


def claude_cli_projects_dir_optional() -> Path | None:
    """同上，但不存在时返回 None 而不是抛异常（用户可能没装 Claude Code CLI）。"""
    d = _cli_projects_candidate()
    return d if d.is_dir() else None


def report() -> dict:
    """一次性诊断报告：所有关键路径的状态。用于启动时日志 + GUI 展示。"""
    r: dict = {"platform": detect_platform()}
    r["claude_desktop_root"] = str(_claude_desktop_root())
    r["desktop_profiles"] = [p.to_dict() for p in discover_desktop_profiles()]
    r["claude_desktop_installed"] = claude_desktop_installed()
    for name, fn in [
        ("cache_dir", claude_desktop_cache_dir),
        ("cookies_path", claude_desktop_cookies_path),
        ("local_state_path", claude_desktop_local_state_path),
        ("cli_projects_dir", claude_cli_projects_dir),
    ]:
        try:
            r[name] = str(fn())
            r[f"{name}_exists"] = True
        except FileNotFoundError as e:
            r[name] = str(e)
            r[f"{name}_exists"] = False
    return r


# ──────────────────────────────────────────────
# Claude Desktop 版本检测（用于构造匹配的 User-Agent）
# ──────────────────────────────────────────────

def _claude_desktop_app_bundle() -> Path | None:
    """macOS: Claude.app bundle 路径。"""
    p = Path("/Applications/Claude.app")
    return p if p.is_dir() else None


def _detect_mac_claude_info() -> dict[str, str | None]:
    """macOS: 从 .app bundle 提取 Claude / Electron / Chrome 版本号。"""
    import plistlib, mmap, re

    info: dict[str, str | None] = {
        "claude_version": None,
        "electron_version": None,
        "chrome_version": None,
    }
    app = _claude_desktop_app_bundle()
    if not app:
        return info

    # Claude Desktop 版本
    info_plist = app / "Contents" / "Info.plist"
    if info_plist.is_file():
        try:
            with open(info_plist, "rb") as f:
                info["claude_version"] = plistlib.load(f).get(
                    "CFBundleShortVersionString"
                )
        except Exception:
            pass

    # Electron 版本
    electron_plist = (
        app
        / "Contents"
        / "Frameworks"
        / "Electron Framework.framework"
        / "Versions"
        / "A"
        / "Resources"
        / "Info.plist"
    )
    if electron_plist.is_file():
        try:
            with open(electron_plist, "rb") as f:
                info["electron_version"] = plistlib.load(f).get("CFBundleVersion")
        except Exception:
            pass

    # Chrome 版本（从 Electron Framework 二进制的字符串表提取）
    framework_bin = (
        app
        / "Contents"
        / "Frameworks"
        / "Electron Framework.framework"
        / "Versions"
        / "A"
        / "Electron Framework"
    )
    if framework_bin.is_file():
        try:
            with open(framework_bin, "rb") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    m = re.search(rb"Chrome/(\d+\.\d+\.\d+\.\d+)", mm)
                    if m:
                        info["chrome_version"] = m.group(1).decode()
        except Exception:
            pass

    return info


def _find_win_claude_exe() -> Path | None:
    """在 Windows 上定位 Claude Desktop exe（含 Microsoft Store 安装）。"""
    candidates: list[Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "Programs" / "Claude" / "Claude.exe")
        candidates.append(Path(local_appdata) / "Claude" / "Claude.exe")
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "Claude" / "Claude.exe")

    for c in candidates:
        if c.is_file():
            return c

    # Запущенный Claude Desktop — точный путь к exe без PowerShell
    for proc in list_desktop_processes():
        if proc.exe and Path(proc.exe).is_file():
            return Path(proc.exe)

    # Microsoft Store 安装：通过 Get-AppxPackage 查找
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
             "(Get-AppxPackage -Name '*Claude*' | Select-Object -First 1).InstallLocation"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=_NO_WINDOW,
        )
        if result.returncode == 0 and result.stdout.strip():
            install_dir = Path(result.stdout.strip())
            # Store 版 exe 在 app\ 子目录下，该目录 ACL 允许直接访问
            exe = install_dir / "app" / "claude.exe"
            if exe.is_file():
                return exe
    except Exception:
        pass

    return None


def _detect_win_claude_info() -> dict[str, str | None]:
    """Windows: 从已安装的 Claude Desktop 提取版本号。

    支持标准安装和 Microsoft Store (UWP) 安装。
    Claude 版本从 exe VERSIONINFO 读取（pywin32 → PowerShell 兜底）。
    Electron 版本读 exe 同目录下的 ``version`` 文件。
    Chrome 版本从 Electron 映射表推导。
    """
    info: dict[str, str | None] = {
        "claude_version": None,
        "electron_version": None,
        "chrome_version": None,
    }

    exe_path = _find_win_claude_exe()
    if not exe_path:
        return info

    # Claude 版本：pywin32 优先（快），PowerShell 兜底（Store 版 pywin32 可能因 ACL 失败）
    try:
        import win32api

        lang, codepage = win32api.GetFileVersionInfo(
            str(exe_path), "\\VarFileInfo\\Translation"
        )[0]
        version = win32api.GetFileVersionInfo(
            str(exe_path),
            f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileVersion",
        )
        if version:
            info["claude_version"] = version
    except Exception:
        pass

    if not info["claude_version"]:
        try:
            lit = "'" + re.sub("['‘’‚‛]",
                               lambda m: m.group() * 2, str(exe_path)) + "'"
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Item -LiteralPath {lit}).VersionInfo.FileVersion"],
                capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
            )
            if result.returncode == 0 and result.stdout.strip():
                info["claude_version"] = result.stdout.strip()
        except Exception:
            pass

    # Electron 版本：exe 同目录下的 version 文件（Electron 惯例）
    version_file = exe_path.parent / "version"
    if version_file.is_file():
        try:
            electron_ver = version_file.read_text().strip()
            if electron_ver:
                info["electron_version"] = electron_ver
        except Exception:
            pass

    # Chrome 版本：从 Electron 映射表推导
    info["chrome_version"] = _chrome_from_electron(info["electron_version"])

    return info


@functools.lru_cache(maxsize=1)
def detect_claude_desktop_info() -> dict[str, str | None]:
    """检测已安装的 Claude Desktop 版本信息。

    Returns:
        {"claude_version": "1.4758.0",
         "electron_version": "41.3.0",
         "chrome_version": "146.0.7680.188"}
    未安装或检测失败时对应字段为 None。
    """
    p = detect_platform()
    if p == "mac":
        return _detect_mac_claude_info()
    if p == "win":
        return _detect_win_claude_info()
    return {"claude_version": None, "electron_version": None, "chrome_version": None}


# 已知的 Electron → Chromium 版本映射（兜底用）
_ELECTRON_CHROME_MAP: dict[str, str] = {
    "30.0.0": "124.0.6367.243",
    "31.0.0": "126.0.6478.234",
    "32.0.0": "128.0.6613.162",
    "33.0.0": "130.0.6723.170",
    "34.0.0": "132.0.6834.194",
    "35.0.0": "134.0.6998.165",
    "36.0.0": "136.0.7108.80",
    "37.0.0": "138.0.7204.87",
    "38.0.0": "140.0.7339.95",
    "39.0.0": "142.0.7462.96",
    "40.0.0": "144.0.7559.171",
    "41.0.0": "146.0.7680.188",
}


def _chrome_from_electron(electron_version: str | None) -> str | None:
    """从 Electron 版本推导 Chromium 版本（查表 + 前缀匹配）。"""
    if not electron_version:
        return None
    # 精确匹配
    if electron_version in _ELECTRON_CHROME_MAP:
        return _ELECTRON_CHROME_MAP[electron_version]
    # 前缀匹配（如 "41.3.0" 匹配 "41."）
    prefix = electron_version.split(".")[0] + "."
    for ev, cv in _ELECTRON_CHROME_MAP.items():
        if ev.startswith(prefix):
            return cv
    return None


def get_user_agent() -> str:
    """构造与当前安装的 Claude Desktop 匹配的 User-Agent 字符串。

    动态检测失败时回退到已知的旧版本（claudeai/0.14.2 + Electron/30 + Chrome/124）。
    """
    import platform as plat

    info = detect_claude_desktop_info()
    claude_ver = info.get("claude_version") or "0.14.2"
    electron_ver = info.get("electron_version") or "30.0.0"
    chrome_ver = info.get("chrome_version") or _chrome_from_electron(
        info.get("electron_version")
    ) or "124.0.0.0"

    if sys.platform == "darwin":
        # macOS 版本号用下划线格式，如 15.4.0 → 15_4_0
        mac_ver = plat.mac_ver()[0] or "10.15.7"
        os_part = f"Macintosh; Intel Mac OS X {mac_ver.replace('.', '_')}"
    elif sys.platform == "win32":
        os_part = "Windows NT 10.0; Win64; x64"
    else:
        os_part = "X11; Linux x86_64"

    return (
        f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) claudeai/{claude_ver} "
        f"Chrome/{chrome_ver} Electron/{electron_ver} Safari/537.36"
    )


if __name__ == "__main__":
    import json

    print(json.dumps(report(), ensure_ascii=False, indent=2))

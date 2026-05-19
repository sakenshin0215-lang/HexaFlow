import os
import platform
import subprocess
import time
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def _default_user_data_dir() -> str:
    return str(Path.home() / "pw-profiles" / "hexaflow-cdp")


def _platform_key() -> str:
    return platform.system().lower()


def _candidate_chrome_binaries() -> list[str]:
    system = _platform_key()
    candidates: list[str] = []

    if "darwin" in system:
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                str(
                    Path.home()
                    / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                ),
                "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        )
    elif "windows" in system:
        env_roots = [
            os.getenv("PROGRAMFILES", ""),
            os.getenv("PROGRAMFILES(X86)", ""),
            os.getenv("LOCALAPPDATA", ""),
        ]
        suffixes = [
            r"Google\Chrome\Application\chrome.exe",
            r"Google\Chrome Beta\Application\chrome.exe",
            r"Google\Chrome Dev\Application\chrome.exe",
            r"Google\Chrome SxS\Application\chrome.exe",
            r"Chromium\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
        ]
        for root in env_roots:
            if not root:
                continue
            for suffix in suffixes:
                candidates.append(str(Path(root) / suffix))
    else:
        for name in [
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
            "msedge",
        ]:
            resolved = shutil.which(name)
            if resolved:
                candidates.append(resolved)

    seen = set()
    ordered = []
    for path in candidates:
        if path and path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _normalize_user_supplied_binary(raw_path: str) -> str:
    if not raw_path:
        return ""
    expanded = os.path.expanduser(os.path.expandvars(raw_path.strip()))
    if expanded.endswith(".app"):
        app_name = Path(expanded).stem
        mac_exec = Path(expanded) / "Contents" / "MacOS" / app_name
        return str(mac_exec)
    return expanded


def resolve_chrome_binary(config: "CDPConfig") -> str:
    explicit = _normalize_user_supplied_binary(config.chrome_binary)
    if explicit:
        return explicit

    for candidate in _candidate_chrome_binaries():
        if Path(candidate).exists():
            return candidate

    return ""


def is_default_system_chrome_profile(user_data_dir: str) -> bool:
    if not user_data_dir:
        return False
    raw = str(Path(user_data_dir).expanduser())
    system = _platform_key()
    markers = []
    if "darwin" in system:
        markers = [
            str(Path.home() / "Library/Application Support/Google/Chrome"),
            str(Path.home() / "Library/Application Support/Microsoft Edge"),
        ]
    elif "windows" in system:
        local_app_data = os.getenv("LOCALAPPDATA", "")
        roaming = os.getenv("APPDATA", "")
        if local_app_data:
            markers.extend(
                [
                    str(Path(local_app_data) / "Google/Chrome/User Data"),
                    str(Path(local_app_data) / "Microsoft/Edge/User Data"),
                ]
            )
        if roaming:
            markers.append(str(Path(roaming) / "Chromium"))
    else:
        markers = [
            str(Path.home() / ".config/google-chrome"),
            str(Path.home() / ".config/chromium"),
            str(Path.home() / ".config/microsoft-edge"),
        ]

    normalized_raw = raw.rstrip("/\\").lower()
    return any(normalized_raw == marker.rstrip("/\\").lower() for marker in markers)


@dataclass
class CDPConfig:
    port: int = int(os.getenv("CDP_PORT", "9333"))
    endpoint: str = os.getenv("CDP_ENDPOINT", "")
    user_data_dir: str = os.getenv(
        "CHROME_USER_DATA_DIR", _default_user_data_dir()
    )
    profile_directory: str = os.getenv("CHROME_PROFILE_DIRECTORY", "Default")
    chrome_binary: str = os.getenv("CHROME_BINARY", "")
    auto_launch: bool = True
    launch_timeout_sec: int = 25

    def __post_init__(self):
        if not self.endpoint:
            self.endpoint = f"http://127.0.0.1:{self.port}"
        self.user_data_dir = os.path.expanduser(os.path.expandvars(self.user_data_dir))
        self.chrome_binary = _normalize_user_supplied_binary(self.chrome_binary)


def is_cdp_ready(endpoint: str) -> bool:
    try:
        with urlopen(f"{endpoint}/json/version", timeout=1.5):
            return True
    except URLError:
        return False
    except Exception:
        return False


def get_cdp_version_info(endpoint: str) -> dict:
    try:
        with urlopen(f"{endpoint}/json/version", timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def is_supported_chromium_cdp(version_info: dict) -> bool:
    browser = (version_info.get("Browser") or "").lower()
    websocket_url = (version_info.get("webSocketDebuggerUrl") or "").lower()
    supported_tokens = [
        "chrome/",
        "chromium/",
        "headlesschrome/",
        "microsoft edge/",
        "edg/",
    ]
    return any(token in browser for token in supported_tokens) and "/devtools/browser/" in websocket_url


def launch_chrome_with_cdp(config: CDPConfig, start_url: str = "about:blank"):
    Path(config.user_data_dir).mkdir(parents=True, exist_ok=True)
    chrome_bin = resolve_chrome_binary(config)
    if not chrome_bin:
        raise RuntimeError(
            "未找到可用的 Chrome/Chromium/Edge 可执行文件。"
            "请安装 Chrome 系浏览器，或通过 CHROME_BINARY 指定完整路径。"
        )
    if not Path(chrome_bin).exists():
        raise RuntimeError(
            f"CHROME_BINARY 不存在或不可访问: {chrome_bin}"
        )

    args = [
        f"--remote-debugging-port={config.port}",
        f"--user-data-dir={config.user_data_dir}",
        f"--profile-directory={config.profile_directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        start_url,
    ]
    cmd = [chrome_bin, *args]
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def ensure_cdp_browser(config: CDPConfig, start_url: str = "about:blank"):
    if is_cdp_ready(config.endpoint):
        version_info = get_cdp_version_info(config.endpoint)
        if not is_supported_chromium_cdp(version_info):
            browser_name = version_info.get("Browser", "<unknown>")
            raise RuntimeError(
                "检测到远程调试端口已被其他 CDP 服务占用，"
                f"当前端口 {config.port} 返回 Browser={browser_name}，"
                "它不像是 Playwright 可稳定接管的标准 Chrome/Chromium 实例。"
                "请改用新的 CDP_PORT（例如 9333/9444），"
                "或先关闭占用该端口的程序后重试。"
            )
        return

    if not config.auto_launch:
        raise RuntimeError(
            f"未检测到 CDP 浏览器，请先手动启动并开启远程调试端口 {config.port}"
        )

    profile_hint = ""
    if is_default_system_chrome_profile(config.user_data_dir):
        profile_hint = (
            "检测到你正在尝试复用系统默认浏览器目录。"
            "这在浏览器已打开时很容易因为 profile 锁导致 CDP 拉起失败。"
            f"更稳妥的做法是使用独立目录，例如: {Path.home() / 'pw-profiles' / 'hexaflow-cdp'}。"
        )

    launch_chrome_with_cdp(config, start_url=start_url)
    deadline = time.time() + config.launch_timeout_sec
    while time.time() < deadline:
        if is_cdp_ready(config.endpoint):
            version_info = get_cdp_version_info(config.endpoint)
            if not is_supported_chromium_cdp(version_info):
                browser_name = version_info.get("Browser", "<unknown>")
                raise RuntimeError(
                    "CDP 端口已经起来了，但返回的不是可稳定接管的 Chrome/Chromium 实例。"
                    f" Browser={browser_name}, endpoint={config.endpoint}"
                )
            return
        time.sleep(0.5)

    chrome_bin = resolve_chrome_binary(config) or "<not found>"
    raise RuntimeError(
        "CDP 启动失败。"
        f" port={config.port}, endpoint={config.endpoint}, chrome_binary={chrome_bin}, "
        f"user_data_dir={config.user_data_dir}, profile={config.profile_directory}。"
        " 请确认浏览器没有被默认 profile 锁住，或改用独立的 CHROME_USER_DATA_DIR 后重试。"
        + (f" {profile_hint}" if profile_hint else "")
    )

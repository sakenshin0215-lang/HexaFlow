import os
import platform
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


@dataclass
class CDPConfig:
    port: int = int(os.getenv("CDP_PORT", "9222"))
    endpoint: str = os.getenv("CDP_ENDPOINT", "")
    user_data_dir: str = os.getenv(
        "CHROME_USER_DATA_DIR", str(Path.home() / "pw-profiles" / "hexaflow-cdp")
    )
    profile_directory: str = os.getenv("CHROME_PROFILE_DIRECTORY", "Default")
    chrome_binary: str = os.getenv("CHROME_BINARY", "")
    auto_launch: bool = True
    launch_timeout_sec: int = 25

    def __post_init__(self):
        if not self.endpoint:
            self.endpoint = f"http://127.0.0.1:{self.port}"


def is_cdp_ready(endpoint: str) -> bool:
    try:
        with urlopen(f"{endpoint}/json/version", timeout=1.5):
            return True
    except URLError:
        return False
    except Exception:
        return False


def launch_chrome_with_cdp(config: CDPConfig, start_url: str = "about:blank"):
    Path(config.user_data_dir).mkdir(parents=True, exist_ok=True)
    args = [
        f"--remote-debugging-port={config.port}",
        f"--user-data-dir={config.user_data_dir}",
        f"--profile-directory={config.profile_directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        start_url,
    ]

    system = platform.system().lower()
    if "darwin" in system:
        cmd = ["open", "-na", "Google Chrome", "--args", *args]
    elif "windows" in system:
        chrome_bin = config.chrome_binary or r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
        cmd = [chrome_bin, *args]
    else:
        chrome_bin = config.chrome_binary or "google-chrome"
        cmd = [chrome_bin, *args]

    subprocess.Popen(cmd)


def ensure_cdp_browser(config: CDPConfig, start_url: str = "about:blank"):
    if is_cdp_ready(config.endpoint):
        return

    if not config.auto_launch:
        raise RuntimeError(
            f"未检测到 CDP 浏览器，请先手动启动并开启远程调试端口 {config.port}"
        )

    launch_chrome_with_cdp(config, start_url=start_url)
    deadline = time.time() + config.launch_timeout_sec
    while time.time() < deadline:
        if is_cdp_ready(config.endpoint):
            return
        time.sleep(0.5)

    raise RuntimeError(
        f"CDP 启动失败，请手动用 --remote-debugging-port={config.port} 启动 Chrome"
    )


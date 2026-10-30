"""The bot's own Chrome: your installed Google Chrome with a dedicated profile
(.chrome-profile), started off-screen, controlled over a random local port with
Playwright. You log in to each site once yourself (python main.py login); the
bot reuses those sessions. It never types passwords and never solves CAPTCHAs.
"""
from __future__ import annotations

import os
import random
import subprocess
import time
import urllib.request

from .config import DATA_DIR, ROOT, SCREENS_DIR

PROFILE_DIR = ROOT / ".chrome-profile"
PORT_FILE = DATA_DIR / ".cdp_port"
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
NO_WINDOW = 0x08000000      # CREATE_NO_WINDOW

CHROME_ARGS = [
    "--no-first-run", "--no-default-browser-check", "--disable-notifications",
    "--hide-crash-restore-bubble", "--disable-session-crashed-bubble", "--noerrdialogs",
    "--disable-features=Translate,MediaRouter", "--deny-permission-prompts",
    # keep working at full speed while off-screen
    "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--window-position=-3000,0", "--window-size=1920,1080",
]

LOGIN_PAGES = {
    "linkedin": "https://www.linkedin.com/login",
    "naukri": "https://www.naukri.com/nlogin/login",
    "indeed": "https://secure.indeed.com/auth",
    "glassdoor": "https://www.glassdoor.co.in/profile/login_input.htm",
    "unstop": "https://unstop.com/auth/login",
}


def _port() -> int | None:
    try:
        return int(PORT_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _chrome_up(port: int | None) -> bool:
    if not port:
        return False
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except OSError:
        return False


def kill_bot_chrome() -> None:
    """Stop only Chrome processes using the bot's profile - never your normal Chrome."""
    if os.name != "nt":
        subprocess.run(["pkill", "-f", "chrome-profile"], capture_output=True)
        return
    ps = ("Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
          "Where-Object { $_.CommandLine -like '*\\.chrome-profile*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=NO_WINDOW,
                   capture_output=True)
    time.sleep(2)


def _start_chrome() -> int:
    exe = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if not exe:
        raise SystemExit("Google Chrome not found - install it first")
    kill_bot_chrome()
    port = random.randint(20000, 60000)
    PORT_FILE.write_text(str(port))
    subprocess.Popen([exe, f"--remote-debugging-port={port}", f"--user-data-dir={PROFILE_DIR}",
                      *CHROME_ARGS, "about:blank"], creationflags=NO_WINDOW if os.name == "nt" else 0)
    for _ in range(30):
        time.sleep(1)
        if _chrome_up(port):
            time.sleep(2)
            return port
    raise SystemExit("Chrome started but its control port did not open")


def connect(show: bool = False):
    """Returns (playwright, context). show=True brings the window on screen."""
    from playwright.sync_api import sync_playwright

    port = _port()
    if not _chrome_up(port):
        port = _start_chrome()
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception as e:  # noqa: BLE001
        pw.stop()
        raise SystemExit("Could not attach to the bot's Chrome. Run: python main.py stop, then retry.") from e
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    place(ctx, show)
    return pw, ctx


def place(ctx, show: bool) -> None:
    """Off-screen at desktop size, or on screen for logging in / solving a check."""
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        cdp = ctx.new_cdp_session(page)
        win = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": "normal"}})
        bounds = ({"left": 50, "top": 30, "width": 1400, "height": 900} if show
                  else {"left": -3000, "top": 0, "width": 1920, "height": 1080})
        cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": bounds})
        cdp.detach()
    except Exception:  # noqa: BLE001 - placement is cosmetic
        pass


def screenshot(page, name: str) -> str:
    """Returns the file name (relative to data/screens) or '' if it failed."""
    fname = f"{time.strftime('%Y%m%d-%H%M%S')}_{name}.png".replace(":", "_")
    try:
        page.screenshot(path=str(SCREENS_DIR / fname), full_page=False, timeout=15000)
        return fname
    except Exception:  # noqa: BLE001
        return ""


def wait_for(fn, timeout: float = 10, every: float = 0.5):
    """Poll fn() until it returns something truthy."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
        time.sleep(every)
    return None

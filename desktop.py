"""omniTop as a desktop app: the same view in its own window.

    omniTop.exe          live capture (Windows asks for administrator rights)
    omniTop.exe --demo   simulated traffic
    omniTop.exe --agent  no window: lets omniTop on other PCs watch this one (see agent.py);
                         --install-agent and --uninstall-agent make it start with Windows or not

The window runs with your normal rights. Capture needs administrator rights,
so it runs in a second, elevated copy of this program that has no window of
its own and quits when the window closes. If you start the app as
administrator, capture runs in the window's own process instead.

Run from source with:  python desktop.py [--demo]   (needs pywebview)
"""
from __future__ import annotations

import argparse
import ctypes
import html
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import winreg
from ctypes import wintypes

import agent
import server

TITLE = "omniTop"
# The app was called Port traffic before; its windows count as this app's too.
TITLES = (TITLE, "Port traffic")
FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "PortTrafficViz")
ERROR_FILE = os.path.join(APP_DIR, "capture-error.txt")

SYNCHRONIZE = 0x00100000
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
ERROR_ALREADY_EXISTS = 183
ERROR_CANCELLED = 1223
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SEE_MASK_FLAG_NO_UI = 0x00000400       # errors come back to us, not in a box of the shell's
SW_HIDE, SW_SHOWNORMAL, SW_RESTORE = 0, 1, 9
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
SWP_FRAMECHANGED = 0x0020
SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0004, 0x0010
MB_ICONERROR, MB_ICONINFORMATION = 0x10, 0x40

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
shell32.ShellExecuteExW.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]

# Talk to our own local server directly, never through a configured proxy.
_local = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
# Windows helpers
# ---------------------------------------------------------------------------

def system_dark() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def app_windows(pid: int | None = None) -> list[int]:
    """Visible top-level windows of this app, optionally only those of one process."""
    found: list[int] = []

    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(256)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            user32.GetClassNameW(hwnd, cls, 256)
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if (title.value.startswith(TITLES) and cls.value.startswith("WindowsForms")
                    and (pid is None or owner.value == pid)):
                found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(visit), 0)
    return found


_mutex = None


def already_running() -> bool:
    """True if another window is open; that window is brought to the front."""
    global _mutex
    ctypes.set_last_error(0)
    _mutex = kernel32.CreateMutexW(None, False, "Local\\PortTrafficViz.Window")
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        return False
    for hwnd in app_windows():
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        break
    return True


def dark_title_bar(dark: bool) -> None:
    """Match the title bar to the Windows theme (WinForms leaves it light)."""
    value = ctypes.c_int(1 if dark else 0)
    for hwnd in app_windows(os.getpid()):
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE,
                                                   ctypes.byref(value), ctypes.sizeof(value))
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE
                            | SWP_NOZORDER | SWP_NOACTIVATE)


def local_copy(src: str, dst: str) -> str:
    """Copy src to dst unless an identical copy is already there."""
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
        return dst
    try:
        s, d = os.stat(src), os.stat(dst)
        if s.st_size == d.st_size and int(s.st_mtime) == int(d.st_mtime):
            return dst
    except OSError:
        pass
    try:
        shutil.copy2(src, dst)
    except OSError:
        if not os.path.exists(dst):  # in use by a capture helper that's still running
            raise
    return dst


def helper_command(port: int) -> tuple[str, str]:
    """Program and arguments for the elevated capture helper.

    It runs from %LOCALAPPDATA% because elevated processes can't always see
    drive letters mounted in the user's session (Google Drive, mapped drives).
    """
    os.makedirs(APP_DIR, exist_ok=True)
    args = f"--capture --port {port} --parent {os.getpid()}"
    if FROZEN:
        return local_copy(sys.executable, os.path.join(APP_DIR, os.path.basename(sys.executable))), args
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("desktop.py", "agent.py", "server.py", "sysmon.py", "perfmon.py", "netwatch.py"):
        local_copy(os.path.join(here, name), os.path.join(APP_DIR, name))
    shutil.copytree(server.WEB_DIR, os.path.join(APP_DIR, "web"), dirs_exist_ok=True)
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return exe, f'"{os.path.join(APP_DIR, "desktop.py")}" {args}'


def launch_elevated(exe: str, params: str, show: int = SW_HIDE) -> int | None:
    """Start exe as administrator. Returns its process handle, or None if the user said no."""
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.lpDirectory = APP_DIR
    sei.nShow = show
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            return None
        raise ctypes.WinError(err)
    return sei.hProcess


def wait_ready(url: str, proc: int, seconds: float) -> str:
    """'ready', 'exited' (the helper quit) or 'timeout'."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if kernel32.WaitForSingleObject(proc, 0) == WAIT_OBJECT_0:
            return "exited"
        try:
            with _local.open(url + "api/info", timeout=1):
                return "ready"
        except OSError:
            time.sleep(0.2)
    return "timeout"


def describe(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or type(exc).__name__


# ---------------------------------------------------------------------------
# Elevated capture helper (no window)
# ---------------------------------------------------------------------------

def run_capture(port: int, parent_pid: int) -> int:
    try:
        srv = server.Server("live", port, exact_port=True, owner_pid=parent_pid or None)
    except BaseException as exc:  # noqa: BLE001 - reported to the window
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(ERROR_FILE, "w", encoding="utf-8") as f:
                f.write(describe(exc))
        except OSError:
            pass
        return 1

    def watch_parent():
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid) if parent_pid else None
        if handle:
            kernel32.WaitForSingleObject(handle, INFINITE)
            kernel32.CloseHandle(handle)
        else:
            # Can't see the window's process (for example when a different
            # administrator account approved the prompt): quit once no window
            # has been connected for 30 seconds instead.
            while srv.hub.viewers() or time.monotonic() - srv.hub.last_change < 30:
                time.sleep(1)
        srv.shutdown()

    threading.Thread(target=watch_parent, name="watch-parent", daemon=True).start()
    srv.run()
    return 0


# ---------------------------------------------------------------------------
# The agent other PCs watch this one through (no window)
# ---------------------------------------------------------------------------

def console():
    """Something to print with, if this was started from a console; else None."""
    if not FROZEN:
        return print if sys.stdout else None
    if not kernel32.AttachConsole(wintypes.DWORD(-1)):      # the parent's
        return None
    try:
        out = open("CONOUT$", "w", encoding="utf-8", buffering=1)   # noqa: SIM115 - for as long as this runs
    except OSError:
        return None
    return lambda text: print(text, file=out)


def run_agent(listen: str, port: int) -> int:
    echo = console()
    if echo:
        echo("Ctrl+C, or closing this window, stops the agent.")
    return agent.run(listen, port, server.VERSION, os.path.join(APP_DIR, "agent.log"), echo)


def manage_agent(action: str, port: int) -> int:
    """Install or uninstall the agent, asking for administrator rights if need be."""
    if not FROZEN:
        raise SystemExit("Installing the agent needs omniTop.exe. From source, run: python desktop.py --agent")
    if not server.is_admin():
        os.makedirs(APP_DIR, exist_ok=True)
        # A copy on a local drive: elevated processes can't always see mapped ones.
        exe = local_copy(sys.executable, os.path.join(APP_DIR, "omniTop-setup.exe"))
        try:
            proc = launch_elevated(exe, f"--{action}-agent --agent-port {port}", SW_SHOWNORMAL)
            if proc is None:
                raise SystemExit("This needs administrator rights, and Windows didn't grant them. Nothing was changed.")
            kernel32.WaitForSingleObject(proc, INFINITE)
            code = wintypes.DWORD(1)
            kernel32.GetExitCodeProcess(proc, ctypes.byref(code))
            kernel32.CloseHandle(proc)
        finally:
            try:
                os.remove(exe)
            except OSError:
                pass
        return int(code.value)                    # it has said what went wrong, if anything did
    try:
        text = agent.install(sys.executable, port) if action == "install" else agent.uninstall()
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc) or type(exc).__name__) from None
    user32.MessageBoxW(None, text, TITLE, MB_ICONINFORMATION)
    return 0


def can_ask() -> bool:
    """Whether anyone could see a message: not the capture helper, and not a service's session."""
    return "--capture" not in sys.argv and not agent.in_services_session()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

PAGE = """<!doctype html><meta charset="utf-8"><style>
:root {{ color-scheme: light dark; --bg:#fcfcfb; --text:#0b0b0b; --muted:#6b6a64; --btn:#f0efea; --bd:rgba(11,11,11,.16) }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1a1a19; --text:#f0efec; --muted:#a3a198; --btn:#2a2a28; --bd:rgba(255,255,255,.18) }} }}
html, body {{ height: 100%; margin: 0 }}
body {{ display: grid; place-items: center; background: var(--bg); color: var(--text);
       font: 15px/1.5 "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif }}
.box {{ max-width: 520px; padding: 24px }}
h1 {{ font-size: 18px; font-weight: 600; margin: 0 0 6px }}
p {{ margin: 0 0 6px; color: var(--muted) }}
.row {{ margin-top: 16px; display: flex; gap: 8px; flex-wrap: wrap }}
button {{ font: inherit; color: inherit; background: var(--btn); border: 1px solid var(--bd);
         border-radius: 6px; padding: 6px 14px; cursor: pointer }}
button:first-child {{ font-weight: 600 }}
</style><div class="box"><h1>{title}</h1>{body}{buttons}</div>"""

BUTTONS = ('<div class="row"><button onclick="pywebview.api.try_live()">Try again</button>'
           '<button onclick="pywebview.api.show_demo()">Show simulated traffic</button></div>')

# A starting settings.ini, in CPU Graph's format, for a PC that doesn't have one
SETTINGS_TEMPLATE = """\
# Read by CPU Graph and by omniTop's CPU, disk & network tab.
# Changes to adapters, drives and scales apply within a second.

# Network adapters to graph, separated by ; - their connection names (or words from
# their description) - or auto for the connected network cards, or none. Up to 6.
networks=auto
# Axis per adapter: scale.<adapter>=link (its link speed), auto (fit to the traffic),
# or a fixed rate such as 100M, 2.5G or 125MB/s.

# Drives to graph: letters such as C:,D:, or auto for every fixed drive, or none. Up to 6.
disks=auto

# Remote machines to show, up to 6, separated by ; - Windows PCs running omniTop's agent
# (omniTop.exe --install-agent) or Linux machines running cpugraph-agent: host names or IP
# addresses, each optionally with :port (default 7820). A [section] named like the
# machine can choose its networks= and disks= (by the names that machine uses).
remotes=
"""


class App:
    def __init__(self, demo: bool) -> None:
        self.demo = demo
        self.dark = system_dark()
        self.srv: server.Server | None = None
        self.busy = threading.Lock()
        self.window = None

    def run(self) -> int:
        import webview  # only the window process needs it

        self.window = webview.create_window(
            TITLE, html=self.page("Starting…"), width=1380, height=900, min_size=(760, 560),
            background_color="#1a1a19" if self.dark else "#fcfcfb")
        self.window.expose(self.try_live, self.show_demo, self.set_on_top, self.edit_perf_settings)
        self.window.events.shown += lambda: dark_title_bar(self.dark)
        # Not private mode, so the page's settings (sample rate, hiding own
        # traffic) are still there next time.
        webview.start(self.boot, gui="edgechromium", private_mode=False,
                      storage_path=os.path.join(APP_DIR, "webview"))
        if self.srv:
            self.srv.shutdown()
        return 0

    def page(self, title: str, *lines: str, buttons: bool = False) -> str:
        body = "".join(f"<p>{html.escape(x)}</p>" for x in lines)
        return PAGE.format(title=html.escape(title), body=body, buttons=BUTTONS if buttons else "")

    def show(self, title: str, *lines: str, buttons: bool = False) -> None:
        self.window.load_html(self.page(title, *lines, buttons=buttons))

    def boot(self) -> None:
        # Let the first page finish loading, or it can land after (and hide)
        # whatever is loaded next.
        self.window.events.loaded.wait(15)
        if self.demo:
            self.start_here("demo")
        elif server.is_admin():
            self.start_here("live")
        else:
            self.start_helper()

    # Called from the page's buttons. Return at once so the page's call settles
    # before the work below navigates the window away from it.
    def try_live(self) -> None:
        threading.Thread(target=self.start_helper, daemon=True).start()

    def show_demo(self) -> None:
        threading.Thread(target=self.start_here, args=("demo",), daemon=True).start()

    # The CPU, disk & network tab's T and E keys.
    def set_on_top(self, on) -> None:
        """Keep the window above others. Called from the page on a pywebview worker thread.

        pywebview's own `window.on_top` sets the form's TopMost property straight from
        this thread. That property call keeps Python's lock while Windows waits on the
        window's thread, which needs the lock for pywebview's event handlers: a deadlock
        that freezes the window. Invoke runs the change on the window's thread instead,
        and releases the lock while it waits.
        """
        on = bool(on)
        try:
            from System import Func, Type
            from webview.platforms.winforms import BrowserView
            form = BrowserView.instances.get(self.window.uid)
        except Exception:
            form = None
        if form is None:
            return

        def apply():
            form.TopMost = on

        if form.InvokeRequired:
            form.Invoke(Func[Type](apply))
        else:
            apply()

    def edit_perf_settings(self) -> None:
        """Open CPU Graph's settings.ini (which that tab reads) in Notepad."""
        path = server.perfmon.SETTINGS_PATH
        try:
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(SETTINGS_TEMPLATE)
            subprocess.Popen(["notepad.exe", path])
        except OSError:
            pass

    def start_here(self, mode: str) -> None:
        with self.busy:
            try:
                if self.srv is None or self.srv.mode != mode:
                    if self.srv:
                        self.srv.shutdown()
                    self.srv = server.Server(mode, server.DEFAULT_PORT)
                    threading.Thread(target=self.srv.run, name="http", daemon=True).start()
                self.window.load_url(self.srv.url)
            except Exception as exc:  # noqa: BLE001 - shown in the window
                self.srv = None
                self.show("omniTop couldn't start.", describe(exc), buttons=mode == "live")

    def start_helper(self) -> None:
        if not self.busy.acquire(blocking=False):
            return
        try:
            self.show("Waiting for administrator permission…",
                      "Live capture reads Windows' own network event trace, which needs "
                      "administrator rights. Approve the Windows prompt to continue.")
            port = server.free_port(server.DEFAULT_PORT)
            try:
                os.remove(ERROR_FILE)
            except OSError:
                pass
            try:
                exe, params = helper_command(port)
                proc = launch_elevated(exe, params)
            except OSError as exc:
                self.show("Live capture couldn't start.", describe(exc), buttons=True)
                return
            if proc is None:
                self.show("Live capture needs administrator rights.",
                          "Windows didn't grant them, so nothing is being captured.", buttons=True)
                return
            self.show("Starting live capture…")
            url = f"http://127.0.0.1:{port}/"
            state = wait_ready(url, proc, 60)
            kernel32.CloseHandle(proc)
            if state == "ready":
                self.window.load_url(url)
                return
            try:
                with open(ERROR_FILE, encoding="utf-8") as f:
                    reason = f.read().strip()
            except OSError:
                reason = ""
            if state == "exited":
                self.show("Live capture couldn't start.",
                          reason or "The capture process stopped before it was ready.", buttons=True)
            else:
                self.show("Live capture didn't respond.",
                          "The capture process started but hasn't answered after a minute.", buttons=True)
        finally:
            self.busy.release()


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Live isometric view of per-port network traffic.")
    ap.add_argument("--demo", action="store_true", help="show simulated traffic")
    ap.add_argument("--capture", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=server.DEFAULT_PORT, help=argparse.SUPPRESS)
    ap.add_argument("--parent", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--agent", action="store_true", help="let omniTop on other PCs watch this one")
    ap.add_argument("--install-agent", action="store_true", help="start the agent with Windows")
    ap.add_argument("--uninstall-agent", action="store_true", help="remove the agent")
    ap.add_argument("--agent-cleanup", action="store_true", help=argparse.SUPPRESS)   # run by --uninstall-agent
    ap.add_argument("--agent-port", type=int, default=server.perfmon.AGENT_PORT, help="the agent's TCP port (default 7820)")
    ap.add_argument("--listen", default="", help="an address for --agent to listen on (default all)")
    args = ap.parse_args()

    if not 0 < args.agent_port < 65536:
        raise SystemExit("--agent-port must be between 1 and 65535.")
    if args.agent_cleanup:
        return agent.cleanup(os.path.join(APP_DIR, "agent.log"))
    if args.agent:
        return run_agent(args.listen, args.agent_port)
    if args.install_agent or args.uninstall_agent:
        return manage_agent("install" if args.install_agent else "uninstall", args.agent_port)
    server.check_layouts()
    if args.capture:
        return run_capture(args.port, args.parent)
    if already_running():
        return 0
    return App(args.demo).run()


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else 1
        if isinstance(stop.code, str) and can_ask():
            user32.MessageBoxW(None, stop.code, TITLE, MB_ICONERROR)
    except BaseException:  # noqa: BLE001 - a windowed exe has no console to print to
        if can_ask():
            user32.MessageBoxW(None, traceback.format_exc(limit=6), TITLE + " crashed", MB_ICONERROR)
        code = 1
    sys.exit(code)

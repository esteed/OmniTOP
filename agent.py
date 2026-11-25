"""omniTop's agent: lets omniTop on other PCs watch this Windows PC.

    omniTop.exe --agent              until it's closed
    omniTop.exe --install-agent      starts with Windows (asks for administrator rights)
    omniTop.exe --uninstall-agent    removes it again

It speaks CPU Graph's agent protocol (version 2), the one cpugraph-agent speaks
on Linux: plain text lines over TCP.

    agent -> viewer, once:  cpugraph-agent 2 host=<name> cpus=<n> kernel=<build> version=<v> os=windows
    agent -> viewer, once:  have net <adapter>...  and  have disk <disk>...
    viewer -> agent, any time:  interval <ms>      (10..10000; until then 1000)
    viewer -> agent, any time:  net auto | net <adapter>...  and  disk auto | disk <disk>...
    agent -> viewer, at the start and whenever the columns change:
        label net <adapter> <text>     its name as Windows shows it (this agent's addition)
        nets <adapter>:<link>...       link = Mb/s (0 = unknown), down, or missing
        label disk <disk> <text>
        about <disk> <bytes> <text>    its size and volume name
        disks <disk>[:missing]...
    agent -> viewer, each sample:
        s <ms> <all %> <cpu0 %>... <rx B/s> <tx B/s>... <read B/s> <write B/s> <busy %>...
    agent -> viewer, before closing:  err <reason>

Names in the protocol are single words, so an adapter called "Realtek 2.5GbE"
travels as Realtek_2.5GbE and drive C: as C; the labels carry the real names.

It only reads counters that need no rights, runs no commands for viewers, and
answers nothing else. Like the Linux agent, it isn't encrypted or
authenticated: anyone who can reach the port can read these numbers.
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import perfmon

PROTOCOL = 2
DEFAULT_MS, MIN_MS, MAX_MS = 1000, 10, 10000
MAX_SELECTED = 16            # adapters or disks one viewer can ask for
MAX_VIEWERS = 8
WRITE_TIMEOUT = 5.0          # a viewer that stops reading is dropped
MAX_COMMAND = 1024           # bytes in one command line
LOOK_EVERY = 3.0             # seconds between looks at which adapters there are, and missing drives
LOG_LIMIT = 256 * 1024

NAME = "omniTop agent"       # the scheduled task and firewall rule
LOCAL_SERVICE = "S-1-5-19"   # the account the installed agent runs as: few rights, no password

_WORD = re.compile(r"[A-Za-z0-9_.@!+-]{1,64}")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def word(text: str) -> str:
    """A name as one word of the protocol, or "" if it has characters that can't be in one."""
    w = re.sub(r"[^\x21-\x7e]+", "_", text) if text.isascii() else ""
    w = re.sub(r"[^A-Za-z0-9_.@!+-]", "_", w)[:64]
    return w if _WORD.fullmatch(w) and w.strip("_.") else ""


def printable(text: str, limit: int = 64) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())[:limit]


def adapter_words(found: list[dict]) -> dict[str, dict]:
    """Every adapter by the word it has in the protocol (found: perfmon.adapters()).

    Without the entries network filter drivers add for each adapter.
    """
    out: dict[str, dict] = {}
    for a in found:
        if a.get("filter"):
            continue
        base = word(a["alias"]) or f"adapter{a['luid'] & 0xFFFFFF}"
        w, n = base, 2
        while w.lower() in (k.lower() for k in out):
            w, n = f"{base[:60]}.{n}", n + 1
        out[w] = a
    return out


# ---------------------------------------------------------------------------
# Sampling: each viewer has its own, so they don't disturb each other
# ---------------------------------------------------------------------------

class Net:
    def __init__(self, name: str) -> None:
        self.word, self.label, self.alias = name, "", None
        self.reader, self.link = None, "missing"
        self.value = (0.0, 0.0)


class Disk:
    def __init__(self, letter: str) -> None:
        self.word, self.drive = letter, letter + ":"
        self.reader, self.about, self.retry = None, "", 0.0
        self.value = (0.0, 0.0, 0.0)


class Sampler:
    def __init__(self) -> None:
        try:
            self.cpu = perfmon.CpuTimes()
        except OSError:
            self.cpu = perfmon.SystemTimes()
        self.cpu.read()                       # percentages need two readings
        self.nets: list[Net] = []
        self.disks: list[Disk] = []
        self.changed = False                  # the columns, since the viewer was told
        self.look_at = time.monotonic() + LOOK_EVERY
        self.found = adapter_words(perfmon.adapters())

    def close(self) -> None:
        for d in self.disks:
            if d.reader:
                d.reader.pdh.close()
        self.disks = []

    def have_lines(self) -> str:
        return f"have net {' '.join(self.found)}".rstrip() + "\n" + \
               f"have disk {' '.join(d[0] for d in perfmon.fixed_drives(26))}".rstrip() + "\n"

    def select_nets(self, names: list[str] | None) -> None:
        self.found = adapter_words(perfmon.adapters())
        if names is None:
            auto = perfmon.auto_adapters(list(self.found.values()))
            names = [w for w, a in self.found.items() if a["alias"] in auto]
        self.nets = []
        for name in names:
            w = self._net_word(name)
            if w and len(self.nets) < MAX_SELECTED and all(w.lower() != n.word.lower() for n in self.nets):
                self.nets.append(Net(w))
        self._look_nets(perfmon.now())

    def _net_word(self, name: str) -> str:
        """The word for an adapter asked for by its word or its name (with _ for spaces)."""
        low = name.lower()
        for w, a in self.found.items():
            if low == w.lower() or low == re.sub(r"\s+", "_", a["alias"]).lower():
                return w
        return word(name)

    def _look_nets(self, t: float) -> None:
        """Open adapters that have come, and note the links of all of them."""
        for n in self.nets:
            a = next((a for w, a in self.found.items() if w.lower() == n.word.lower()), None)
            if a is None:
                n.reader = None
            elif n.reader is None or n.alias != a["alias"]:
                n.alias, n.label = a["alias"], printable(a["alias"])
                try:
                    n.reader = perfmon.NetReader(a["alias"], list(self.found.values()))
                    n.reader.read(t)          # so the next reading is already a rate
                except OSError:
                    n.reader = None
            if n.reader is None:
                self._set_link(n, "missing")
            else:
                try:
                    speed = n.reader._refresh()
                    self._set_link(n, "down" if speed < 0 else str(int(speed / 1e6)))
                except OSError:
                    n.reader = None
                    self._set_link(n, "missing")

    def _set_link(self, n: Net, link: str) -> None:
        if n.link != link:
            n.link, self.changed = link, True

    def select_disks(self, names: list[str] | None) -> None:
        auto = names is None
        if auto:
            names = perfmon.fixed_drives()
        self.close()
        for name in names:
            m = re.fullmatch(r"([A-Za-z]):?\\?", name)
            if m and len(self.disks) < MAX_SELECTED and all(m[1].upper() != d.word for d in self.disks):
                self.disks.append(Disk(m[1].upper()))
        for d in self.disks:
            self._open_disk(d)
        if auto:                              # leave out drives without counters, such as cloud storage
            self.disks = [d for d in self.disks if d.reader]

    def _open_disk(self, d: Disk) -> None:
        try:
            d.reader = perfmon.DiskReader(d.drive)
        except OSError:
            d.reader, d.retry = None, time.monotonic() + perfmon.RETRY_SECONDS
            return
        try:
            size = shutil.disk_usage(d.drive + "\\").total
        except OSError:
            size = 0
        d.about = f"{size} {printable(d.reader.detail)}".rstrip()
        self.changed = True

    def lists(self) -> str:
        """The lines that describe the sample columns."""
        self.changed = False
        out = [f"label net {n.word} {n.label}" for n in self.nets if n.label]
        out.append(" ".join(["nets"] + [f"{n.word}:{n.link}" for n in self.nets]))
        out += [f"label disk {d.word} {d.drive}" for d in self.disks]
        out += [f"about {d.word} {d.about}" for d in self.disks if d.reader]
        out.append(" ".join(["disks"] + [d.word if d.reader else d.word + ":missing" for d in self.disks]))
        return "\n".join(out) + "\n"

    def sample(self, t: float) -> str | None:
        """One "s" line, or None while there's nothing to report."""
        cpu = self.cpu.read()
        mono = time.monotonic()
        for n in self.nets:
            v = None
            if n.reader:
                try:
                    v = n.reader.read(t)
                except OSError:
                    n.reader = None
                    self._set_link(n, "missing")
            n.value = (v[0] / 8, v[1] / 8) if v else (0.0, 0.0)
        for d in self.disks:
            v = None
            if d.reader:
                try:
                    v = d.reader.read(t)
                except OSError:
                    d.reader.pdh.close()
                    d.reader, d.retry, self.changed = None, mono + perfmon.RETRY_SECONDS, True
            d.value = (v[1], v[2], v[0]) if v else (0.0, 0.0, 0.0)
        if cpu is None:
            return None
        parts = [f"s {t * 1000:.3f}"] + [f"{x:.1f}" for x in cpu[:1 + self.cpu.n]]
        for rx, tx in (n.value for n in self.nets):
            parts += (f"{rx:.0f}", f"{tx:.0f}")
        for read, write, busy in (d.value for d in self.disks):
            parts += (f"{read:.0f}", f"{write:.0f}", f"{busy:.1f}")
        return " ".join(parts) + "\n"

    def look(self, t: float) -> None:
        """Every few seconds, between samples: what came, went or changed its link."""
        mono = time.monotonic()
        if mono < self.look_at:
            return
        self.look_at = mono + LOOK_EVERY
        self.found = adapter_words(perfmon.adapters())
        self._look_nets(t)
        for d in self.disks:
            if d.reader is None and mono >= d.retry:
                self._open_disk(d)


# ---------------------------------------------------------------------------
# Serving viewers
# ---------------------------------------------------------------------------

def parse_command(line: str) -> tuple[str, object] | None:
    """("interval", ms), ("net", names or None for auto) or ("disk", ...); None for anything else."""
    f = line.split()
    if len(f) == 2 and f[0] == "interval" and re.fullmatch(r"\d{1,9}", f[1]):
        return "interval", max(MIN_MS, min(MAX_MS, int(f[1])))
    if f and f[0] in ("net", "disk"):
        return f[0], None if f[1:] == ["auto"] else f[1:1 + MAX_SELECTED]
    return None


class Viewer:
    """One connected viewer: a thread sends samples, another reads its commands."""

    def __init__(self, server: "AgentServer", sock: socket.socket, peer: str) -> None:
        self.server, self.sock, self.peer = server, sock, peer
        self.pending: dict[str, object] = {}      # the latest command of each kind
        self.lock = threading.Lock()
        self.wake = threading.Event()             # a command arrived, or the viewer left
        self.gone = False

    def send(self, text: str) -> None:
        self.sock.sendall(text.encode("utf-8"))

    def read_commands(self) -> None:
        buf = b""
        try:
            while not self.server.stopping.is_set():
                try:
                    data = self.sock.recv(4096)
                except TimeoutError:
                    continue                      # quiet, not gone
                if not data:
                    break
                buf += data
                *lines, buf = buf.split(b"\n")
                if len(buf) > MAX_COMMAND or any(len(x) > MAX_COMMAND for x in lines):
                    break
                for raw in lines:
                    c = parse_command(raw.decode("utf-8", errors="replace"))
                    if c:
                        with self.lock:
                            self.pending[c[0]] = c[1]
                        self.wake.set()
        except OSError:
            pass
        self.gone = True
        self.wake.set()

    def run(self) -> None:
        log = self.server.log
        sampler = None
        try:
            self.sock.settimeout(WRITE_TIMEOUT)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sampler = Sampler()
            self.send(self.server.hello(sampler.cpu.n) + sampler.have_lines())
            sampler.select_nets(None)
            sampler.select_disks(None)
            self.send(sampler.lists())
            log(f"{self.peer} connected")
            threading.Thread(target=self.read_commands, name=f"commands {self.peer}", daemon=True).start()
            self._stream(sampler)
            log(f"{self.peer} disconnected")
        except OSError as exc:
            log(f"{self.peer} disconnected: {exc}")
        except Exception as exc:  # noqa: BLE001 - one viewer's trouble stays its own
            log(f"{self.peer}: {type(exc).__name__}: {exc}")
        finally:
            if sampler:
                sampler.close()
            self.sock.close()

    def _stream(self, sampler: Sampler) -> None:
        step = DEFAULT_MS / 1000
        last = time.perf_counter()                # the selections just took the first readings
        due = last + step
        while not self.gone and not self.server.stopping.is_set():
            wait = due - time.perf_counter()
            if wait > 0 and not self.wake.is_set():
                time.sleep(min(wait, 0.1))        # finer than waiting on the event, whose timer ticks every 15.6 ms
                continue
            if self.wake.is_set():
                self.wake.clear()
                with self.lock:
                    todo, self.pending = self.pending, {}
                if "interval" in todo:
                    step = todo["interval"] / 1000
                    due = last + step             # a whole new interval after the last reading
                if "net" in todo:
                    sampler.select_nets(todo["net"])
                if "disk" in todo:
                    sampler.select_disks(todo["disk"])
                if "net" in todo or "disk" in todo:
                    self.send(sampler.lists())
                continue
            last = time.perf_counter()
            line = sampler.sample(perfmon.now())
            if line:
                self.send(line)
            sampler.look(perfmon.now())
            if sampler.changed:                   # the samples from here on have these columns
                self.send(sampler.lists())
            due += step                           # absolute deadlines: no drift
            if due <= time.perf_counter():        # fell behind (asleep, busy): start again from now
                due = time.perf_counter() + step


class AgentServer:
    def __init__(self, listen: str, port: int, version: str, log) -> None:
        self.version, self.log = version, log
        self.stopping = threading.Event()
        self.slots = threading.BoundedSemaphore(MAX_VIEWERS)
        if listen:
            family = socket.AF_INET6 if ":" in listen else socket.AF_INET
            self.sock = self._open(family, (listen, port), dual=False)
        else:
            try:
                self.sock = self._open(socket.AF_INET6, ("", port), dual=True)
            except OSError as exc:
                if exc.errno == 10048:            # in use
                    raise
                self.sock = self._open(socket.AF_INET, ("", port), dual=False)   # IPv6 is off here
        self.port = self.sock.getsockname()[1]

    @staticmethod
    def _open(family: int, address: tuple, dual: bool) -> socket.socket:
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)   # no other program can share the port
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if dual else 1)
            s.bind(address)
            s.listen(16)
        except OSError:
            s.close()
            raise
        return s

    def hello(self, cpus: int) -> str:
        v = sys.getwindowsversion()
        host = word(socket.gethostname()) or "unknown"
        return (f"cpugraph-agent {PROTOCOL} host={host} cpus={cpus} "
                f"kernel={v.major}.{v.minor}.{v.build} version={self.version} os=windows\n")

    def serve(self) -> None:
        """Until stop()."""
        self.sock.settimeout(0.5)                 # so Ctrl+C and stop() are noticed
        while not self.stopping.is_set():
            try:
                conn, addr = self.sock.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if self.stopping.is_set():
                    break
                self.log(f"accept failed: {exc}")
                time.sleep(0.1)
                continue
            peer = addr[0].removeprefix("::ffff:")
            if not self.slots.acquire(blocking=False):
                try:
                    conn.settimeout(1)
                    conn.sendall(b"err too many viewers\n")
                except OSError:
                    pass
                conn.close()
                self.log(f"{peer} refused: already serving {MAX_VIEWERS} viewers")
                continue
            threading.Thread(target=self._serve_one, args=(conn, peer), name=f"viewer {peer}", daemon=True).start()
        self.sock.close()

    def _serve_one(self, conn: socket.socket, peer: str) -> None:
        try:
            Viewer(self, conn, peer).run()
        finally:
            self.slots.release()

    def stop(self) -> None:
        self.stopping.set()


class Log:
    """A short log file, started over when it gets long."""

    def __init__(self, path: str, echo=None) -> None:
        self.path, self.echo, self.lock = path, echo, threading.Lock()

    def __call__(self, text: str) -> None:
        if self.echo:
            self.echo(text)
        try:
            with self.lock:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                long = os.path.exists(self.path) and os.path.getsize(self.path) > LOG_LIMIT
                with open(self.path, "w" if long else "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            pass


def in_services_session() -> bool:
    """Whether this runs where Windows runs services, with no one there to see a message."""
    session = wintypes.DWORD()
    ctypes.WinDLL("kernel32").ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    return session.value == 0


def _empty(folder: str, keep: str) -> bool:
    """Delete what's in a folder except the file `keep`. Links are removed, never followed. True if that's all that's left."""
    clear = True
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return False
    for e in entries:
        if os.path.normcase(e.path) == os.path.normcase(keep):
            continue
        try:
            if e.is_junction() or (e.is_symlink() and e.is_dir()):
                os.rmdir(e.path)                  # the link itself
            elif e.is_dir(follow_symlinks=False):
                done = _empty(e.path, keep)
                clear = clear and done
                if done and not keep.lower().startswith(e.path.lower() + os.sep):
                    os.rmdir(e.path)
            else:
                os.unlink(e.path)
        except OSError:
            clear = False                         # still in use
    return clear


def sweep(temp: str, keep: str = "", seconds: float = 0.0) -> int:
    """Delete copies of this program left in a temp folder; returns how many.

    The exe unpacks itself there to run, and deletes that when it ends, but
    not when it's ended from outside, or by Windows shutting down. The file
    that tells a copy of this program goes last, so one that was still partly
    in use after `seconds` is known for what it is next time. Only the account
    a copy was unpacked by can see into it, administrators included.
    """
    gone = 0
    deadline = time.monotonic() + seconds
    try:
        names = os.listdir(temp)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(temp, name)
        mark = os.path.join(path, "web", "dimm.js")
        if (not name.startswith("_MEI") or os.path.normcase(path) == os.path.normcase(keep)
                or os.path.islink(path) or os.path.isjunction(path) or not os.path.isfile(mark)):
            continue
        while not _empty(path, mark) and time.monotonic() < deadline:
            time.sleep(0.25)
        if _empty(path, mark):
            try:
                os.unlink(mark)
                os.rmdir(os.path.dirname(mark))
                os.rmdir(path)
                gone += 1
            except OSError:
                pass
    return gone


def cleanup(log_path: str) -> int:
    """Run as the installed agent's account when it's being removed: delete what it left in its folders."""
    unpacked = getattr(sys, "_MEIPASS", "")
    if not unpacked or not in_services_session():
        return 0
    sweep(os.path.dirname(unpacked), unpacked, seconds=10)   # this copy deletes itself when it ends
    try:
        os.remove(log_path)
        os.rmdir(os.path.dirname(log_path))       # only if that was all
    except OSError:
        pass
    return 0


def run(listen: str, port: int, version: str, log_path: str, echo=None) -> int:
    log = Log(log_path, echo)
    unpacked = getattr(sys, "_MEIPASS", "")
    if unpacked and in_services_session():        # only the installed agent runs there: the others are stale
        sweep(os.path.dirname(unpacked), unpacked)
    try:
        srv = AgentServer(listen, port, version, log)
    except OSError as exc:
        why = "another program is using it" if exc.errno == 10048 else (exc.strerror or str(exc))
        log(f"can't listen on port {port}: {why}")
        raise SystemExit(f"The agent can't listen on TCP port {port}: {why}.") from None
    log(f"omniTop agent {version} listening on {listen or 'all addresses'}, TCP port {srv.port}")
    try:
        _k32 = ctypes.WinDLL("kernel32")
        _k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        _k32.GetCurrentProcess.restype = wintypes.HANDLE
        _k32.SetPriorityClass(_k32.GetCurrentProcess(), 0x00008000)   # above normal: samples on time
        srv.serve()
    except KeyboardInterrupt:
        srv.stop()
    return 0


# ---------------------------------------------------------------------------
# Starting with Windows: a scheduled task and a firewall rule (administrators)
# ---------------------------------------------------------------------------

class GUID(ctypes.Structure):
    _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]


def program_files() -> str:
    """Program Files, asked of Windows rather than taken from the environment."""
    fid = GUID(0x905E63B6, 0xC1BF, 0x494E, (ctypes.c_ubyte * 8)(0xB2, 0x9C, 0x65, 0xB7, 0x32, 0xD3, 0xD2, 0x1A))
    shell = ctypes.WinDLL("shell32")
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                           ctypes.POINTER(ctypes.c_void_p)]
    out = ctypes.c_void_p()
    if shell.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out)):
        raise OSError("Windows didn't say where Program Files is.")
    try:
        return ctypes.wstring_at(out.value)
    finally:
        ole = ctypes.WinDLL("ole32")
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole.CoTaskMemFree(out)


def system_tool(name: str) -> str:
    buf = ctypes.create_unicode_buffer(260)
    ctypes.WinDLL("kernel32").GetSystemDirectoryW(buf, 260)
    return os.path.join(buf.value, name)


def tool(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([system_tool(name), *args], capture_output=True, text=True, errors="replace",
                          stdin=subprocess.DEVNULL, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)


def check(p: subprocess.CompletedProcess, what: str) -> None:
    if p.returncode:
        raise OSError(f"{what}: {(p.stderr or p.stdout or '').strip() or f'error {p.returncode}'}")


def install_dir() -> str:
    return os.path.join(program_files(), "omniTop agent")


def installed_exe() -> str:
    return os.path.join(install_dir(), "omniTop.exe")


def xml_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def task_xml(exe: str, port: int, arguments: str = "") -> str:
    """The scheduled task: from start-up, as LOCAL SERVICE, for as long as Windows runs."""
    arguments = arguments or f"--agent --agent-port {port}"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Lets omniTop on other PCs watch this PC's CPU, network and disk activity (TCP port {port}).</Description>
  </RegistrationInfo>
  <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
  <Principals>
    <Principal id="Author"><UserId>{LOCAL_SERVICE}</UserId><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{xml_text(exe)}</Command><Arguments>{arguments}</Arguments></Exec>
  </Actions>
</Task>
"""


def remove_rules() -> None:
    tool("netsh.exe", "advfirewall", "firewall", "delete", "rule", f"name={NAME}")


def installed_processes(end: bool = False) -> int:
    """How many processes run from the installed copy; with end, they're ended."""
    path = os.path.normcase(installed_exe())
    was = ctypes.c_ubyte()                        # another account's processes: needs the debug privilege
    ctypes.WinDLL("ntdll").RtlAdjustPrivilege(20, 1, 0, ctypes.byref(was))
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi = ctypes.WinDLL("psapi")
    psapi.EnumProcesses.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    ids, got = (wintypes.DWORD * 16384)(), wintypes.DWORD()
    if not psapi.EnumProcesses(ids, ctypes.sizeof(ids), ctypes.byref(got)):
        return 0
    count = 0
    for pid in ids[:got.value // 4]:
        h = k32.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, pid) if pid != os.getpid() else None   # query, end, wait
        if not h:
            continue
        try:
            buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) and os.path.normcase(buf.value) == path:
                count += 1
                if end:
                    k32.TerminateProcess(h, 0)
                    k32.WaitForSingleObject(h, 5000)
        finally:
            k32.CloseHandle(h)
    return count


def wait_for(test, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not test():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def stop_installed() -> None:
    """End the installed agent: its task, then any process of its still running."""
    tool("schtasks.exe", "/End", "/TN", NAME)
    installed_processes(end=True)


def register(exe: str, port: int, arguments: str = "") -> None:
    """Create the scheduled task, or change it."""
    xml = os.path.join(install_dir(), "task.xml")   # only administrators can write here
    try:
        with open(xml, "w", encoding="utf-16") as f:
            f.write(task_xml(exe, port, arguments))
        check(tool("schtasks.exe", "/Create", "/TN", NAME, "/XML", xml, "/F"), "Couldn't create the scheduled task")
    finally:
        try:
            os.remove(xml)
        except OSError:
            pass


def answers(port: int, seconds: float) -> str:
    """The hello line the agent on this PC answers with, or ""."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
                line = s.makefile("rb").readline(512).decode("utf-8", errors="replace").strip()
                if line.startswith("cpugraph-agent "):
                    return line
        except OSError:
            pass
        time.sleep(0.5)
    return ""


def local_addresses() -> list[str]:
    try:
        found = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        return []
    return sorted(a for a in found if not a.startswith(("127.", "169.254.")))


def install(source: str, port: int) -> str:
    """Copy the program to Program Files, open its port, and start it with Windows. Returns what to tell the user."""
    folder, exe = install_dir(), installed_exe()
    stop_installed()
    os.makedirs(folder, exist_ok=True)
    if os.path.normcase(os.path.abspath(source)) != os.path.normcase(exe):
        for _ in range(20):                       # a process that just ended can hold its file a moment
            try:
                shutil.copyfile(source, exe)
                break
            except PermissionError:
                time.sleep(0.25)
        else:
            raise OSError(f"Couldn't replace {exe}: it's in use.")
    register(exe, port)
    remove_rules()
    check(tool("netsh.exe", "advfirewall", "firewall", "add", "rule", f"name={NAME}", "dir=in", "action=allow",
               f"program={exe}", "protocol=TCP", f"localport={port}", "profile=private,domain"),
          "Couldn't open the port in Windows Firewall")
    check(tool("schtasks.exe", "/Run", "/TN", NAME), "Couldn't start the agent")
    if not answers(port, 30):
        raise OSError(f"The agent was installed, but it isn't answering on TCP port {port}.")
    name = socket.gethostname()
    suffix = "" if port == perfmon.AGENT_PORT else f":{port}"
    where = " or ".join([name + suffix] + [a + suffix for a in local_addresses()[:3]])
    return (f"omniTop on other PCs can now watch this PC. The agent starts with Windows and needs no one signed in.\n\n"
            f"On the other PC, press E on omniTop's CPU, disk & network tab and add this to remotes=\n{where}\n\n"
            f"TCP port {port} is open on private and domain networks. To remove the agent, run\n"
            f"omniTop.exe --uninstall-agent")


def uninstall() -> str:
    if os.path.normcase(sys.executable) == os.path.normcase(installed_exe()):
        raise OSError("The installed agent can't remove itself. Run this with your own copy of omniTop.exe.")
    folder, exe = install_dir(), installed_exe()
    remove_rules()
    stop_installed()
    if os.path.exists(exe):                       # its account clears up what it left in its own folders
        try:
            register(exe, 0, "--agent-cleanup")
            check(tool("schtasks.exe", "/Run", "/TN", NAME), "")
            if wait_for(lambda: installed_processes() > 0, 10):
                wait_for(lambda: installed_processes() == 0, 30)
        except (OSError, subprocess.SubprocessError):
            pass                                  # what's left is a log and some temporary files
        stop_installed()
    tool("schtasks.exe", "/Delete", "/TN", NAME, "/F")
    for _ in range(20):
        try:
            if os.path.exists(exe):
                os.remove(exe)
            break
        except PermissionError:
            time.sleep(0.25)
    try:
        os.rmdir(folder)
    except OSError:
        pass
    left = [what for what, there in (("its program", os.path.exists(exe)),
                                     ("its scheduled task", not tool("schtasks.exe", "/Query", "/TN", NAME).returncode)) if there]
    if left:
        raise OSError(f"Couldn't remove {' and '.join(left)}.")
    return "The agent is removed: other PCs can't watch this one any more, and its port is closed."

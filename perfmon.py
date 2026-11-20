"""CPU, network and disk sampling for the CPU, disk & network tab.

A port of CPU Graph 2.0 (github.com/esteed/CpuGraph) into this app:

- CPU: every logical processor's busy time from one NtQuerySystemInformation
  call; the total can instead come from "% Processor Utility", the
  frequency-aware counter Task Manager shows.
- Network: one GetIfEntry2 call per adapter returns its 64-bit byte counters
  together with the live link state and speed.
- Disks: the LogicalDisk performance counters (busy time, read and write bytes).
- Linux machines: cpugraph-agent (protocol 1 or 2) streams timestamped samples
  over TCP; they're mapped onto this app's clock so all rows line up.

Which adapters, drives and Linux machines to show comes from CPU Graph's own
settings file, %LOCALAPPDATA%\\CpuGraph\\settings.ini, so both apps agree. It's
only read here, never written. Without it: the connected network cards, every
fixed drive, and no Linux machines.

Samples go out as "perf" server-sent events, one per tick; "perfmeta" events
describe the machines and their adapters and disks.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import random
import socket
import struct
import threading
import time
from ctypes import wintypes

from sysmon import PDH_FMT_COUNTERVALUE

INTERVALS = (50, 100, 250, 500, 1000)    # ms between samples: 20, 10, 4, 2, 1 a second
MAX_ITEMS = 6                            # adapters, drives and Linux machines, each
AGENT_PORT = 7820
RETRY_SECONDS = 5
SETTINGS_PATH = os.path.join(os.environ.get("LOCALAPPDATA") or "", "CpuGraph", "settings.ini")

_T0 = time.perf_counter()


def now() -> float:
    """Seconds on the clock every sample, local or remote, is placed on."""
    return time.perf_counter() - _T0


# ---------------------------------------------------------------------------
# settings.ini (CPU Graph's): which adapters, drives and machines to show
# ---------------------------------------------------------------------------

class Choice:
    """What to graph on one machine. networks/disks: None = auto, [] = none."""

    def __init__(self) -> None:
        self.networks: list[str] | None = None
        self.disks: list[str] | None = None
        self.scales: dict[str, str] = {}

    def key(self) -> tuple:
        return (self.networks, self.disks)


def _names(value: str, seps: str, limit: int = MAX_ITEMS) -> list[str] | None:
    v = value.strip()
    if v.lower() == "auto":
        return None
    if v.lower() == "none":
        return []
    for s in seps[1:]:
        v = v.replace(s, seps[0])
    out: list[str] = []
    for part in v.split(seps[0]):
        name = part.strip()
        if name and len(name) <= 255 and name.lower() not in (x.lower() for x in out) and len(out) < limit:
            out.append(name)
    return out


def _drives(value: str) -> list[str] | None:
    names = _names(value, ",; ")
    if names is None:
        return None
    out = []
    for n in names:
        c = n[0].upper()
        if "A" <= c <= "Z" and f"{c}:" not in out:
            out.append(f"{c}:")
    return out


def read_settings(path: str = SETTINGS_PATH):
    """(this PC's Choice, [remote targets], {section name lower: Choice})."""
    local, remotes, sections = Choice(), [], {}
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return local, remotes, sections
    current = local
    for raw in lines:
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip().lower(), Choice())
            continue
        key, eq, val = line.partition("=")
        if not eq:
            continue
        key, val = key.strip(), val.strip()
        low = key.lower()
        if low.startswith("scale."):
            current.scales[key[6:].strip().lower()] = val.split("#", 1)[0].strip()
        elif low == "networks":
            current.networks = _names(val, ";")
        elif low == "disks":
            current.disks = _drives(val) if current is local else _names(val, ";, ")
        elif low == "remotes" and current is local:
            remotes = _names(val, "; ,\t") or []
    return local, remotes, sections


def parse_scale(text: str | None) -> tuple[dict, str | None]:
    """CPU Graph's network scale: link, auto, or a rate like 100M, 2.5G, 125MB/s."""
    t = (text or "").strip()
    if not t or t.lower() == "link":
        return {"mode": "link"}, None
    if t.lower() == "auto":
        return {"mode": "auto"}, None
    i = 0
    while i < len(t) and (t[i].isdigit() or t[i] == "."):
        i += 1
    try:
        number = float(t[:i])
    except ValueError:
        number = None
    unit = t[i:].strip()
    mult = 1.0
    if number is not None and unit and unit[0].lower() in "kmgt":
        mult = 1000.0 ** ("kmgt".index(unit[0].lower()) + 1)
        unit = unit[1:].strip()
    low = unit.lower()
    if low in ("byte", "bytes", "byte/s", "bytes/s"):
        is_bytes = True
    elif low in ("", "b", "bps", "b/s", "bit", "bits", "bit/s", "bits/s"):
        is_bytes = bool(unit) and unit[0] == "B" and not low.startswith("bit")
    else:
        number = None
    if number is not None:
        bits = number * mult * (8 if is_bytes else 1)
        if 1 <= bits <= 1e15:
            return {"mode": "fixed", "bps": bits}, None
    return {"mode": "link"}, f'scale "{t}" not understood – using the link speed'


# ---------------------------------------------------------------------------
# Readers for this PC
# ---------------------------------------------------------------------------

_nt = ctypes.WinDLL("ntdll")
_nt.NtQuerySystemInformation.argtypes = [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
_nt.NtQuerySystemInformation.restype = ctypes.c_long
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
_k32.GetLogicalDrives.restype = wintypes.DWORD
_k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_k32.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
_k32.SetErrorMode.argtypes = [wintypes.UINT]
_k32.GetCurrentThread.restype = wintypes.HANDLE
_k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]


class CpuTimes:
    """Busy % for all CPUs and each logical processor (SystemProcessorPerformanceInformation)."""

    ENTRY, MAX = 48, 64

    def __init__(self) -> None:
        self.buf = ctypes.create_string_buffer(self.ENTRY * self.MAX)
        ret = wintypes.ULONG()
        if _nt.NtQuerySystemInformation(8, self.buf, len(self.buf), ctypes.byref(ret)):
            raise OSError("per-processor times unavailable")
        self.n = min(self.MAX, ret.value // self.ENTRY)
        self.last: list[tuple[int, int]] | None = None

    def read(self) -> list[float] | None:
        ret = wintypes.ULONG()
        if _nt.NtQuerySystemInformation(8, self.buf, len(self.buf), ctypes.byref(ret)):
            return None
        cur = []
        for i in range(self.n):
            idle, kernel, user = struct.unpack_from("<qqq", self.buf, i * self.ENTRY)
            total = kernel + user                       # kernel time includes idle time
            cur.append((total - idle, total))
        last, self.last = self.last, cur
        if last is None:
            return None
        out, sb, st = [0.0], 0, 0
        for (b, t), (lb, lt) in zip(cur, last):
            dt = t - lt
            db = max(0, min(dt, b - lb))
            if dt <= 0:
                db = dt = 0
            out.append(100.0 * db / dt if dt else 0.0)
            sb, st = sb + db, st + dt
        if st <= 0:
            return None
        out[0] = 100.0 * sb / st
        return out


class SystemTimes:
    """All CPUs only, if per-processor times aren't available."""

    n = 0

    def __init__(self) -> None:
        self.last = None

    def read(self) -> list[float] | None:
        t = [wintypes.FILETIME() for _ in range(3)]
        if not _k32.GetSystemTimes(*(ctypes.byref(x) for x in t)):
            return None
        idle, kernel, user = ((x.dwHighDateTime << 32) | x.dwLowDateTime for x in t)
        cur, last = (idle, kernel + user), self.last
        self.last = cur
        if last is None:
            return None
        di, dt = cur[0] - last[0], cur[1] - last[1]
        return [max(0.0, 100.0 * (1 - di / dt))] if dt > 0 and di >= 0 else None


class Pdh:
    """A few PDH counters read together (PdhGetFormattedCounterValue)."""

    _dll = None
    FMT = 0x00000200 | 0x00008000             # PDH_FMT_DOUBLE | PDH_FMT_NOCAP100

    def __init__(self, paths: list[str]) -> None:
        if Pdh._dll is None:
            d = ctypes.WinDLL("pdh")
            d.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
            d.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t,
                                                ctypes.POINTER(ctypes.c_void_p)]
            d.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            d.PdhGetFormattedCounterValue.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                                      ctypes.POINTER(PDH_FMT_COUNTERVALUE)]
            d.PdhCloseQuery.argtypes = [ctypes.c_void_p]
            for f in (d.PdhOpenQueryW, d.PdhAddEnglishCounterW, d.PdhCollectQueryData,
                      d.PdhGetFormattedCounterValue, d.PdhCloseQuery):
                f.restype = wintypes.DWORD
            Pdh._dll = d
        self.q = ctypes.c_void_p()
        rc = self._dll.PdhOpenQueryW(None, 0, ctypes.byref(self.q))
        self.counters = []
        for p in paths:
            if rc:
                break
            h = ctypes.c_void_p()
            rc = self._dll.PdhAddEnglishCounterW(self.q, p, 0, ctypes.byref(h))
            self.counters.append(h)
        if not rc:
            rc = self._dll.PdhCollectQueryData(self.q)   # rates need two collections
        if rc:
            self.close()
            raise OSError(f"0x{rc:08X}")

    def read(self) -> list[float] | None:
        if not self.q or self._dll.PdhCollectQueryData(self.q):
            return None
        out = []
        for h in self.counters:
            v = PDH_FMT_COUNTERVALUE()
            if self._dll.PdhGetFormattedCounterValue(h, self.FMT, None, ctypes.byref(v)) or v.CStatus not in (0, 1):
                return None
            out.append(v.u.doubleValue)
        return out

    def close(self) -> None:
        if self.q:
            self._dll.PdhCloseQuery(self.q)
            self.q = ctypes.c_void_p()


# MIB_IF_ROW2 (1352 bytes, the same on x86 and x64)
ROW = 1352
OFF_ALIAS, OFF_DESC, OFF_TYPE, OFF_FLAGS, OFF_OPER = 28, 542, 1128, 1152, 1156
OFF_TXLINK, OFF_RXLINK, OFF_IN, OFF_OUT = 1192, 1200, 1208, 1280
_ip = ctypes.WinDLL("iphlpapi")
_ip.GetIfEntry2.argtypes = [ctypes.c_void_p]
_ip.GetIfEntry2.restype = wintypes.ULONG
_ip.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
_ip.GetIfTable2.restype = wintypes.ULONG
_ip.FreeMibTable.argtypes = [ctypes.c_void_p]


def _wstr(row: bytes, off: int) -> str:
    end = off
    while end < off + 514 and row[end:end + 2] != b"\0\0":
        end += 2
    return row[off:end].decode("utf-16-le", errors="replace")


def adapters() -> list[dict]:
    """Network adapters except loopback and tunnels, by connection name."""
    table = ctypes.c_void_p()
    if _ip.GetIfTable2(ctypes.byref(table)):
        return []
    try:
        count = ctypes.c_ulong.from_address(table.value).value
        out = []
        for i in range(count):
            row = ctypes.string_at(table.value + 8 + i * ROW, ROW)
            kind = struct.unpack_from("<I", row, OFF_TYPE)[0]
            if kind in (24, 131):                      # loopback, tunnel
                continue
            out.append({"luid": struct.unpack_from("<Q", row, 0)[0], "alias": _wstr(row, OFF_ALIAS),
                        "desc": _wstr(row, OFF_DESC), "type": kind,
                        "card": row[OFF_FLAGS] & 0x5 == 0x5,   # hardware interface with a connector
                        "up": struct.unpack_from("<i", row, OFF_OPER)[0] == 1})
        out.sort(key=lambda a: a["alias"].lower())
        return out
    finally:
        _ip.FreeMibTable(table)


def auto_adapters(found: list[dict]) -> list[str]:
    """The connected network cards; failing that, any connected Ethernet or Wi-Fi adapter."""
    pick = [a["alias"] for a in found if a["up"] and a["card"]][:MAX_ITEMS]
    return pick or [a["alias"] for a in found if a["up"] and a["type"] in (6, 71)][:MAX_ITEMS]


def link_text(link: float) -> str:
    if link < 0:
        return "disconnected"
    if link == 0:
        return ""
    return f"{link / 1e9:.3g} Gb/s link" if link >= 1e9 else f"{link / 1e6:.4g} Mb/s link"


class NetReader:
    """Received and sent bits/s and the link speed of one adapter."""

    def __init__(self, name: str, found: list[dict]) -> None:
        match = next((a for a in found if a["alias"].lower() == name.lower()), None)
        if match is None:
            words = name.lower().split()
            for a in found:
                if words and all(w in a["desc"].lower() for w in words) and (match is None or a["up"]):
                    match = a
        if match is None:
            raise OSError(f'Network adapter "{name}" not found')
        self.luid = match["luid"]
        self.buf = ctypes.create_string_buffer(ROW + 512)
        self.last = None
        self._refresh()
        if _wstr(self.buf.raw, OFF_ALIAS) != match["alias"]:   # the row layout must be right
            raise OSError(f'Network adapter "{name}" returned unexpected data')

    def _refresh(self) -> float:
        struct.pack_into("<Q", self.buf, 0, self.luid)
        rc = _ip.GetIfEntry2(self.buf)
        if rc:
            raise OSError("adapter removed" if rc in (2, 1168) else f"error {rc}")
        if struct.unpack_from("<i", self.buf, OFF_OPER)[0] != 1:
            return -1.0
        speed = max(struct.unpack_from("<q", self.buf, OFF_TXLINK)[0], struct.unpack_from("<q", self.buf, OFF_RXLINK)[0])
        return float(speed) if speed > 0 else 0.0               # ULONG64 max ("unknown") reads as -1

    def read(self, t: float) -> list[float] | None:
        link = self._refresh()
        rx, tx = struct.unpack_from("<Q", self.buf, OFF_IN)[0], struct.unpack_from("<Q", self.buf, OFF_OUT)[0]
        last, self.last = self.last, (t, rx, tx)
        if not last or t <= last[0] or rx < last[1] or tx < last[2]:   # counters reset: start again
            return None
        dt = t - last[0]
        return [(rx - last[1]) * 8 / dt, (tx - last[2]) * 8 / dt, link]


def fixed_drives(limit: int = MAX_ITEMS) -> list[str]:
    mask, out = _k32.GetLogicalDrives(), []
    for i in range(26):
        if mask >> i & 1 and len(out) < limit:
            d = f"{chr(65 + i)}:"
            if _k32.GetDriveTypeW(d + "\\") == 3 and volume_label(d) is not None:   # DRIVE_FIXED and ready
                out.append(d)
    return out


def volume_label(drive: str) -> str | None:
    buf = ctypes.create_unicode_buffer(261)
    old = _k32.SetErrorMode(1)                           # no "insert a disk" dialogs
    try:
        ok = _k32.GetVolumeInformationW(drive + "\\", buf, 261, None, None, None, None, 0)
    finally:
        _k32.SetErrorMode(old)
    return buf.value if ok else None


class DiskReader:
    """Busy %, read and write bytes/s of one drive letter (LogicalDisk counters)."""

    def __init__(self, drive: str) -> None:
        label = volume_label(drive)
        if label is None:
            raise OSError(f"Drive {drive} not available")
        self.detail = label
        p = f"\\LogicalDisk({drive})\\"
        try:
            self.pdh = Pdh([p + "% Idle Time", p + "Disk Read Bytes/sec", p + "Disk Write Bytes/sec"])
        except OSError as exc:
            raise OSError(f"Disk counters for {drive} unavailable ({exc})") from None
        self.failures = 0

    def read(self, t: float) -> list[float] | None:
        v = self.pdh.read()
        if v is None:
            self.failures += 1
            if self.failures > 40:
                raise OSError("no data from the disk counters")
            return None
        self.failures = 0
        return [max(0.0, min(100.0, 100 - v[0])), max(0.0, v[1]), max(0.0, v[2])]


# ---------------------------------------------------------------------------
# Machines: what each row shows, and how the view learns about it
# ---------------------------------------------------------------------------

class Item:
    """One adapter or disk on a machine."""

    def __init__(self, name: str) -> None:
        self.name, self.detail, self.status = name, "", ""
        self.scale, self.problem = {"mode": "link"}, None
        self.reader = None
        self.retry = 0.0


class Machine:
    def __init__(self, mid: str, name: str) -> None:
        self.id, self.name = mid, name
        self.detail = self.status = self.note = ""
        self.threads = 0
        self.nets: list[Item] = []
        self.disks: list[Item] = []
        self.version = 0          # bumped whenever the shape of the samples changes

    def meta(self) -> dict:
        return {"id": self.id, "name": self.name, "detail": self.detail, "status": self.status, "note": self.note,
                "threads": self.threads, "v": self.version,
                "nets": [{"name": i.name, "detail": i.detail, "status": i.status, "scale": i.scale,
                          "problem": i.problem} for i in self.nets],
                "disks": [{"name": i.name, "detail": i.detail, "status": i.status} for i in self.disks]}


def threads_text(n: int) -> str:
    return f"{n} thread" if n == 1 else f"{n} threads"


def size_text(b: float) -> str:
    if b >= 1e12:
        return f"{b / 1e12:.1f}".rstrip("0").rstrip(".") + " TB"
    return f"{b / 1e9:.0f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


# ---------------------------------------------------------------------------
# Linux machines running cpugraph-agent
# ---------------------------------------------------------------------------

class AgentError(Exception):
    pass


def parse_endpoint(target: str) -> tuple[str, int]:
    """host, host:port, [v6addr] or [v6addr]:port."""
    host, port_text = target.strip(), None
    if host.startswith("["):
        close = host.find("]")
        if close > 0:
            rest, host = host[close + 1:], host[1:close]
            if rest.startswith(":"):
                port_text = rest[1:]
    elif host.count(":") == 1:
        host, port_text = host.split(":")
    port = int(port_text) if port_text and port_text.isdigit() and 0 < int(port_text) < 65536 else AGENT_PORT
    return host, port


class RemoteAgent:
    """Streams one Linux machine's samples. Its own thread connects, reads and reconnects."""

    DRIFT = 200e-6           # real clocks differ by far less than 200 ppm

    def __init__(self, target: str, monitor: "PerfMonitor") -> None:
        self.target, self.monitor = target, monitor
        self.host, self.port = parse_endpoint(target)
        self.machine = Machine("r:" + target.lower(), self.host)
        self.machine.status = "Connecting…"
        self._stop = threading.Event()
        self._sock = None
        self._lock = threading.Lock()
        self.reselect = False
        self.agent_host = None
        self.have_nets: list[str] = []
        self.have_disks: list[str] = []
        self.links: list[float] = []
        self.about: dict[str, str] = {}

    @property
    def endpoint(self) -> str:
        if self.port == AGENT_PORT:
            return self.host
        return (f"[{self.host}]" if ":" in self.host else self.host) + f":{self.port}"

    def start(self) -> None:
        threading.Thread(target=self._run, name=f"agent {self.target}", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def _close(self) -> None:
        with self._lock:
            s, self._sock = self._sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.monitor.active():
                self._stop.wait(1)
                continue
            try:
                self._session()
                continue                                   # ended because nobody is watching
            except Exception as exc:  # noqa: BLE001 - shown on the row
                if self._stop.is_set():
                    return
                self.machine.status = f"{self._describe(exc)} – retrying every {RETRY_SECONDS} s"
                self.monitor.meta_changed()
            self._stop.wait(RETRY_SECONDS)

    def _describe(self, exc: Exception) -> str:
        if isinstance(exc, AgentError):
            return str(exc)
        if isinstance(exc, socket.gaierror):
            return f"Unknown host {self.host}"
        if isinstance(exc, ConnectionRefusedError):
            return f"Agent not running on {self.endpoint}"
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return f"No answer from {self.endpoint}" if not getattr(self, "_connected", False) \
                else f"The agent on {self.endpoint} stopped sending"
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return f"Lost the connection to {self.endpoint}"
        if isinstance(exc, OSError):
            return f"Can't reach {self.host}" if exc.errno in (10051, 10065) else f"Network error with {self.endpoint}"
        return str(exc) or type(exc).__name__

    def _send(self, sock, text: str) -> None:
        sock.sendall(text.encode("ascii", errors="replace"))

    def _selection(self) -> str:
        c = self.monitor.choice_for_remote(self.target, self.agent_host)
        return (f"net {'auto' if c.networks is None else ' '.join(c.networks)}\n"
                f"disk {'auto' if c.disks is None else ' '.join(c.disks)}\n")

    def _session(self) -> None:
        self._connected = False
        infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        infos.sort(key=lambda i: i[0] != socket.AF_INET)   # IPv4 first
        if not infos:
            raise AgentError(f"Unknown host {self.host}")
        sock = socket.socket(infos[0][0], socket.SOCK_STREAM)
        with self._lock:
            if self._stop.is_set():
                sock.close()
                return
            self._sock = sock
        try:
            sock.settimeout(3)
            try:
                sock.connect(infos[0][4])
            except socket.timeout:
                raise AgentError(f"No answer from {self.endpoint}") from None
            self._connected = True
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sent = self.monitor.interval
            sock.settimeout(max(5.0, sent * 3 / 1000 + 2))
            f = sock.makefile("rb")
            protocol = self._hello(f.readline())
            self._send(sock, f"interval {sent}\n")
            selection = None
            if protocol >= 2:
                selection = self._selection()
                self._send(sock, selection)
            m = self.machine
            m.status = ""
            m.version += 1                                 # new connection, new clock offset: start afresh
            self.offset = None
            self.last_mapped = float("-inf")
            self.monitor.meta_changed()
            idle_since = None
            while not self._stop.is_set():
                if not self.monitor.active():              # nobody watching for a while: hang up
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since > 10:
                        m.status = "Connecting…"
                        return
                else:
                    idle_since = None
                want = self.monitor.interval
                if want != sent:
                    self._send(sock, f"interval {want}\n")
                    sent = want
                    sock.settimeout(max(5.0, want * 3 / 1000 + 2))
                if self.reselect and protocol >= 2:
                    self.reselect = False
                    text = self._selection()
                    if text != selection:
                        self._send(sock, text)
                        selection = text
                raw = f.readline()
                arrival = now()
                if not raw:
                    raise AgentError(f"The agent on {self.endpoint} closed the connection")
                line = raw.decode("ascii", errors="replace").rstrip("\r\n")
                if line.startswith("s "):
                    self._sample(line, arrival)
                elif line.startswith("cpu "):
                    self._total_only(line, arrival)
                elif line == "nets" or line.startswith("nets "):
                    self._nets(line)
                elif line == "disks" or line.startswith("disks "):
                    self._disks(line)
                elif line.startswith("about "):
                    self._about(line)
                elif line.startswith("have "):
                    parts = line.split()
                    if len(parts) >= 2:
                        if parts[1] == "net":
                            self.have_nets = parts[2:]
                        elif parts[1] == "disk":
                            self.have_disks = parts[2:]
                elif line.startswith("err "):
                    raise AgentError(f"Agent on {self.endpoint}: {line[4:]}")
                # anything else is from a newer agent: ignore it
        finally:
            self._close()

    # "cpugraph-agent 2 host=tinker9 cpus=2 kernel=5.15.0 version=2.0.0"
    def _hello(self, raw: bytes) -> int:
        if not raw:
            raise AgentError(f"The agent on {self.endpoint} closed the connection")
        line = raw.decode("ascii", errors="replace").strip()
        if line.startswith("err "):
            raise AgentError(f"Agent on {self.endpoint}: {line[4:]}")
        parts = line.split(" ")
        if len(parts) < 2 or parts[0] != "cpugraph-agent":
            raise AgentError(f"No CPU Graph agent at {self.endpoint}")
        if parts[1] not in ("1", "2"):
            raise AgentError(f"The agent on {self.endpoint} uses protocol {parts[1]}; this app speaks 1 and 2")
        protocol = int(parts[1])
        fields = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
        name = fields.get("host") or self.host
        if len(name) > 64:
            name = self.host
        self.agent_host = name
        cpus = int(fields["cpus"]) if fields.get("cpus", "").isdigit() else 0
        m = self.machine
        m.name = name
        m.threads = max(0, min(1024, cpus)) if protocol >= 2 else 0
        detail = threads_text(cpus) if cpus else ""
        if name.lower() != self.host.lower():
            detail = f"{detail} · {self.endpoint}" if detail else self.endpoint
        m.detail = detail
        if protocol == 1:
            m.nets, m.disks, self.links = [], [], []
            m.note = (f"Agent {fields.get('version', '1')} on {name} sends only the total CPU – "
                      "install the new agent to see each thread, its network and disks")
        else:
            m.note = ""
        return protocol

    def _map(self, remote_seconds: float, arrival: float) -> float:
        """Agent clock -> this app's clock, from the fastest network trip seen so far."""
        candidate = arrival - remote_seconds
        if self.offset is None:
            self.offset = candidate
        else:
            self.offset += max(0.0, arrival - self.last_arrival) * self.DRIFT
            self.offset = min(self.offset, candidate)
        self.last_arrival = arrival
        t = remote_seconds + self.offset
        if t <= self.last_mapped:
            t = self.last_mapped + 1e-6
        self.last_mapped = t
        return t

    # "s <ms> <all %> <cpu0 %>... <rx B/s> <tx B/s>... <read B/s> <write B/s> <busy %>..."
    def _sample(self, line: str, arrival: float) -> None:
        try:
            v = [float(x) for x in line[2:].split()]
        except ValueError:
            return
        m = self.machine
        if any(math.isnan(x) or math.isinf(x) for x in v) or len(v) != 2 + m.threads + 2 * len(m.nets) + 3 * len(m.disks):
            return                                         # not the layout we know
        t = self._map(v[0] / 1000, arrival)
        cpu = [max(0.0, min(100.0, x)) for x in v[1:2 + m.threads]]
        k = 2 + m.threads
        nets = []
        for i in range(len(m.nets)):
            nets.append([max(0.0, v[k]) * 8, max(0.0, v[k + 1]) * 8, self.links[i] if i < len(self.links) else 0])
            k += 2
        disks = []
        for _ in m.disks:
            disks.append([max(0.0, min(100.0, v[k + 2])), max(0.0, v[k]), max(0.0, v[k + 1])])
            k += 3
        self.monitor.add_remote(m.id, m.version, t, cpu, nets, disks)

    # Protocol 1: "cpu <ms> <percent>"
    def _total_only(self, line: str, arrival: float) -> None:
        parts = line.split()
        try:
            ms, pct = float(parts[1]), float(parts[2])
        except (IndexError, ValueError):
            return
        if self.machine.threads == 0 and not (math.isnan(pct) or math.isinf(ms)):
            self.monitor.add_remote(self.machine.id, self.machine.version, self._map(ms / 1000, arrival),
                                    [max(0.0, min(100.0, pct))], [], [])

    def _not_found(self, name: str, have: list[str]) -> str:
        return f"not found on {self.machine.name}" + (f" (it has {', '.join(have)})" if have else "")

    # "nets enp1s0:1000 wlan0:down usb0:missing" - the adapters in sample order
    def _nets(self, line: str) -> None:
        items, links = [], []
        for it in line.split()[1:]:
            name, colon, state = it.rpartition(":")
            if not colon:
                name, state = it, "0"
            item = Item(name)
            if state == "missing":
                item.status = self._not_found(name, self.have_nets)
                links.append(-1.0)
            elif state == "down":
                links.append(-1.0)
            else:
                links.append(float(state) * 1e6 if state.isdigit() else 0.0)
            item.scale, item.problem = parse_scale(self.monitor.scale_for_remote(self.target, self.agent_host, name))
            items.append(item)
        self.links = links
        self.machine.nets = items
        self.machine.version += 1
        self.monitor.meta_changed()

    # "about sda 512110190592 Samsung SSD 870" - comes just before "disks"
    def _about(self, line: str) -> None:
        parts = line.split(" ", 3)
        if len(parts) < 3:
            return
        try:
            size = size_text(float(parts[2])) if float(parts[2]) > 0 else ""
        except ValueError:
            size = ""
        model = parts[3].strip()[:64] if len(parts) > 3 else ""
        self.about[parts[1]] = " · ".join(x for x in (model, size) if x)

    # "disks sda nvme0n1 sdz:missing" - the disks in sample order
    def _disks(self, line: str) -> None:
        items = []
        for it in line.split()[1:]:
            missing = it.endswith(":missing")
            item = Item(it[:-8] if missing else it)
            item.detail = self.about.get(item.name, "")
            if missing:
                item.status = self._not_found(item.name, self.have_disks)
            items.append(item)
        self.machine.disks = items
        self.machine.version += 1
        self.monitor.meta_changed()


# ---------------------------------------------------------------------------
# The sampler: this PC every tick, plus whatever the Linux machines sent
# ---------------------------------------------------------------------------

class PerfMonitor:
    def __init__(self, hub, mode: str) -> None:
        self.hub, self.mode = hub, mode
        self.interval = 50
        self.source = "cputime"
        self.source_note = ""
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict[str, list] = {}
        self._meta_dirty = True
        self.local = Machine("local", "This PC")
        self.remotes: list[RemoteAgent] = []
        self.choice, self.sections = Choice(), {}
        self._settings_stamp = None
        self._last_viewer = 0.0

    # --- used by the HTTP handler and the agents ------------------------------
    def set(self, interval: int | None = None, source: str | None = None) -> None:
        if interval is not None:
            if interval not in INTERVALS:
                raise ValueError("interval")
            self.interval = interval
        if source is not None:
            if source not in ("cputime", "utility"):
                raise ValueError("source")
            self.source = source
        self.meta_changed()

    def active(self) -> bool:
        """Someone is watching (or was, within the last few seconds)."""
        if self.hub.viewers():
            self._last_viewer = time.monotonic()
            return True
        return time.monotonic() - self._last_viewer < 3

    def meta_changed(self) -> None:
        self._meta_dirty = True

    def add_remote(self, mid: str, version: int, t: float, cpu, nets, disks) -> None:
        with self._lock:
            self._pending.setdefault(mid, [version, []])
            entry = self._pending[mid]
            if entry[0] != version:
                entry[0], entry[1] = version, []
            entry[1].append(_pack(t, cpu, nets, disks))

    def choice_for_remote(self, target: str, host: str | None) -> Choice:
        return self.sections.get(target.lower()) or (host and self.sections.get(host.lower())) or Choice()

    def scale_for_remote(self, target: str, host: str | None, adapter: str) -> str | None:
        return self.choice_for_remote(target, host).scales.get(adapter.lower())

    def start(self) -> None:
        threading.Thread(target=self._run, name="perf", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        for r in self.remotes:
            r.stop()

    # --- settings.ini ----------------------------------------------------------
    def _check_settings(self, force: bool = False) -> None:
        try:
            stamp = os.stat(SETTINGS_PATH).st_mtime
        except OSError:
            stamp = None
        if stamp == self._settings_stamp and not force:
            return
        self._settings_stamp = stamp
        choice, remotes, sections = read_settings()
        local_changed = choice.key() != self.choice.key() or force
        self.choice, self.sections = choice, sections
        if self.mode == "demo":
            return
        if local_changed:
            self._setup_local()
        for item in self.local.nets:
            item.scale, item.problem = parse_scale(choice.scales.get(item.name.lower()))
        have = [r.target.lower() for r in self.remotes]
        if [t.lower() for t in remotes[:MAX_ITEMS]] != have:
            for r in self.remotes:
                r.stop()
            self.remotes = [RemoteAgent(t, self) for t in remotes[:MAX_ITEMS]]
            for r in self.remotes:
                r.start()
        else:
            for r in self.remotes:
                r.reselect = True
        self.meta_changed()

    def _setup_local(self) -> None:
        found = adapters()
        nets = auto_adapters(found) if self.choice.networks is None else self.choice.networks
        drives = fixed_drives() if self.choice.disks is None else self.choice.disks
        m = self.local
        m.nets = [Item(n) for n in nets[:MAX_ITEMS]]
        m.disks = [Item(d) for d in drives[:MAX_ITEMS]]
        m.version += 1
        for item in m.nets:
            self._open_net(item, found)
        for item in m.disks:
            self._open_disk(item)

    def _open_net(self, item: Item, found=None) -> None:
        try:
            item.reader = NetReader(item.name, found if found is not None else adapters())
            item.status = ""
        except OSError as exc:
            item.reader, item.status, item.retry = None, f"{exc} – retrying every {RETRY_SECONDS} s", time.monotonic() + RETRY_SECONDS
        self.meta_changed()

    def _open_disk(self, item: Item) -> None:
        try:
            item.reader = DiskReader(item.name)
            item.detail, item.status = item.reader.detail, ""
        except OSError as exc:
            item.reader, item.status, item.retry = None, f"{exc} – retrying every {RETRY_SECONDS} s", time.monotonic() + RETRY_SECONDS
        self.meta_changed()

    # --- the loop ----------------------------------------------------------------
    def _run(self) -> None:
        try:
            _k32.SetThreadPriority(_k32.GetCurrentThread(), 1)   # above normal: steady timing
        except OSError:
            pass
        if self.mode == "demo":
            source = DemoPerf(self)
        else:
            source = LocalPerf(self)
            self._check_settings(force=True)
        next_t = time.perf_counter()
        last_meta = last_settings = 0.0
        while not self._stop.is_set():
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(min(delay, 0.1))
                continue
            step = self.interval / 1000
            next_t += step
            if time.perf_counter() - next_t > step:            # fell behind (or was idle): don't race
                next_t = time.perf_counter() + step
            if not self.active():
                continue
            mono = time.monotonic()
            if mono - last_settings >= 1:
                last_settings = mono
                if self.mode != "demo":
                    self._check_settings()
                    source.retry()
            t = now()
            local = source.sample(t)
            with self._lock:
                pending, self._pending = self._pending, {}
            machines = {"local": [self.local.version, [local]] if local else [self.local.version, []]}
            machines.update(pending)
            self.hub.publish(json.dumps({"t": round(t, 4), "m": machines}, separators=(",", ":")), "perf")
            if self._meta_dirty or mono - last_meta >= 2:
                self._meta_dirty = False
                last_meta = mono
                self.hub.publish(json.dumps(self.meta(), separators=(",", ":")), "perfmeta")

    def meta(self) -> dict:
        machines = [self.local] + [r.machine for r in self.remotes]
        return {"interval": self.interval, "source": self.source, "sourceNote": self.source_note,
                "settings": SETTINGS_PATH, "settingsFound": self._settings_stamp is not None,
                "machines": [m.meta() for m in machines]}


def _pack(t: float, cpu, nets, disks) -> list:
    return [round(t, 4), [round(x, 1) for x in cpu],
            [[round(a), round(b), round(c)] for a, b, c in nets],
            [[round(a, 1), round(b), round(c)] for a, b, c in disks]]


class LocalPerf:
    """This PC: CPU threads (or just the total), adapters and drives."""

    def __init__(self, mon: PerfMonitor) -> None:
        self.mon = mon
        try:
            self.cpu = CpuTimes()
        except OSError:
            self.cpu = SystemTimes()
        m = mon.local
        m.threads = self.cpu.n
        m.detail = threads_text(self.cpu.n) if self.cpu.n else ""
        self.utility = None
        self.utility_failed = False

    def retry(self) -> None:
        mono = time.monotonic()
        for item in self.mon.local.nets:
            if item.reader is None and mono >= item.retry:
                self.mon._open_net(item)
        for item in self.mon.local.disks:
            if item.reader is None and mono >= item.retry:
                self.mon._open_disk(item)

    def _total(self, values: list[float]) -> list[float]:
        mon = self.mon
        if mon.source != "utility":
            if self.utility:
                self.utility.close()
                self.utility = None
            if mon.source_note:
                mon.source_note = ""
                mon.meta_changed()
            return values
        if self.utility is None and not self.utility_failed:
            try:
                self.utility = Pdh([r"\Processor Information(_Total)\% Processor Utility"])
            except OSError as exc:
                self.utility_failed = True
                mon.source_note = f"Processor utility unavailable ({exc}) – showing CPU time"
                mon.meta_changed()
        if self.utility:
            v = self.utility.read()
            if v is not None:
                values = [max(0.0, min(100.0, v[0]))] + values[1:]
        return values

    def sample(self, t: float) -> list | None:
        cpu = self.cpu.read()
        if cpu is None:
            return None
        cpu = self._total(cpu)
        nets, disks = [], []
        for items, out, width in ((self.mon.local.nets, nets, 3), (self.mon.local.disks, disks, 3)):
            for item in items:
                v = None
                if item.reader is not None:
                    try:
                        v = item.reader.read(t)
                    except OSError as exc:
                        item.reader = None
                        item.status = f"{exc} – retrying every {RETRY_SECONDS} s"
                        item.retry = time.monotonic() + RETRY_SECONDS
                        self.mon.meta_changed()
                out.append(v or [0.0] * width)
        return _pack(t, cpu, nets, disks)


class DemoPerf:
    """Made-up numbers: a 16-thread PC with two adapters and two drives, and one Linux machine."""

    def __init__(self, mon: PerfMonitor) -> None:
        self.mon, self.rng = mon, random.Random()
        m = mon.local
        m.threads, m.detail = 16, threads_text(16)
        m.nets = [Item("Ethernet"), Item("Wi-Fi")]
        m.disks = [Item("C:"), Item("D:")]
        m.disks[0].detail, m.disks[1].detail = "Windows", "Data"
        m.version += 1
        r = Machine("r:demo-linux", "demo-linux")
        r.threads, r.detail = 4, threads_text(4) + " · 10.0.0.42"
        r.nets = [Item("enp1s0")]
        r.disks = [Item("sda")]
        r.disks[0].detail = "QEMU HARDDISK · 107 GB"
        r.version += 1
        agent = RemoteAgent("demo-linux", mon)
        agent.machine = r
        mon.remotes = [agent]                          # never started: samples come from here
        self.load = [self.rng.uniform(2, 20) for _ in range(16)]
        self.rload = [self.rng.uniform(1, 10) for _ in range(4)]
        self.t0 = now()

    def retry(self) -> None:
        pass

    def _walk(self, loads, burst):
        for i, x in enumerate(loads):
            target = 8 + 70 * burst * (1 if i % 3 == 0 else 0.3)
            loads[i] = min(100.0, max(0.0, x + (target - x) * 0.08 + self.rng.gauss(0, 6)))
        return loads

    def sample(self, t: float) -> list:
        rng, el = self.rng, t - self.t0
        burst = max(0.0, math.sin(el / 9)) ** 3
        loads = self._walk(self.load, burst)
        cpu = [sum(loads) / len(loads)] + loads
        dl = max(0.0, 4e8 * max(0.0, math.sin(el / 13)) ** 4 + rng.gauss(2e6, 1e6))
        nets = [[dl, dl * 0.04 + rng.uniform(1e5, 5e5), 1e9],
                [max(0.0, rng.gauss(3e5, 2e5)), max(0.0, rng.gauss(8e4, 4e4)), 0.0]]
        write = max(0.0, 3e8 * max(0.0, math.sin(el / 17 + 1)) ** 6 + rng.gauss(1e6, 5e5))
        disks = [[min(100.0, write / 4e6 + rng.uniform(0, 3)), max(0.0, rng.gauss(4e6, 2e6)), write],
                 [rng.uniform(0, 2), max(0.0, rng.gauss(2e5, 1e5)), max(0.0, rng.gauss(1e5, 5e4))]]
        rl = self._walk(self.rload, burst * 0.5)
        rm = self.mon.remotes[0].machine
        self.mon.add_remote(rm.id, rm.version, t - 0.004, [sum(rl) / 4] + rl,
                            [[max(0.0, rng.gauss(1.5e5, 5e4)), max(0.0, rng.gauss(4e4, 1e4)), 1e9]],
                            [[rng.uniform(0, 4), max(0.0, rng.gauss(5e4, 3e4)), max(0.0, rng.gauss(2e5, 1e5))]])
        return _pack(t, cpu, nets, disks)


if __name__ == "__main__":
    # A quick look at what this PC would show: python perfmon.py
    local, remotes, sections = read_settings()
    print("settings:", SETTINGS_PATH, "| networks:", local.networks, "| disks:", local.disks, "| remotes:", remotes)
    found = adapters()
    print("auto adapters:", auto_adapters(found), "| fixed drives:", fixed_drives())
    cpu = CpuTimes()
    readers = [NetReader(n, found) for n in (local.networks if local.networks is not None else auto_adapters(found))]
    disks = [DiskReader(d) for d in (local.disks if local.disks is not None else fixed_drives())]
    cpu.read()
    for r in readers + disks:
        r.read(now())
    time.sleep(1)
    t = now()
    c = cpu.read()
    print(f"CPU {c[0]:.1f}% over {cpu.n} threads; first four: {[round(x, 1) for x in c[1:5]]}")
    for n, r in zip(local.networks or auto_adapters(found), readers):
        v = r.read(t)
        print(f"  {n}: down {v[0] / 1e6:.2f} Mb/s, up {v[1] / 1e6:.2f} Mb/s, {link_text(v[2])}")
    for d, r in zip(local.disks or fixed_drives(), disks):
        v = r.read(t)
        print(f"  {d} ({r.detail}): busy {v[0]:.1f}%, read {v[1] / 1e6:.2f} MB/s, write {v[2] / 1e6:.2f} MB/s")

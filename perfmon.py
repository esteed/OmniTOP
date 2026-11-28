"""CPU, network and disk sampling, for this PC and for remote machines.

It started as a port of CPU Graph 2.0 (github.com/esteed/CpuGraph):

- CPU: every logical processor's busy time from one NtQuerySystemInformation
  call; the total can instead come from "% Processor Utility", the
  frequency-aware counter Task Manager shows.
- Network: one GetIfEntry2 call per adapter returns its 64-bit byte counters
  together with the live link state and speed. NetTracker picks the adapters
  worth showing.
- Disks: each drive letter's running totals from IOCTL_DISK_PERFORMANCE, which the
  LogicalDisk performance counters are made from. DiskRates turns running totals, of this
  PC's drives or a remote machine's, into throughput, IOPS, latency, queue and busy time.
- Remote machines: an agent streams timestamped samples over TCP. They're mapped
  onto this app's clock so all graphs line up. The agents are omniTop's own
  (agent.py on Windows, omnitop-agent.py on Linux), or CPU Graph's cpugraph-agent,
  which sends no memory.

Samples go out as "perf" server-sent events, one per tick; "perfmeta" events
describe the machines and their adapters and disks; "rmem" events carry the
remote machines' memory; "lat" events the latency and jitter of a machine's adapters,
while its Network page shows (latency.py).
"""
from __future__ import annotations

import ctypes
import json
import math
import random
import shutil
import socket
import struct
import sys
import threading
import time
import winreg
from ctypes import wintypes

import latency
import netwatch
from settings import AGENT_PORT
from sysmon import PDH_FMT_COUNTERVALUE

INTERVALS = (50, 100, 250, 500, 1000)    # ms between samples: 20, 10, 4, 2, 1 a second
RETRY_SECONDS = 5
ECHO_MS = latency.SITE_MS     # between round trips to each site, while a Network page shows
ECHO_IDLE = 5.0               # seconds the timing goes on after the page closes: a quick look back costs nothing
ECHO_SINCE = "2.3"            # the version whose agents time Google, Microsoft and AT&T
PROCIO_MS = 1000              # how often processes' disk I/O comes, while a Disk page shows a measure by process
PROCIO_HOLD = 30.0            # seconds a request for it lasts, unless it's made again
PROCIO_TOP = 16               # processes sent per disk; the rest together as "(other)"

_T0 = time.perf_counter()


def now() -> float:
    """Seconds on the clock every sample, local or remote, is placed on."""
    return time.perf_counter() - _T0


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
                        "filter": bool(row[OFF_FLAGS] & 0x2),  # a filter driver's view of another adapter
                        "hidden": _wstr(row, OFF_DESC).startswith("WAN Miniport"),   # plumbing for dial-up and VPNs
                        "up": struct.unpack_from("<i", row, OFF_OPER)[0] == 1})
        out.sort(key=lambda a: a["alias"].lower())
        return out
    finally:
        _ip.FreeMibTable(table)


def auto_adapters(found: list[dict], limit: int = 6) -> list[str]:
    """The connected network cards; failing that, any connected Ethernet or Wi-Fi adapter."""
    pick = [a["alias"] for a in found if a["up"] and a["card"]][:limit]
    return pick or [a["alias"] for a in found if a["up"] and a["type"] in (6, 71)][:limit]


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
        self._open(match)

    @classmethod
    def of(cls, adapter: dict) -> "NetReader":
        """The reader of one of adapters()."""
        r = cls.__new__(cls)
        r._open(adapter)
        return r

    def _open(self, adapter: dict) -> None:
        self.luid = adapter["luid"]
        self.buf = ctypes.create_string_buffer(ROW + 512)
        self.last = None
        self.link = -1.0                                        # bits/s, 0 unknown, -1 no link: as last read
        self._refresh()
        if _wstr(self.buf.raw, OFF_ALIAS) != adapter["alias"]:  # the row layout must be right
            raise OSError(f'Network adapter "{adapter["alias"]}" returned unexpected data')

    def _refresh(self) -> float:
        struct.pack_into("<Q", self.buf, 0, self.luid)
        rc = _ip.GetIfEntry2(self.buf)
        if rc:
            self.link = -1.0
            raise OSError("adapter removed" if rc in (2, 1168) else f"error {rc}")
        if struct.unpack_from("<i", self.buf, OFF_OPER)[0] != 1:
            self.link = -1.0
        else:
            speed = max(struct.unpack_from("<q", self.buf, OFF_TXLINK)[0],
                        struct.unpack_from("<q", self.buf, OFF_RXLINK)[0])
            self.link = float(speed) if speed > 0 else 0.0      # ULONG64 max ("unknown") reads as -1
        return self.link

    def read(self, t: float) -> list[float] | None:
        link = self._refresh()
        rx, tx = struct.unpack_from("<Q", self.buf, OFF_IN)[0], struct.unpack_from("<Q", self.buf, OFF_OUT)[0]
        last, self.last = self.last, (t, rx, tx)
        if not last or t <= last[0] or rx < last[1] or tx < last[2]:   # counters reset: start again
            return None
        dt = t - last[0]
        return [(rx - last[1]) * 8 / dt, (tx - last[2]) * 8 / dt, link]


REMOVABLE, FIXED, NETWORK, OPTICAL, RAMDISK = 2, 3, 4, 5, 6


def drive_letters() -> list[tuple[str, int]]:
    """Every drive letter in use, with its type."""
    mask = _k32.GetLogicalDrives()
    found = ((f"{chr(65 + i)}:", _k32.GetDriveTypeW(f"{chr(65 + i)}:\\")) for i in range(26) if mask >> i & 1)
    return [(d, kind) for d, kind in found if kind in (REMOVABLE, FIXED, NETWORK, OPTICAL, RAMDISK)]


def disk_usage(drive: str) -> tuple[int, int]:
    """Bytes used and in all, or (0, 0)."""
    old = _k32.SetErrorMode(1)
    try:
        u = shutil.disk_usage(drive + "\\")
        return u.used, u.total
    except OSError:
        return 0, 0
    finally:
        _k32.SetErrorMode(old)


def windows_version() -> str:
    v = sys.getwindowsversion()
    return f"{v.major}.{v.minor}.{v.build}"


def windows_name() -> str:
    """Windows 11 Pro 25H2, for example."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            name = str(winreg.QueryValueEx(k, "ProductName")[0])
            try:
                release = str(winreg.QueryValueEx(k, "DisplayVersion")[0])
            except OSError:
                release = ""
    except OSError:
        return "Windows"
    if sys.getwindowsversion().build >= 22000:              # the registry still says 10
        name = name.replace("Windows 10", "Windows 11")
    return f"{name} {release}".strip()


def fixed_drives(limit: int = 6) -> list[str]:
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


class DISK_PERFORMANCE(ctypes.Structure):
    _fields_ = [("BytesRead", ctypes.c_int64), ("BytesWritten", ctypes.c_int64), ("ReadTime", ctypes.c_int64),
                ("WriteTime", ctypes.c_int64), ("IdleTime", ctypes.c_int64), ("ReadCount", wintypes.DWORD),
                ("WriteCount", wintypes.DWORD), ("QueueDepth", wintypes.DWORD), ("SplitCount", wintypes.DWORD),
                ("QueryTime", ctypes.c_int64), ("StorageDeviceNumber", wintypes.DWORD),
                ("StorageManagerName", wintypes.WCHAR * 8)]


_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             wintypes.DWORD, wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
IOCTL_DISK_PERFORMANCE = 0x70020
INVALID_HANDLE = wintypes.HANDLE(-1).value


def disk_performance(drive: str) -> list[int]:
    """A drive letter's running totals, as Windows keeps them: [bytes read, bytes written, time reading, time
    writing, idle time, reads, writes, queue depth now, time of these figures], times in 100 ns. No rights are
    needed. The volume is opened each time, so that it isn't held: it can be taken out."""
    h = _k32.CreateFileW(f"\\\\.\\{drive}", 0, 3, None, 3, 0, None)
    if h is None or h == INVALID_HANDLE:
        raise OSError(f"Drive {drive} not available")
    try:
        p, n = DISK_PERFORMANCE(), wintypes.DWORD()
        if not _k32.DeviceIoControl(h, IOCTL_DISK_PERFORMANCE, None, 0, ctypes.byref(p), ctypes.sizeof(p), ctypes.byref(n), None):
            raise OSError(f"Windows keeps no activity figures for {drive}")
    finally:
        _k32.CloseHandle(h)
    return [p.BytesRead, p.BytesWritten, p.ReadTime, p.WriteTime, p.IdleTime, p.ReadCount, p.WriteCount, p.QueueDepth, p.QueryTime]


class DiskRates:
    """What one disk is doing, from its running totals.

    update() takes (time, bytes read, bytes written, reads, writes, seconds reading, seconds writing, seconds
    busy, seconds queued) and returns what happened since the last ones: [busy %, read B/s, written B/s, reads/s,
    writes/s, ms per read, ms per write, queue depth, busy % reading]. The rest of the busy time is writing: reads
    and writes overlap, so the time the disk was busy is shared out by how long each took.
    """

    def __init__(self) -> None:
        self.last: tuple | None = None
        self.reading = 0.5

    def update(self, raw: tuple) -> list[float] | None:
        last, self.last = self.last, raw
        if last is None or raw[0] <= last[0] or any(a < b for a, b in zip(raw[1:], last[1:])):   # or they started over
            return None
        dt, rb, wb, r, w, rt, wt, busy, queued = (a - b for a, b in zip(raw, last))
        if rt + wt > 0:
            self.reading = rt / (rt + wt)
        pct = min(100.0, 100.0 * busy / dt)
        return [pct, rb / dt, wb / dt, r / dt, w / dt, 1000 * rt / r if r else 0.0, 1000 * wt / w if w else 0.0,
                queued / dt, pct * self.reading]


def windows_totals(v: list) -> tuple:
    """disk_performance()'s figures as DiskRates takes them."""
    rb, wb, rt, wt, idle, r, w, _depth, at = v
    return at / 1e7, rb, wb, r, w, rt / 1e7, wt / 1e7, (at - idle) / 1e7, (rt + wt) / 1e7


def linux_totals(text: str, t: float) -> dict[str, tuple]:
    """Every disk's line of /proc/diskstats, read at time t, as DiskRates takes it."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        try:
            r, _, rs, rms, w, _, ws, wms, _, busy, queued = (int(x) for x in f[3:14])
        except ValueError:
            continue
        out[f[2]] = (t, rs * 512, ws * 512, r, w, rms / 1000, wms / 1000, busy / 1000, queued / 1000)
    return out


class DiskReader:
    """What one drive letter is doing: DiskRates' figures."""

    def __init__(self, drive: str) -> None:
        self.drive = drive
        disk_performance(drive)                            # says why, if there are none
        label = volume_label(drive)
        if label is None:
            raise OSError(f"Drive {drive} not available")
        self.detail = label
        self.rates = DiskRates()
        self.failures = 0

    def read(self, t: float) -> list[float] | None:
        try:
            v = disk_performance(self.drive)
        except OSError:
            self.failures += 1
            if self.failures > 3:
                raise
            return None
        self.failures = 0
        return self.rates.update(windows_totals(v))


# ---------------------------------------------------------------------------
# Which network adapters to show
# ---------------------------------------------------------------------------

class NetTracker:
    """Picks the network adapters worth showing on one machine, as they come and go.

    Network cards are shown while they have a link. Virtual adapters (VPNs, host-only
    networks, bridges) usually report a link whether or not anyone uses them, so
    they're shown once they carry real traffic: ACTIVE_BPS, averaged over about TAU
    seconds, for BUSY seconds. An adapter that loses its link stays for LOST seconds, marked lost, and
    goes if it hasn't come back by then.
    """

    ACTIVE_BPS = 64e3
    TAU, BUSY = 2.0, 1.0
    LOST = 10.0

    def __init__(self) -> None:
        self.known: dict[str, dict] = {}
        self._order = 0

    def update(self, t: float, found: list[dict]) -> bool:
        """found: every adapter there is now, as {"key", "name", "card", "link" (bits/s, 0 unknown, -1
        down), "bps"}. Returns whether what's shown, or whether it's lost, changed."""
        changed = False
        here = {a["key"] for a in found}
        any_card = any(a["card"] for a in found)
        for a in found:
            k = self.known.get(a["key"])
            if k is None:
                k = self.known[a["key"]] = {"avg": 0.0, "at": t, "busy": None, "shown": False, "lost": None, "order": 0}
            up = a["link"] >= 0
            f = 1 - math.exp(-max(0.0, t - k["at"]) / self.TAU)
            k["avg"] += ((a["bps"] if up else 0.0) - k["avg"]) * f
            k["at"] = t
            if k["avg"] < self.ACTIVE_BPS:
                k["busy"] = None
            elif k["busy"] is None:
                k["busy"] = t
            busy = k["busy"] is not None and t - k["busy"] >= self.BUSY
            # A machine without any network cards (some virtual machines, containers) has only virtual ones.
            wanted = up and (a["card"] or not any_card or busy)
            changed |= self._set(k, t, up, wanted)
        for key, k in list(self.known.items()):
            if key not in here:                            # the adapter itself is gone
                changed |= self._set(k, t, False, False)
                if not k["shown"]:
                    del self.known[key]
        return changed

    def _set(self, k: dict, t: float, up: bool, wanted: bool) -> bool:
        was = (k["shown"], k["lost"] is not None)
        if up:
            k["lost"] = None
            if wanted and not k["shown"]:
                k["shown"] = True
                self._order += 1
                k["order"] = self._order
        elif k["shown"]:
            if k["lost"] is None:
                k["lost"] = t
            elif t - k["lost"] >= self.LOST:
                k["shown"], k["lost"], k["avg"] = False, None, 0.0
        return was != (k["shown"], k["lost"] is not None)

    def shown(self) -> list[str]:
        """Keys of the adapters to show, in the order they appeared."""
        return sorted((key for key, k in self.known.items() if k["shown"]), key=lambda key: self.known[key]["order"])

    def lost(self, key: str) -> bool:
        k = self.known.get(key)
        return bool(k and k["lost"] is not None)


# ---------------------------------------------------------------------------
# Machines: what's shown of each, and how the view learns about it
# ---------------------------------------------------------------------------

class Item:
    """One adapter or disk on a machine."""

    def __init__(self, key: str, name: str = "") -> None:
        self.key, self.name = key, name or key
        self.detail = self.status = ""
        self.lost = False                                  # an adapter whose link went down a moment ago
        self.virtual = False                               # an adapter whose link speed means nothing
        self.used = self.size = 0                          # of a disk, in bytes
        self.reader = None
        self.retry = 0.0

    def meta(self) -> dict:
        out = {"key": self.key, "name": self.name, "detail": self.detail, "status": self.status}
        if self.lost:
            out["lost"] = True
        if self.virtual:
            out["virtual"] = True
        if self.size:
            out["used"], out["size"] = self.used, self.size
        return out


class Machine:
    def __init__(self, mid: str, name: str) -> None:
        self.id, self.name = mid, name
        self.detail = self.status = self.note = ""
        self.state = "ok"                                  # "ok", "connecting" or "down"
        self.os = self.system = self.kernel = self.version = ""   # "windows" or "linux"; its full name; agent version
        self.address = ""
        self.memory = False                                # whether it sends memory
        self.io = True                                     # and its disks' IOPS, latency and so on
        self.threads = 0
        self.nets: list[Item] = []
        self.disks: list[Item] = []
        self.silent: list[str] = []                        # disks there are no activity figures for
        self.version_no = 0                                # bumped whenever the shape of the samples changes
        self.ports = False                                 # whether it sends port traffic
        self.ports_note = ""                               # why not
        self.dynamic: list[int] | None = None              # its ephemeral port range
        self.ports_error = ""                              # what keeps it from capturing, now
        self.ports_nets = False                            # whether its Ports page can show one adapter's traffic
        self.procio = False                                # whether it tells which processes do disk I/O
        self.echo = True                                   # whether it measures its adapters' latency
        self.echo_note = ""                                # and why not
        self.ip = ""                                       # the address it's reached at

    def meta(self) -> dict:
        return {"id": self.id, "name": self.name, "detail": self.detail, "state": self.state, "status": self.status,
                "note": self.note, "os": self.os, "system": self.system, "kernel": self.kernel,
                "agent": self.version, "address": self.address, "ip": self.ip, "memory": self.memory, "io": self.io,
                "threads": self.threads, "v": self.version_no, "silent": self.silent,
                "ports": {"can": self.ports, "note": self.ports_note, "dynamic": self.dynamic, "error": self.ports_error,
                          "nets": self.ports_nets},
                "procio": self.procio,
                "lat": {"can": self.echo, "note": self.echo_note},
                "nets": [i.meta() for i in self.nets], "disks": [i.meta() for i in self.disks]}


def version_at_least(text: str, want: tuple[int, ...]) -> bool:
    try:
        return tuple(int(x) for x in text.split(".")[:len(want)]) >= want
    except ValueError:
        return False


class TaggedHub:
    """Publishes for one remote machine: what goes out says which, as "m"."""

    def __init__(self, hub, mid: str) -> None:
        self.hub, self.prefix = hub, '{"m":' + json.dumps(mid) + ","

    def publish(self, msg: str, event: str | None = None) -> None:
        self.hub.publish(self.prefix + msg[1:] if msg.startswith("{") and len(msg) > 2 else msg, event)


class RemoteScans:
    """A ScanWatch for each remote machine that sends probes, all stepped by one thread."""

    def __init__(self, hub) -> None:
        self.hub = hub
        self.lock = threading.Lock()
        self.watches: dict[str, netwatch.ScanWatch] = {}
        self.thread: threading.Thread | None = None
        self.stopping = threading.Event()

    def get(self, mid: str) -> netwatch.ScanWatch:
        with self.lock:
            w = self.watches.get(mid)
            if w is None:
                w = self.watches[mid] = netwatch.ScanWatch(TaggedHub(self.hub, mid))
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="remote scans", daemon=True)
                self.thread.start()
            return w

    def keep(self, mids) -> None:
        """Forget the machines that are no longer listed."""
        with self.lock:
            for mid in [m for m in self.watches if m not in mids]:
                del self.watches[mid]

    def views(self) -> list[dict]:
        with self.lock:
            watches = list(self.watches.items())
        return [{"m": mid, **v} for mid, w in watches for v in w.views()]

    def _run(self) -> None:
        while not self.stopping.wait(netwatch.ScanWatch.STEP):
            with self.lock:
                watches = list(self.watches.values())
            for w in watches:
                w.beat()

    def stop(self) -> None:
        self.stopping.set()


def threads_text(n: int) -> str:
    return f"{n} thread" if n == 1 else f"{n} threads"


def size_text(b: float) -> str:
    if b >= 1e12:
        return f"{b / 1e12:.1f}".rstrip("0").rstrip(".") + " TB"
    return f"{b / 1e9:.0f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


# ---------------------------------------------------------------------------
# Remote machines running an agent: omniTop's own (agent.py on Windows,
# omnitop-agent.py on Linux) or CPU Graph's cpugraph-agent (Linux)
# ---------------------------------------------------------------------------

class AgentError(Exception):
    pass


def endpoint_text(host: str, port: int) -> str:
    if port == AGENT_PORT:
        return host
    return (f"[{host}]" if ":" in host else host) + f":{port}"


def ip_literal(host: str) -> str:
    """The host if it's an IP address already, else ""."""
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return host
        except (OSError, ValueError):
            pass
    return ""


def resolve(host: str, port: int):
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    infos.sort(key=lambda i: i[0] != socket.AF_INET)       # IPv4 first
    if not infos:
        raise AgentError(f"Unknown host {host}")
    return infos[0]


def describe_failure(exc: Exception, host: str, endpoint: str, connected: bool) -> str:
    if isinstance(exc, AgentError):
        return str(exc)
    if isinstance(exc, socket.gaierror):
        return f"Unknown host {host}"
    if isinstance(exc, ConnectionRefusedError):
        return f"The agent isn't running on {endpoint}"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f"The agent on {endpoint} stopped sending" if connected else f"No answer from {endpoint}"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return f"Lost the connection to {endpoint}"
    if isinstance(exc, OSError):
        return f"Can't reach {host}" if exc.errno in (10051, 10065) else f"Network error with {endpoint}"
    return str(exc) or type(exc).__name__


def parse_hello(raw: bytes, endpoint: str) -> tuple[int, dict]:
    """(protocol, fields) from "cpugraph-agent 2 host=server1 cpus=2 kernel=5.15.0 version=2.0.0 os=linux omni=1"."""
    if not raw:
        raise AgentError(f"The agent on {endpoint} closed the connection")
    line = raw.decode("utf-8", errors="replace").strip()
    if line.startswith("err "):
        raise AgentError(f"Agent on {endpoint}: {line[4:]}")
    parts = line.split(" ")
    if len(parts) < 2 or parts[0] != "cpugraph-agent":
        raise AgentError(f"What answers on {endpoint} isn't an agent")
    if parts[1] not in ("1", "2"):
        raise AgentError(f"The agent on {endpoint} uses protocol {parts[1]}; this app speaks 1 and 2")
    return int(parts[1]), dict(p.split("=", 1) for p in parts[2:] if "=" in p)


def test_agent(host: str, port: int) -> dict:
    """Connect once and say what answers: {"ok", "host", "os", "system", "cpus", "agent", "memory"} or {"error"}."""
    endpoint, connected = endpoint_text(host, port), False
    try:
        info = resolve(host, port)
        with socket.socket(info[0], socket.SOCK_STREAM) as s:
            s.settimeout(4)
            s.connect(info[4])
            connected = True
            f = s.makefile("rb")
            _, fields = parse_hello(f.readline(512), endpoint)
            omni = fields.get("omni", "").isdigit() and int(fields["omni"]) >= 1
            system = ""
            if omni:                                       # it describes its system right after
                for _ in range(8):
                    line = f.readline(65536).decode("utf-8", errors="replace")
                    if not line.startswith("sys "):
                        continue
                    try:
                        system = str(json.loads(line[4:]).get("system", ""))[:80]
                    except (ValueError, AttributeError):
                        pass
                    break
    except Exception as exc:  # noqa: BLE001 - shown in the form
        return {"ok": False, "error": describe_failure(exc, host, endpoint, connected)}
    system_kind = fields.get("os", "").lower()
    return {"ok": True, "host": fields.get("host", "")[:64], "os": system_kind if system_kind in ("windows", "linux") else "linux",
            "system": system, "cpus": fields.get("cpus", ""), "agent": fields.get("version", "")[:32], "memory": omni}


class RemoteAgent:
    """Streams one remote machine's samples. Its own thread connects, reads and reconnects."""

    DRIFT = 200e-6           # real clocks differ by far less than 200 ppm
    MAX_LINE = 1 << 20

    def __init__(self, config: dict, monitor: "PerfMonitor") -> None:
        self.config, self.monitor = config, monitor
        self.host, self.port = config["address"], config["port"]
        self.machine = m = Machine(config["id"], config["name"] or self.host)
        m.state, m.status = "connecting", "Connecting…"
        m.os, m.address = config["os"], endpoint_text(self.host, self.port)
        m.ip = ip_literal(self.host)
        self._stop = threading.Event()
        self._sock = None
        self._lock = threading.Lock()
        self._telling = threading.Lock()
        self._told = ""
        self._connected = False
        self._waiters: dict[int, list] = {}                # port -> [[event, details]] asked of the agent
        self.ports_net: str | None = None                  # the adapter its Ports page shows, or None for all
        self._forget()

    def _forget(self) -> None:
        self.adapters: list[dict] = []                     # as the agent lists them
        self.disk_words: list[str] = []
        self.about: dict[str, tuple[str, int]] = {}
        self.usage: dict[str, tuple[int, int]] = {}
        self.labels: dict[tuple[str, str], str] = {}       # ("net" or "disk", word) -> the name that machine shows
        self.virtual: set[str] = set()
        self.silent: list[str] = []
        self.omni = False
        self.can: set[str] = set()                         # what its feeds can hold
        self.rates: dict[str, DiskRates] = {}
        self.io: dict[str, list[float]] = {}               # disks' figures, for the sample that follows
        self.tracker = NetTracker()
        self.echoes = latency.Window()                     # its adapters' round trips to the sites

    @property
    def endpoint(self) -> str:
        return endpoint_text(self.host, self.port)

    def start(self) -> None:
        threading.Thread(target=self._run, name=f"agent {self.endpoint}", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def rename(self, config: dict) -> None:
        """A new name for the same address."""
        self.config = config
        self._name_it()
        self.monitor.meta_changed()

    def _name_it(self) -> None:
        self.machine.name = self.config["name"] or getattr(self, "agent_host", "") or self.host

    def _close(self) -> None:
        with self._lock:
            s, self._sock = self._sock, None
            self._told = ""
            waiting, self._waiters = self._waiters, {}
        for ws in waiting.values():
            for w in ws:
                w[0].set()                                 # with no answer
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
            except Exception as exc:  # noqa: BLE001 - shown on the machine
                if self._stop.is_set():
                    return
                m = self.machine
                m.state = "down"
                m.status = describe_failure(exc, self.host, self.endpoint, self._connected)
                self.monitor.meta_changed()
            self._stop.wait(RETRY_SECONDS)

    def _send(self, sock, text: str) -> None:
        sock.sendall(text.encode("utf-8", errors="replace"))

    def tell(self, sock=None) -> None:
        """Send the agent what's wanted now, if that has changed. Called from several threads."""
        with self._telling:
            want = self._wishes()
            sock = sock or (self._sock if self._told else None)     # not before the agent has said hello
            if sock and want != self._told:
                self._told = want
                try:
                    self._send(sock, want)
                except OSError:
                    pass                                   # its own thread notices

    def _wishes(self) -> str:
        """The commands that say what we want now."""
        interval, mem = self.monitor.rates_for(self.machine.id)
        out = f"interval {interval}\n" + (f"mem {mem}\n" if self.omni else "")
        if "feed" in self.can:
            want = {"diskperf": ["*"]} if "diskperf" in self.can else {"files": ["/proc/diskstats"]}
            out += "feed disks sample " + json.dumps(want) + "\n" if self.monitor.wants_io(self.machine.id) else "feed disks off\n"
        if "ports" in self.can:                            # probes always: a scan is news on any page
            if "portsnet" in self.can:
                out += f"portsnet {self.ports_net or 'all'}\n"
            out += (f"ports {REMOTE_PORTS_MS}\n" if self.monitor.wants_ports(self.machine.id) else "ports off\n") + "probes on\n"
        if "procio" in self.can:
            out += f"procio {PROCIO_MS}\n" if self.monitor.wants_procio(self.machine.id) else "procio off\n"
        if "echo" in self.can:                             # the sites; 2.2's agents, their gateways
            on = self.monitor.wants_echo(self.machine.id)
            out += "echo off\n" if not on else latency.command(ECHO_MS) if "sites" in self.can else f"echo {ECHO_MS}\n"
        return out

    def port_details(self, port: int, seconds: float = 5.0) -> dict:
        """The processes and connections on a port of that machine, asked of its agent."""
        waiter = [threading.Event(), None]
        with self._lock:
            sock = self._sock if self._told and "ports" in self.can else None
            if sock is None:
                raise AgentError("not connected" if self.machine.state != "ok" else "its agent can't describe ports")
            self._waiters.setdefault(port, []).append(waiter)
        try:
            with self._telling:
                self._send(sock, f"port {port}\n")
            if not waiter[0].wait(seconds):
                raise AgentError("no answer")
            return waiter[1]
        finally:
            with self._lock:
                left = [w for w in self._waiters.get(port, []) if w is not waiter]
                if left:
                    self._waiters[port] = left
                else:
                    self._waiters.pop(port, None)

    def _session(self) -> None:
        self._connected = False
        info = resolve(self.host, self.port)
        ip = info[4][0].removeprefix("::ffff:")
        if ip != self.machine.ip:
            self.machine.ip = ip
            self.monitor.meta_changed()
        sock = socket.socket(info[0], socket.SOCK_STREAM)
        with self._lock:
            if self._stop.is_set():
                sock.close()
                return
            self._sock = sock
        try:
            sock.settimeout(3)
            try:
                sock.connect(info[4])
            except socket.timeout:
                raise AgentError(f"No answer from {self.endpoint}") from None
            self._connected = True
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(8)
            f = sock.makefile("rb")
            self._forget()
            self._told = ""
            protocol = self._hello(f.readline(512))
            self.tell(sock)
            self._send(sock, "net all\ndisk all\n" if self.omni else "net auto\ndisk auto\n" if protocol >= 2 else "")
            m = self.machine
            m.state, m.status = "ok", ""
            m.version_no += 1                              # new connection, new clock offset
            self.offset = None
            self.last_mapped = float("-inf")
            self.monitor.meta_changed()
            idle_since = None
            while not self._stop.is_set():
                if not self.monitor.active():              # nobody watching for a while: hang up
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since > 10:
                        m.state, m.status = "connecting", "Connecting…"
                        return
                else:
                    idle_since = None
                self.tell(sock)
                raw = f.readline(self.MAX_LINE)
                arrival = now()
                if not raw.endswith(b"\n"):
                    raise AgentError(f"The agent on {self.endpoint} closed the connection")
                self._line(raw.decode("utf-8", errors="replace").rstrip("\r\n"), arrival)
        finally:
            self._close()

    def _line(self, line: str, arrival: float) -> None:
        word, _, rest = line.partition(" ")
        if word == "s":
            self._sample(line, arrival)
        elif word == "cpu":
            self._total_only(line, arrival)
        elif word == "feed":
            self._feed(rest)
        elif word == "mem":
            self._memory(rest, arrival)
        elif word == "nets":
            self._nets(line)
        elif word == "disks":
            self._disks(line)
        elif word == "about":
            self._about(line)
        elif word == "usage":
            self._usage(line)
        elif word == "label":
            parts = line.split(" ", 3)
            if len(parts) == 4 and parts[3].strip():
                self.labels[parts[1], parts[2]] = parts[3].strip()[:255]
        elif word == "kind":                               # "kind net Tailscale virtual"
            parts = line.split()
            if len(parts) == 4 and parts[1] == "net":
                (self.virtual.add if parts[3] == "virtual" else self.virtual.discard)(parts[2])
        elif word == "silent":                             # disks it has no activity figures for, as JSON
            try:
                self.silent = [str(x)[:80] for x in json.loads(rest)][:64]
            except (ValueError, TypeError):
                return
            self.machine.silent = self.silent
            self.monitor.meta_changed()
        elif word == "sys":
            try:
                d = json.loads(rest)
                self.machine.system = str(d.get("system", ""))[:80]
                if d.get("os") in ("windows", "linux"):
                    self.machine.os = d["os"]
            except (ValueError, AttributeError):
                return
            self.monitor.meta_changed()
        elif word == "ports":
            self._ports(rest)
        elif word == "probes":
            self._probes(rest)
        elif word == "port":
            self._port(rest)
        elif word == "echo":
            self._echo(rest)
        elif word == "procio":
            self._procio(rest)
        elif word == "portsinfo":
            try:
                d = json.loads(rest)
                dyn = d.get("dynamic")
                self.machine.dynamic = [int(dyn[0]), int(dyn[1])] if isinstance(dyn, list) and len(dyn) == 2 else None
                self.machine.ports_error = str(d.get("error") or "")[:200]
            except (ValueError, TypeError, AttributeError):
                return
            self.monitor.meta_changed()
        elif word == "err":
            raise AgentError(f"Agent on {self.endpoint}: {rest}")
        # anything else ("have", or from a newer agent): ignore it

    # 'ports {"dt", "t", "in", "out", "si", "so", "p": [[port, tcp local, tcp remote, udp local, udp remote, name]], "s"}'
    def _ports(self, text: str) -> None:
        if not self.monitor.wants_ports(self.machine.id):
            return                                         # from before it was told
        try:
            d = json.loads(text)
            if d.get("net") != (self.ports_net if "portsnet" in self.can else None):
                return                                     # another adapter's: from before it was told
            num = lambda x: max(0, int(x))
            rows = [[num(r[0]), num(r[1]), num(r[2]), num(r[3]), num(r[4]), str(r[5])[:64] if len(r) > 5 else ""]
                    for r in d["p"] if 0 < int(r[0]) < 65536]
            own = [[num(r[0]), num(r[1]), num(r[2]), num(r[3]), num(r[4])] for r in d.get("s", []) if 0 < int(r[0]) < 65536]
            out = {"id": self.machine.id, "dt": float(d["dt"]), "t": float(d.get("t", 0)), "in": num(d["in"]),
                   "out": num(d["out"]), "si": num(d.get("si", 0)), "so": num(d.get("so", 0)), "p": rows, "s": own}
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            return
        self.monitor.hub.publish(json.dumps(out, separators=(",", ":")), "rports")

    # 'probes {"t": now, "p": [[time, remote, port, 0 TCP | 1 UDP, "open" | "closed", remote port, local address]]}'
    def _probes(self, text: str) -> None:
        try:
            d = json.loads(text)
            shift = time.time() - float(d["t"])            # its clock to this one's, give or take the trip
            found = d["p"][:20000]
        except (ValueError, KeyError, TypeError):
            return
        watch = self.monitor.remote_scans.get(self.machine.id)
        for p in found:
            try:
                t, remote, lport, proto, kind, rport, local = p[:7]
                if kind in ("open", "closed") and proto in (0, 1) and 0 < int(lport) < 65536:
                    watch.probe(str(remote)[:64], int(lport), int(proto), kind, int(rport), str(local)[:64], float(t) + shift)
            except (ValueError, TypeError):
                continue

    # 'echo {"t": now, "g": {adapter: gateway}, "r": [[adapter, time, ms or null, site, "icmp" | "tcp"]...], "e": ""}'
    # (2.2's agents: [adapter, time, ms or null], of the gateway)
    def _echo(self, text: str) -> None:
        if not self.monitor.wants_echo(self.machine.id):
            return                                         # from before it was told
        try:
            d = json.loads(text)
            shift = time.time() - float(d["t"])            # its clock to this one's
            gws = {str(k)[:64]: str(v)[:64] for k, v in d.get("g", {}).items()}
            for r in d.get("r", [])[:1000]:
                site, method = (str(r[3])[:16], str(r[4])[:8]) if len(r) >= 5 else (latency.GATEWAY, "icmp")
                self.echoes.add(str(r[0])[:64], site, float(r[1]) + shift,
                                None if r[2] is None else max(0.0, min(60000.0, float(r[2]))), method)
            error = str(d.get("e") or "")[:300]
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        self.monitor.publish_echo(self.machine.id, gws, self.echoes.summary(time.time()), error)

    # 'procio {"t": now, "dt": seconds, "disks": {disk or "*": [[name, pid, read B/s, written B/s, reads/s, writes/s]]},
    #          "e": ""}' - "*" when it's every disk's (Linux doesn't count processes' I/O per disk); null for what isn't counted
    def _procio(self, text: str) -> None:
        if not self.monitor.wants_procio(self.machine.id):
            return
        try:
            d = json.loads(text)
            disks = {}
            for key, rows in d.get("disks", {}).items():
                disks[str(key)[:64]] = [[str(r[0])[:64], int(r[1] or 0)] + [None if x is None else max(0.0, float(x)) for x in r[2:6]]
                                        for r in rows[:64] if len(r) >= 6]
            error = str(d.get("e") or "")[:300]
        except (ValueError, TypeError, AttributeError, KeyError):
            return
        self.monitor.publish_procio(self.machine.id, disks, error)

    def _port(self, text: str) -> None:
        try:
            d = json.loads(text)
            port = int(d["port"])
        except (ValueError, KeyError, TypeError):
            return
        with self._lock:
            waiting = self._waiters.pop(port, [])
        for w in waiting:
            w[1] = d
            w[0].set()

    def _hello(self, raw: bytes) -> int:
        protocol, fields = parse_hello(raw, self.endpoint)
        name = fields.get("host") or self.host
        self.agent_host = name if len(name) <= 64 else self.host
        self.omni = protocol >= 2 and fields.get("omni", "").isdigit() and int(fields["omni"]) >= 1
        self.can = set(fields.get("can", "").split(",")) if self.omni else set()
        cpus = int(fields["cpus"]) if fields.get("cpus", "").isdigit() else 0
        m = self.machine
        self._name_it()
        m.threads = max(0, min(1024, cpus)) if protocol >= 2 else 0
        m.detail = " · ".join(x for x in (threads_text(cpus) if cpus else "",
                                          self.endpoint if self.endpoint.lower() != m.name.lower() else "") if x)
        kind = fields.get("os", "").lower()
        m.os = kind if kind in ("windows", "linux") else "linux"   # cpugraph-agent is for Linux, and doesn't say
        m.kernel, m.version = fields.get("kernel", "")[:80], fields.get("version", "")[:32]
        m.memory, m.io = self.omni, "feed" in self.can
        m.silent = []
        m.ports, m.dynamic, m.ports_error = "ports" in self.can, None, ""
        m.ports_nets = "portsnet" in self.can
        m.procio = "procio" in self.can
        if m.ports:
            m.ports_note = ""
        elif self.omni and version_at_least(m.version, (2, 1)):
            m.ports_note = ("This PC doesn't let the agent watch its network." if m.os == "windows" else
                            "The agent there can't see ports: that takes its omnitop-agent-ports service, "
                            "which sudo python3 omnitop-agent.py --install sets up.")
        else:
            m.ports_note = (f"Its agent ({m.version or 'an older one'}) doesn't send port traffic. "
                            "Install OmniTOP 2.1's agent there to see it.")
        m.echo = "echo" in self.can
        m.echo_note = ("" if "sites" in self.can else
                       f"Its agent ({m.version}) times only its gateway. Install OmniTOP {ECHO_SINCE}'s agent there "
                       "for Google, Microsoft and AT&T." if m.echo else
                       f"Its agent ({m.version or 'an older one'}) doesn't measure latency. "
                       f"Install OmniTOP {ECHO_SINCE}'s agent there to see it.")
        self.echoes.clear()
        m.nets, m.disks = [], []
        if protocol == 1:
            m.note = f"Agent {m.version or '1'} sends only the total CPU. Install OmniTOP's agent there to see the rest."
        elif not self.omni and m.os == "windows":
            m.note = f"Its agent is OmniTOP {m.version or '1.9'}'s, which doesn't send memory. Install this one's there to see it."
        elif not self.omni:
            m.note = "This is CPU Graph's agent, which doesn't send memory. Install OmniTOP's agent there to see it."
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
        m, na, nd = self.machine, len(self.adapters), len(self.disk_words)
        if any(math.isnan(x) or math.isinf(x) for x in v) or len(v) != 2 + m.threads + 2 * na + 3 * nd:
            return                                         # not the layout we know
        t = self._map(v[0] / 1000, arrival)
        cpu = [max(0.0, min(100.0, x)) for x in v[1:2 + m.threads]]
        k = 2 + m.threads
        rates = {}
        for a in self.adapters:
            rates[a["key"]] = (max(0.0, v[k]) * 8, max(0.0, v[k + 1]) * 8)
            a["bps"] = sum(rates[a["key"]])
            k += 2
        if self.tracker.update(arrival, self.adapters):
            self._show_nets()
        disks = []
        for word in self.disk_words:
            plain = [max(0.0, min(100.0, v[k + 2])), max(0.0, v[k]), max(0.0, v[k + 1])]
            disks.append(self.io.get(word) or plain + [0.0] * 6 if m.io else plain)
            k += 3
        self.io = {}
        links = {a["key"]: a["link"] for a in self.adapters}
        nets = [[*rates.get(i.key, (0.0, 0.0)), links.get(i.key, -1.0)] for i in m.nets]
        self.monitor.add_remote(m.id, m.version_no, t, cpu, nets, disks)

    # 'disks {"t": ms, "files": {"/proc/diskstats": ...}}' or 'disks {"t": ms, "diskperf": {"C:": [...]}}'
    def _feed(self, rest: str) -> None:
        name, _, text = rest.partition(" ")
        if name != "disks":
            return
        try:
            d = json.loads(text)
            if "diskperf" in d:
                totals = {k: windows_totals(v) for k, v in d["diskperf"].items()}
            else:
                totals = linux_totals(d["files"]["/proc/diskstats"], float(d["t"]) / 1000)
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        self.io = {}
        for word in self.disk_words:
            raw = totals.get(self.labels.get(("disk", word), word))
            if raw is not None:
                v = self.rates.setdefault(word, DiskRates()).update(raw)
                if v is not None:
                    self.io[word] = v

    # Protocol 1: "cpu <ms> <percent>"
    def _total_only(self, line: str, arrival: float) -> None:
        parts = line.split()
        try:
            ms, pct = float(parts[1]), float(parts[2])
        except (IndexError, ValueError):
            return
        if self.machine.threads == 0 and not (math.isnan(pct) or math.isinf(ms)):
            self.monitor.add_remote(self.machine.id, self.machine.version_no, self._map(ms / 1000, arrival),
                                    [max(0.0, min(100.0, pct))], [], [])

    def _memory(self, text: str, arrival: float) -> None:
        try:
            d = json.loads(text)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        d["t"] = round(arrival, 3)
        d["iv"] = self.monitor.rates_for(self.machine.id)[1]
        d["os"] = self.machine.os
        self.monitor.remote_memory(self.machine.id, d)

    # "nets enp1s0:1000 wlan0:down usb0:missing" - the adapters in sample order
    def _nets(self, line: str) -> None:
        out = []
        for it in line.split()[1:]:
            word, colon, state = it.rpartition(":")
            if not colon:
                word, state = it, "0"
            link = -1.0 if state in ("missing", "down") else float(state) * 1e6 if state.isdigit() else 0.0
            out.append({"key": word, "name": self.labels.get(("net", word), word),
                        "card": word not in self.virtual, "link": link, "bps": 0.0})
        self.adapters = out
        self.tracker.update(now(), out)
        self._show_nets()

    def _show_nets(self) -> None:
        m, names = self.machine, {a["key"]: a["name"] for a in self.adapters}
        old = {i.key: i for i in m.nets}
        m.nets = []
        for key in self.tracker.shown():
            i = old.get(key) or Item(key)
            i.name = names.get(key, i.name)
            i.virtual = key in self.virtual
            i.lost = self.tracker.lost(key)
            m.nets.append(i)
        if [i.key for i in m.nets] != list(old):
            m.version_no += 1
        self.monitor.meta_changed()

    # "about sda 512110190592 Samsung SSD 870" - comes just before "disks"
    def _about(self, line: str) -> None:
        parts = line.split(" ", 3)
        if len(parts) < 3 or not parts[2].isdigit():
            return
        self.about[parts[1]] = (parts[3].strip()[:80] if len(parts) > 3 else "", int(parts[2]))

    # "usage root 12345678 98765432": bytes used and size of a drive or filesystem
    def _usage(self, line: str) -> None:
        parts = line.split()
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            self.usage[parts[1]] = (int(parts[2]), int(parts[3]))
            for word, item in zip(self.disk_words, self.machine.disks):
                if word == parts[1]:
                    item.used, item.size = self.usage[word]
            self.monitor.meta_changed()

    # "disks sda nvme0n1 sdz:missing" - the disks in sample order
    def _disks(self, line: str) -> None:
        items, words = [], []
        for it in line.split()[1:]:
            missing = it.endswith(":missing")
            word = it[:-8] if missing else it
            words.append(word)
            i = Item(word, self.labels.get(("disk", word), word))
            text, size = self.about.get(word, ("", 0))
            used = self.usage.get(word)
            i.detail = " · ".join(x for x in (text, size_text(size) if size and (not self.omni or self.machine.os == "linux") else "") if x)
            if used:
                i.used, i.size = used
            if missing:
                i.status = f"not found on {self.machine.name}"
            items.append(i)
        if words != self.disk_words:
            self.machine.version_no += 1
        self.disk_words = words
        self.machine.disks = items
        self.monitor.meta_changed()


# ---------------------------------------------------------------------------
# This PC
# ---------------------------------------------------------------------------

class LocalNets:
    """This PC's network adapters: cards and busy virtual ones, found as they come and go."""

    LOOK_EVERY = 1.0      # seconds between looks at which adapters there are
    IDLE_EVERY = 0.5      # and between readings of the ones that aren't shown

    def __init__(self, mon: "PerfMonitor") -> None:
        self.mon = mon
        self.tracker = NetTracker()
        self.readers: dict[str, NetReader] = {}
        self.info: dict[str, dict] = {}
        self._look_at = self._idle_at = 0.0

    def look(self, mono: float) -> None:
        if mono < self._look_at:
            return
        self._look_at = mono + self.LOOK_EVERY
        found = {str(a["luid"]): a for a in adapters() if not a["filter"] and not a["hidden"]}
        for key in list(self.readers):
            if key not in found:
                del self.readers[key]
        for key, a in found.items():
            if key not in self.readers and a["up"]:
                try:
                    self.readers[key] = NetReader.of(a)
                except OSError:
                    pass
        self.info = found

    def sample(self, t: float) -> list[list[float]]:
        mono = time.monotonic()
        shown = self.tracker.shown()
        idle = mono >= self._idle_at
        if idle:
            self._idle_at = mono + self.IDLE_EVERY
        values: dict[str, list[float]] = {}
        seen = []
        for key, reader in list(self.readers.items()):
            if not idle and key not in shown:
                continue
            try:
                v = reader.read(t)
            except OSError:                                # removed: it's lost until the next look says so too
                del self.readers[key]
                self.info.pop(key, None)
                continue
            if v is None:
                v = [0.0, 0.0, reader.link]
            values[key] = v
        for key in self.readers:
            a = self.info.get(key)
            if a is None:
                continue
            v = values.get(key)
            last = self.tracker.known.get(key)
            seen.append({"key": key, "name": a["alias"], "card": a["card"],
                         "link": v[2] if v else self.readers[key].link,
                         "bps": v[0] + v[1] if v else (last["avg"] if last else 0.0)})
        if self.tracker.update(t, seen):
            self._show()
        return [values.get(i.key, [0.0, 0.0, -1.0]) for i in self.mon.local.nets]

    def _show(self) -> None:
        m = self.mon.local
        old = {i.key: i for i in m.nets}
        m.nets = []
        for key in self.tracker.shown():
            i = old.get(key) or Item(key)
            a = self.info.get(key)
            if a:
                i.name, i.virtual = a["alias"], not a["card"]
                i.detail = a["desc"] if i.virtual else ""
            i.lost = self.tracker.lost(key)
            m.nets.append(i)
        if [i.key for i in m.nets] != list(old):
            m.version_no += 1
        self.mon.meta_changed()


class LocalDisks:
    """This PC's drive letters, found as they come and go."""

    LOOK_EVERY = 3.0

    def __init__(self, mon: "PerfMonitor") -> None:
        self.mon = mon
        self._look_at = self._usage_at = 0.0
        self._skip: dict[str, float] = {}                  # letters without counters -> when to try again

    def look(self, mono: float) -> None:
        if mono < self._look_at:
            return
        self._look_at = mono + self.LOOK_EVERY
        m = self.mon.local
        have = {i.key: i for i in m.disks}
        items, silent = [], []
        for d, kind in drive_letters():
            if kind == NETWORK:                            # Windows keeps no figures, and asking one can take long
                silent.append(f"{d} network drive")
                continue
            i = have.get(d)
            if i is None and mono >= self._skip.get(d, 0.0):
                try:
                    i = Item(d)
                    i.reader = DiskReader(d)
                    i.detail = i.reader.detail
                    self._usage_at = 0.0
                except OSError:
                    i = None
                    self._skip[d] = mono + 60
            if i is not None:
                items.append(i)
            elif volume_label(d) is not None:
                silent.append(f"{d} {volume_label(d)}".strip())
        if [i.key for i in items] != list(have):
            m.version_no += 1
            self.mon.meta_changed()
        if silent != m.silent:
            m.silent = silent
            self.mon.meta_changed()
        m.disks = items
        if mono >= self._usage_at:
            self._usage_at = mono + 10
            for i in items:
                used, size = disk_usage(i.key)
                if (used, size) != (i.used, i.size):
                    i.used, i.size = used, size
                    self.mon.meta_changed()

    def sample(self, t: float) -> list[list[float]]:
        out = []
        for i in self.mon.local.disks:
            v = None
            if i.reader is not None:
                try:
                    v = i.reader.read(t)
                except OSError:                            # unplugged, or its figures went away
                    i.reader = None
                    i.status = "no figures from Windows"
                    self._skip[i.key] = time.monotonic() + 10
                    self._look_at = 0.0
                    self.mon.meta_changed()
            elif time.monotonic() >= self._skip.get(i.key, 0.0):
                try:
                    i.reader, i.status = DiskReader(i.key), ""
                except OSError:
                    self._skip[i.key] = time.monotonic() + 10
            out.append(v or [0.0] * 9)
        return out


class LocalEcho:
    """This PC's adapters' round trips to the sites, while its Network page shows."""

    def __init__(self, mon: "PerfMonitor") -> None:
        self.mon = mon
        self.echoes: latency.Echoes | None = None
        self.window = latency.Window()
        self.idle_since = 0.0

    def look(self, mono: float) -> None:
        if self.mon.wants_echo("local"):
            self.idle_since = 0.0
            if self.echoes is None:
                self.window.clear()
                self.echoes = latency.Echoes(self._find, self._report, ECHO_MS).start()
        elif self.echoes is not None:
            self.idle_since = self.idle_since or mono
            if mono - self.idle_since > ECHO_IDLE:
                self.echoes.stop()
                self.echoes = None

    def _find(self):
        keys = {}
        for i in self.mon.local.nets:                      # keyed by LUID
            if i.key.isdigit():
                keys[int(i.key)] = i.key
        return latency.plan(keys, [(name, host) for name, _, host in latency.SITES])

    def _report(self, info: dict, done: list, error: str) -> None:
        for key, t, rtt, method in done:
            adapter, site = key.rsplit("|", 1)
            self.window.add(adapter, site, t, rtt, method)
        if self.mon.wants_echo("local"):
            self.mon.publish_echo("local", info.get("g", {}), self.window.summary(time.time()), error)


class DemoEcho:
    """Made-up round trips for a demo machine's adapters, while its Network page shows."""

    GATEWAYS = {"e": ("192.168.1.1", 0.42), "w": ("192.168.1.1", 2.8), "v": ("100.64.0.1", 21.0),
                "eth0": ("10.0.0.1", 0.31), "vpn": ("10.8.0.1", 17.0)}   # "eth1", a storage network, has none
    SITES = {"google": 14.0, "microsoft": 22.0, "att": 27.0}             # ms beyond the gateway

    def __init__(self, mon: "PerfMonitor", mid: str) -> None:
        self.mon, self.mid, self.at = mon, mid, 0.0
        self.window = latency.Window()

    def step(self, t: float, machine: Machine) -> None:
        if not self.mon.wants_echo(self.mid) or t < self.at:
            return
        self.at = t + ECHO_MS / 1000
        wall, gws = time.time(), {}
        for i in machine.nets:
            if i.lost or i.key not in self.GATEWAYS:
                continue
            gws[i.key], base = self.GATEWAYS[i.key]
            radio = i.key in ("w", "v", "vpn")
            for site, far in self.SITES.items():
                lost = random.random() < (0.02 if radio else 0.002)
                spike = 4 * base + 6 if radio and random.random() < 0.06 else 0.0
                rtt = (base + far) * random.lognormvariate(0, 0.12 if radio else 0.04) + spike
                self.window.add(i.key, site, wall, None if lost else rtt, "icmp")
        self.mon.publish_echo(self.mid, gws, self.window.summary(wall), "")


class LocalProcio:
    """This PC's processes' disk I/O, per drive letter, while its Disk page shows a measure by process (diskio.py)."""

    SESSION = ("omniTop disks", "9B1E3C52-6F0A-4D7B-A8E2-5C9F1D3B7A46")

    def __init__(self, mon: "PerfMonitor") -> None:
        self.mon, self.io, self.at = mon, None, 0.0

    def look(self, mono: float) -> None:
        wanted = self.mon.wants_procio("local")
        if self.io is None:
            if not wanted:
                return
            import diskio
            import server
            self.io = diskio.ProcIO((self.SESSION[0], server.GUID.parse(self.SESSION[1])),
                                    lambda: [i.key for i in self.mon.local.disks])
        if wanted and mono < self.at:
            return
        self.at = mono + PROCIO_MS / 1000
        got = self.io.take(wanted)
        if got is not None:
            rows, error = got
            self.mon.publish_procio("local", {f"{letter}:": r for letter, r in rows.items()}, error)


class DemoProcs:
    """Made-up processes' disk I/O for a demo machine's disks, while its Disk page shows a measure by process."""

    WINDOWS = [("MsMpEng", 3412, 5), ("SearchIndexer", 7240, 3), ("chrome", 11872, 4), ("OneDrive", 9120, 3),
               ("System", 4, 2), ("svchost", 1504, 2), ("Code", 15220, 1), ("steam", 18044, 2), ("explorer", 6200, 0.5),
               ("Teams", 21108, 0.5)]
    LINUX = [("postgres", 1201, 5), ("rsync", 4410, 3), ("systemd-journald", 412, 1), ("jbd2/sda1-8", 280, 1),
             ("containerd", 990, 1.5), ("restic", 6020, 2), ("kworker/u8:2", 77, 0.5)]

    def __init__(self, mon: "PerfMonitor", mid: str, linux: bool) -> None:
        self.mon, self.mid, self.linux, self.at = mon, mid, linux, 0.0
        self.mix = {}                                      # disk -> {name: weight}, drifting

    def step(self, t: float, machine: Machine, disks: list) -> None:
        if not self.mon.wants_procio(self.mid) or t < self.at:
            return
        self.at = t + PROCIO_MS / 1000
        procs = self.LINUX if self.linux else self.WINDOWS
        out = {}
        if self.linux:                                     # every disk's together, and no operations, as Linux counts it
            rb = sum(d[1] for d in disks)
            wb = sum(d[2] for d in disks)
            out["*"] = self._rows("*", procs, rb, wb, None, None)
        else:
            for item, d in zip(machine.disks, disks):
                out[item.key] = self._rows(item.key, procs, d[1], d[2], d[3], d[4])
        self.mon.publish_procio(self.mid, out, "")

    def _rows(self, key: str, procs, rb, wb, ro, wo) -> list:
        mix = self.mix.setdefault(key, {name: w * random.uniform(0.5, 1.5) for name, _, w in procs})
        for name in mix:
            mix[name] = max(0.05, mix[name] * random.uniform(0.85, 1.18))
        total = sum(mix.values())
        rows = []
        for name, pid, _ in procs:
            share = mix[name] / total
            rows.append([name, pid, rb * share, wb * share * random.uniform(0.6, 1.4),
                         None if ro is None else ro * share, None if wo is None else wo * share])
        return rows


class LocalPerf:
    """This PC: CPU threads (or just the total), adapters and drives."""

    def __init__(self, mon: "PerfMonitor") -> None:
        self.mon = mon
        try:
            self.cpu = CpuTimes()
        except OSError:
            self.cpu = SystemTimes()
        m = mon.local
        m.threads = self.cpu.n
        m.detail = threads_text(self.cpu.n) if self.cpu.n else ""
        m.os, m.system, m.kernel = "windows", windows_name(), windows_version()
        m.memory = m.ports_nets = True
        self.nets, self.disks, self.echo, self.procio = LocalNets(mon), LocalDisks(mon), LocalEcho(mon), LocalProcio(mon)
        m.procio = True
        self.utility = None
        self.utility_failed = False

    def look(self) -> None:
        """Between samples: what came or went."""
        mono = time.monotonic()
        self.nets.look(mono)
        self.disks.look(mono)
        self.echo.look(mono)
        self.procio.look(mono)

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
        return _pack(t, self._total(cpu), self.nets.sample(t), self.disks.sample(t))


# ---------------------------------------------------------------------------
# The sampler: this PC every tick, plus whatever the remote machines sent
# ---------------------------------------------------------------------------

class PerfMonitor:
    def __init__(self, hub, mode: str, settings) -> None:
        self.hub, self.mode, self.settings = hub, mode, settings
        self.interval = 50
        self.source = "cputime"
        self.source_note = ""
        self.focus: str | None = None                      # the remote machine being looked at closely
        self.page: str | None = None                       # and the page of it that's showing
        self.local_page: str | None = None                 # the page showing when it's this PC's, or the list
        self.procio_for: tuple[str, float] | None = None   # (machine, when asked): whose processes' disk I/O to send
        self.numbers = False                               # the others are shown as numbers only
        self.remote_scans = RemoteScans(hub)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict[str, list] = {}
        self._memory: dict[str, dict] = {}
        self._meta_dirty = True
        self.local = Machine("local", "This PC" if mode == "demo" else socket.gethostname())
        self.remotes: list[RemoteAgent] = []
        self._remotes_lock = threading.Lock()
        self._last_viewer = 0.0
        self._demo = None

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

    def set_focus(self, mid: str | None, numbers: bool | None = None, page: str | None = None) -> None:
        self.focus = mid if mid and any(r.machine.id == mid for r in self.remotes) else None
        self.page = page if self.focus else None
        self.local_page = None if self.focus else page
        if numbers is not None:
            self.numbers = numbers
        for r in self.remotes:
            if r.machine.id == self.focus:
                r.tell()                                   # at once, rather than with its next sample

    def rates_for(self, mid: str) -> tuple[int, int]:
        """(ms between samples, ms between memory samples) wanted from a machine; 0 for no memory.

        The one being looked at sends at the chosen rate. The others send
        more slowly, the more of them there are, so any number can be watched.
        """
        n = len(self.remotes)
        if mid == self.focus:
            return self.interval, 500
        if self.numbers:
            return max(self.interval, 1000 if n <= 100 else 2000), 0
        slowest, mem = (250, 1000) if n <= 16 else (500, 2000) if n <= 48 else (1000, 3000)
        return max(self.interval, slowest), mem

    def wants_ports(self, mid: str) -> bool:
        """Whether a machine should send its traffic per port: while its Ports page shows."""
        return mid == self.focus and self.page == "net"

    def set_procio(self, mid: str | None) -> None:
        """Send a machine's processes' disk I/O (its Disk page shows a measure by process), or nobody's."""
        self.procio_for = (mid, time.monotonic()) if mid else None
        for r in self.remotes:
            r.tell()

    def wants_procio(self, mid: str) -> bool:
        p = self.procio_for
        return bool(p) and p[0] == mid and time.monotonic() - p[1] < PROCIO_HOLD

    def publish_procio(self, mid: str, disks: dict, error: str = "") -> None:
        """Processes' disk I/O per disk ("*": over every disk), as rates: [[name, pid, read B/s, written B/s, reads/s, writes/s]]."""
        per = "all" if "*" in disks else "disk"
        self.hub.publish(json.dumps({"m": mid, "t": round(now(), 3), "disks": disks, "per": per, "e": error},
                                    separators=(",", ":")), "procio")

    def wants_echo(self, mid: str) -> bool:
        """Whether a machine's adapters' gateways should be pinged: while its Network page shows."""
        if mid == "local":
            return self.focus is None and self.local_page == "network"
        return mid == self.focus and self.page == "network"

    def publish_echo(self, mid: str, gateways: dict, summary: dict, error: str) -> None:
        """A machine's adapters' latency, jitter and losses over the last few seconds, to the view."""
        self.hub.publish(json.dumps({"m": mid, "g": gateways, "n": summary, "e": error, "w": latency.WINDOW,
                                     "sites": [[name, label, host] for name, label, host in latency.SITES]},
                                    separators=(",", ":")), "lat")

    def set_ports_net(self, mid: str, key: str | None) -> None:
        """A remote machine's Ports page shows only this adapter's traffic (its key), or every adapter's."""
        for r in self.remotes:
            if r.machine.id == mid:
                r.ports_net = key
                r.tell()

    def remote_port(self, mid: str, port: int) -> dict:
        r = next((r for r in self.remotes if r.machine.id == mid), None)
        if r is None:
            raise KeyError(mid)
        return r.port_details(port)

    def wants_io(self, mid: str) -> bool:
        """Whether a machine should send its disks' IOPS, latency and so on: if there are few, or it's looked at."""
        return mid == self.focus or len(self.remotes) <= 16 and not self.numbers

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
            entry = self._pending.setdefault(mid, [version, []])
            if entry[0] != version:
                entry[0], entry[1] = version, []
            entry[1].append(_pack(t, cpu, nets, disks))

    def remote_memory(self, mid: str, d: dict) -> None:
        with self._lock:
            if all(r.machine.id != mid for r in self.remotes):
                return
            last = self._memory.get(mid, {})
            # Parts that come only now and then are kept for viewers that arrive later.
            kept = {k: v for k, v in (last.get("ram") or {}).items() if k in ("dimms", "phys")}
            self._memory[mid] = {**d, "ram": {**kept, **(d.get("ram") or {})}}
        self.hub.publish(json.dumps({"id": mid, **d}, separators=(",", ":")), "rmem")

    def memory_now(self) -> list[dict]:
        """The latest memory of every remote machine, for a view that has just opened."""
        with self._lock:
            return [{"id": mid, **d} for mid, d in self._memory.items()]

    def apply_settings(self) -> None:
        """Watch the machines the settings list now."""
        with self._remotes_lock:
            wanted = self.settings.list()
            old = {r.machine.id: r for r in self.remotes}
            new = []
            for c in wanted:
                r = old.get(c["id"])
                if r and (r.host, r.port) == (c["address"], c["port"]):
                    r.rename(c)
                    if r.machine.state != "ok":
                        r.machine.os = c["os"]
                else:
                    r = self._demo.agent(c) if self._demo else RemoteAgent(c, self)
                    if not self._demo:
                        r.start()
                new.append(r)
            self.remotes = new
            keep = {r for r in new}
            for r in old.values():
                if r not in keep:
                    r.stop()
            with self._lock:
                for mid in [k for k in self._memory if all(r.machine.id != k or r is not old.get(k) for r in new)]:
                    del self._memory[mid]
            if self.focus and all(r.machine.id != self.focus for r in new):
                self.focus = None
            self.remote_scans.keep({r.machine.id for r in new})
        self.meta_changed()

    def start(self) -> None:
        threading.Thread(target=self._run, name="perf", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.remote_scans.stop()
        for r in self.remotes:
            r.stop()

    # --- the loop ----------------------------------------------------------------
    def _run(self) -> None:
        try:
            _k32.SetThreadPriority(_k32.GetCurrentThread(), 1)   # above normal: steady timing
        except OSError:
            pass
        source = self._demo = DemoPerf(self) if self.mode == "demo" else None
        if source is None:
            source = LocalPerf(self)
        self.apply_settings()
        next_t = time.perf_counter()
        last_meta = 0.0
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
            source.look()
            t = now()
            local = source.sample(t)
            with self._lock:
                pending, self._pending = self._pending, {}
            # Before the samples it describes. With many machines something changes all the
            # time, so it's held back a little: up to a second with a hundred of them.
            hold = min(1.0, 0.01 * len(self.remotes))
            if (self._meta_dirty and mono - last_meta >= hold) or mono - last_meta >= 2:
                self._meta_dirty = False
                last_meta = mono
                self.hub.publish(json.dumps(self.meta(), separators=(",", ":")), "perfmeta")
            machines = {"local": [self.local.version_no, [local] if local else []]}
            machines.update(pending)
            self.hub.publish(json.dumps({"t": round(t, 4), "m": machines}, separators=(",", ":")), "perf")

    def meta(self) -> dict:
        machines = [self.local] + [r.machine for r in self.remotes]
        return {"interval": self.interval, "source": self.source, "sourceNote": self.source_note,
                "problem": self.settings.problem, "machines": [m.meta() for m in machines]}


REMOTE_PORTS_MS = 200          # how often a remote machine's Ports page gets its traffic


def _pack(t: float, cpu, nets, disks) -> list:
    return [round(t, 4), [round(x, 1) for x in cpu],
            [[round(a), round(b), round(c)] for a, b, c in nets],
            [[round(x, n) for x, n in zip(v, (1, 0, 0, 1, 1, 3, 3, 2, 1))] for v in disks]]


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

DEMO_MACHINES = [
    {"name": "web-01", "address": "10.0.0.41", "port": 7820, "os": "linux"},
    {"name": "build-server", "address": "10.0.0.43", "port": 7820, "os": "windows"},
    {"name": "backup-nas", "address": "10.0.0.50", "port": 7820, "os": "linux"},
    {"name": "lab-pc", "address": "10.0.0.61", "port": 7820, "os": "windows"},
]


def demo_disk(rng, load: float, reading: float, spinning: bool) -> list[float]:
    """Made-up figures for a disk: load 0 to 1, what share of it is reads, and whether it's a slow one."""
    top, ms = (220, 7.0) if spinning else (14000, 0.18)
    n = max(0.0, load * top * rng.uniform(0.8, 1.2))
    r, w = n * reading, n * (1 - reading)
    size = 262144 if spinning else 24576 + 40000 * load
    rl, wl = ms * (1 + 2 * load) * rng.uniform(0.8, 1.2), 0.6 * ms * (1 + 3 * load) * rng.uniform(0.8, 1.2)
    queue = (r * rl + w * wl) / 1000
    busy = min(100.0, 100 * (1 - math.exp(-1.4 * queue)))
    share = r * rl / (r * rl + w * wl) if n else 0.5
    return [busy, r * size, w * size, r, w, rl, wl, queue, busy * share]


class DemoAgent:
    """A made-up remote machine: samples come from DemoPerf, memory from sysmon.DemoSystem."""

    # The local demo's programs, as a Linux machine's
    LINUX = {"chrome": "firefox", "msedge": "curl", "Spotify": "rclone", "OneDrive": "syncthing",
             "svchost": "systemd-resolved", "ms-teams": "zoom", "Code": "code", "steam": "steam",
             "postgres": "postgres", "System": "kernel", "OUTLOOK": "thunderbird", "ssh": "ssh", "TermService": "sshd"}

    def __init__(self, config: dict, mon: PerfMonitor, number: int) -> None:
        self.config, self.mon, self.number = config, mon, number
        self.host, self.port = config["address"], config["port"]
        m = self.machine = Machine(config["id"], config["name"] or self.host)
        m.os = config["os"] or "linux"
        m.address = endpoint_text(self.host, self.port)
        m.threads = (4, 16, 8, 8)[number % 4]
        m.detail = f"{threads_text(m.threads)} · {m.address}"
        m.system = "Ubuntu 24.04 LTS" if m.os == "linux" else "Windows Server 2025"
        m.kernel, m.version, m.memory = ("6.8.0" if m.os == "linux" else "10.0.26100"), "demo", True
        m.ports, m.dynamic = True, [32768, 60999] if m.os == "linux" else [49152, 65535]
        m.ports_nets = m.procio = True
        self.ports_net = None                              # the adapter its Ports page shows
        self.procs = DemoProcs(mon, m.id, m.os == "linux")
        self.traffic = self.details = None                 # made-up port traffic, while its Ports page shows
        self.traffic_at = self.traffic_idle = 0.0
        if number == 0:                                    # and this one is scanned now and then
            self.scans = netwatch.DemoScans(mon.remote_scans.get(m.id))
            self.scans.start()
        m.ip = self.host
        self.load = [random.uniform(1, 10) for _ in range(m.threads)]
        self.tracker = NetTracker()
        self.echo = DemoEcho(mon, m.id)
        self.t0 = now()
        self.memory = None
        self.mem_at = 0.0
        if m.os == "linux":
            m.disks = [Item("nvme0n1"), Item("sda"), Item("sdb")]
            for i, detail in zip(m.disks, ("Simulated SSD · 1 TB", "Simulated disk · 8 TB", "Simulated disk · 8 TB")):
                i.detail = detail
        else:
            m.disks = [Item("C", "C:"), Item("D", "D:")]
            for i, (detail, used, size) in zip(m.disks, (("Windows", 210e9, 512e9), ("Data", 1.1e12, 2e12))):
                i.detail, i.used, i.size = detail, int(used), int(size)

    def stop(self) -> None:
        if self.traffic:
            self.traffic[1].stop()
        if getattr(self, "scans", None):
            self.scans.stop()

    def tell(self) -> None:
        pass

    def port_details(self, port: int) -> dict:
        if self.details is None:
            raise AgentError("its Ports page isn't open")
        return self.details(port)

    def _ports(self, t: float) -> None:
        """Made-up traffic per port, the local demo's, while its Ports page shows."""
        if not self.mon.wants_ports(self.machine.id):
            if self.traffic and t - self.traffic_idle > 10:
                self.traffic[1].stop()
                self.traffic = self.details = None
            return
        self.traffic_idle = t
        if self.traffic is None:
            import server                                  # the local demo's traffic, which needs no more
            acc = server.Accumulator()
            source, seen = server.Demo(acc, (("eth0", 6), ("vpn", 2))), netwatch.PortSeen()
            source.start()
            self.traffic, self.details = (acc, source, seen), netwatch.DemoDetails(source, seen)
            if self.machine.os == "linux":                 # its programs, not Windows'
                names = {pid: self.LINUX.get(n, n.lower()) for pid, n in source.PROCS.items()}
                source.names = lambda pid: names.get(pid, "")
                windows = self.details

                def details(port: int) -> dict:
                    d = windows(port)
                    for p in d["procs"]:
                        name = self.LINUX.get(p["name"], p["name"].lower())
                        p.update(name=name, path=f"/usr/bin/{name}" if p["pid"] != 4 else "", desc="", company="",
                                 user="root" if p["pid"] in (4, 1105, 1113) else "demo",
                                 parent={"pid": 1, "name": "systemd"}, services=[f"{name}.service"] if p["services"] else [])
                    return d
                self.details = details
            self.traffic_at = t
        if t - self.traffic_at < REMOTE_PORTS_MS / 1000:
            return
        acc, source, seen = self.traffic
        dt, self.traffic_at = t - self.traffic_at, t
        acc.watch([self.ports_net])
        ports, own, (b_in, b_out, own_in, own_out, _) = acc.drain(self.ports_net)
        seen.note(ports, time.monotonic())
        rows = [[p, e[0], e[1], e[2], e[3], source.names(e[4])] for p, e in ports.items()]
        own_rows = [[p, e[0], e[1], e[2], e[3]] for p, e in own.items()]
        self.mon.hub.publish(json.dumps({"id": self.machine.id, "dt": round(dt, 4), "t": REMOTE_PORTS_MS / 1000,
                                         "in": b_in, "out": b_out, "si": own_in, "so": own_out, "p": rows, "s": own_rows},
                                        separators=(",", ":")), "rports")

    def rename(self, config: dict) -> None:
        self.config = config
        self.machine.name = config["name"] or self.host
        self.mon.meta_changed()

    def sample(self, t: float) -> None:
        m, el, rng = self.machine, t - self.t0, random
        self._ports(t)
        if self.number % 4 == 3 and el % 60 > 12:          # this one comes and goes
            if m.state != "down":
                m.state, m.status = "down", f"No answer from {self.host}"
                self.mon.meta_changed()
            return
        if m.state != "ok":
            m.state, m.status = "ok", ""
            m.version_no += 1
            self.mon.meta_changed()
        burst = max(0.0, math.sin(el / (7 + 2 * self.number))) ** 3
        self.load = [min(100.0, max(0.0, x + (6 + 60 * burst * (1 if i % 3 == 0 else 0.4) - x) * 0.08 + rng.gauss(0, 5)))
                     for i, x in enumerate(self.load)]
        down = max(0.0, 3e8 * max(0.0, math.sin(el / 11 + self.number)) ** 4 + rng.gauss(1e6, 4e5))
        vpn = max(0.0, 4e6 * math.sin(el / 9) + rng.gauss(0, 2e5)) if 12 < el % 90 < 70 else 0.0
        unplugged = self.number % 4 == 1 and 30 < el % 75 < 48
        found = [{"key": "eth0", "name": "enp1s0" if m.os == "linux" else "Ethernet", "card": True, "link": 1e9, "bps": down},
                 {"key": "vpn", "name": "wg0" if m.os == "linux" else "Tailscale", "card": False,
                  "link": 0.0 if 12 < el % 90 < 70 else -1.0, "bps": vpn}]
        if self.number % 4 == 1:
            found.append({"key": "eth1", "name": "Storage network", "card": True, "link": -1.0 if unplugged else 1e10, "bps": 0.0})
        if self.tracker.update(t, found):
            names = {a["key"]: a["name"] for a in found}
            before = [i.key for i in m.nets]
            m.nets = [Item(k, names[k]) for k in self.tracker.shown()]
            for i in m.nets:
                i.lost, i.virtual = self.tracker.lost(i.key), i.key in ("v", "vpn")
            if [i.key for i in m.nets] != before:
                m.version_no += 1
            self.mon.meta_changed()
        rates = {"eth0": [down, down * 0.05 + rng.uniform(1e4, 2e5), 1e9], "vpn": [vpn, vpn * 0.3, found[1]["link"]],
                 "eth1": [0.0, 0.0, -1.0] if unplugged else [max(0.0, rng.gauss(4e8, 1e8)) * burst, 2e7 * burst, 1e10]}
        disks = [demo_disk(rng, 0.2 + 0.8 * burst / (1 + i), 0.15 + 0.1 * i, i > 0) for i in range(len(m.disks))]
        self.procs.step(t, m, disks)
        self.mon.add_remote(m.id, m.version_no, t - 0.004, [sum(self.load) / len(self.load)] + self.load,
                            [rates[i.key] for i in m.nets], disks)
        self.echo.step(t, m)
        if t >= self.mem_at:
            import sysmon
            self.memory = self.memory or sysmon.DemoSystem(remote=self.number, linux=m.os == "linux")
            self.mem_at = t + 1.0
            d = self.memory.sample(1.0)
            d.update(t=round(t, 3), iv=1000, os=m.os)
            self.mon.remote_memory(m.id, d)


class DemoPerf:
    """Made-up numbers: a 16-thread PC with a VPN that comes and goes, and remote machines."""

    def __init__(self, mon: PerfMonitor) -> None:
        self.mon, self.rng = mon, random.Random()
        m = mon.local
        m.threads, m.detail, m.os, m.system = 16, threads_text(16), "windows", "Windows 11 Pro"
        m.kernel, m.memory, m.ports_nets, m.procio = "10.0.26200", True, True, True
        self.procs = DemoProcs(mon, "local", False)
        m.disks = [Item("C:"), Item("D:")]
        for i, (detail, used, size) in zip(m.disks, (("Windows", 410e9, 1e12), ("Data", 2.6e12, 4e12))):
            i.detail, i.used, i.size = detail, int(used), int(size)
        m.silent = ["G: Cloud Drive"]
        m.version_no += 1
        self.tracker = NetTracker()
        self.echo = DemoEcho(mon, "local")
        self.load = [self.rng.uniform(2, 20) for _ in range(16)]
        self.t0 = now()

    def agent(self, config: dict) -> DemoAgent:
        return DemoAgent(config, self.mon, int(config["id"][1:]) - 1)

    def look(self) -> None:
        pass

    def _walk(self, loads, burst):
        for i, x in enumerate(loads):
            target = 8 + 70 * burst * (1 if i % 3 == 0 else 0.3)
            loads[i] = min(100.0, max(0.0, x + (target - x) * 0.08 + self.rng.gauss(0, 6)))
        return loads

    def sample(self, t: float) -> list:
        rng, el, m = self.rng, t - self.t0, self.mon.local
        burst = max(0.0, math.sin(el / 9)) ** 3
        loads = self._walk(self.load, burst)
        cpu = [sum(loads) / len(loads)] + loads
        dl = max(0.0, 4e8 * max(0.0, math.sin(el / 13)) ** 4 + rng.gauss(2e6, 1e6))
        vpn_on, wifi_on = 15 < el % 80 < 60, not 35 < el % 70 < 50
        vpn = max(0.0, 8e6 + 6e6 * math.sin(el / 5) + rng.gauss(0, 1e6)) if vpn_on else 0.0
        found = [{"key": "e", "name": "Ethernet", "card": True, "link": 1e9, "bps": dl},
                 {"key": "w", "name": "Wi-Fi", "card": True, "link": 1.2e9 if wifi_on else -1.0, "bps": 3e5},
                 {"key": "v", "name": "VPN", "card": False, "link": 0.0 if vpn_on else -1.0, "bps": vpn}]
        if self.tracker.update(t, found):
            names = {a["key"]: a["name"] for a in found}
            before = [i.key for i in m.nets]
            m.nets = [Item(k, names[k]) for k in self.tracker.shown()]
            for i in m.nets:
                i.lost, i.virtual = self.tracker.lost(i.key), i.key in ("v", "vpn")
            if [i.key for i in m.nets] != before:
                m.version_no += 1
            self.mon.meta_changed()
        rates = {"e": [dl, dl * 0.04 + rng.uniform(1e5, 5e5), 1e9],
                 "w": [max(0.0, rng.gauss(3e5, 2e5)), max(0.0, rng.gauss(8e4, 4e4)), 1.2e9] if wifi_on else [0.0, 0.0, -1.0],
                 "v": [vpn, vpn * 0.15, found[2]["link"]]}
        disks = [demo_disk(rng, 0.15 + 0.85 * max(0.0, math.sin(el / 17 + 1)) ** 4, 0.2 + 0.6 * max(0.0, math.sin(el / 8)) ** 2, False),
                 demo_disk(rng, 0.1 + 0.5 * max(0.0, math.sin(el / 23)) ** 4, 0.7, True)]
        self.procs.step(t, m, disks)
        for a in self.mon.remotes:
            a.sample(t)
        self.echo.step(t, m)
        return _pack(t, cpu, [rates[i.key] for i in m.nets], disks)


if __name__ == "__main__":
    # A quick look at what this PC would show: python perfmon.py
    class _Hub:
        def viewers(self): return 1
        def publish(self, *a): pass

    class _None:
        problem = ""
        def list(self): return []

    mon = PerfMonitor(_Hub(), "live", _None())
    src = LocalPerf(mon)
    for _ in range(6):
        src.look()
        last = src.sample(now())
        time.sleep(0.5)
    print(mon.local.system, mon.local.kernel, "|", mon.local.detail)
    print(f"CPU {last[1][0]:.1f}%")
    for i, v in zip(mon.local.nets, last[2]):
        print(f"  {i.name}: down {v[0] / 1e6:.2f} Mb/s, up {v[1] / 1e6:.2f} Mb/s, {link_text(v[2])}")
    for i, v in zip(mon.local.disks, last[3]):
        print(f"  {i.name} ({i.detail}): busy {v[0]:.1f}%, {size_text(i.used)} of {size_text(i.size)}")
    print("  without activity counters:", mon.local.silent)

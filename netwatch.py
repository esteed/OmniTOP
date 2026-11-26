"""What's behind a port, and who's probing this machine.

- PortDetails answers GET /api/port?p=N for the pinned-port bubbles: the
  processes using the port (path, description, user, memory, parent, the
  services a svchost runs) and their connections on it. Everything here is
  what netstat and Task Manager show any user, so the elevated capture process
  doesn't hand anything more private (such as command lines) to the window.
- ScanWatch finds port scans: one machine, or several together, working
  through this machine's ports. Capture reports each probe (a packet Windows
  dropped because nothing listens on that port or the firewall blocks it, or
  an accepted connection), and what this machine itself connects to, so that
  replies aren't taken for probes. ScanWatch publishes "scan" events for the
  view.

DemoDetails and DemoScans stand in for both in demo mode.
"""
from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import random
import socket
import struct
import threading
import time
from ctypes import wintypes

AF_INET, AF_INET6 = 2, 23
TCP, UDP = 0, 1
TCP_STATES = {1: "Closed", 2: "Listening", 3: "SYN sent", 4: "SYN received", 5: "Established",
              6: "FIN wait 1", 7: "FIN wait 2", 8: "Close wait", 9: "Closing", 10: "Last ACK",
              11: "Time wait", 12: "Delete TCB"}
LISTEN = 2
MAX_PROCS = 4        # processes described in full per port; the rest are counted
MAX_CONNS = 6        # connections listed per process


# ---------------------------------------------------------------------------
# Win32 (ctypes)
# ---------------------------------------------------------------------------

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)]
_k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_k32.GetProcessTimes.restype = wintypes.BOOL
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
_k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
_k32.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
_k32.QueryDosDeviceW.restype = wintypes.DWORD

_nt = ctypes.WinDLL("ntdll")
_nt.NtQuerySystemInformation.argtypes = [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
_nt.NtQuerySystemInformation.restype = ctypes.c_long

_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_adv.OpenProcessToken.restype = wintypes.BOOL
_adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD)]
_adv.GetTokenInformation.restype = wintypes.BOOL
_adv.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
                                   ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_int)]
_adv.LookupAccountSidW.restype = wintypes.BOOL
_adv.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_adv.OpenSCManagerW.restype = wintypes.HANDLE
_adv.EnumServicesStatusExW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                       ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR]
_adv.EnumServicesStatusExW.restype = wintypes.BOOL
_adv.CloseServiceHandle.argtypes = [wintypes.HANDLE]

_iph = ctypes.WinDLL("iphlpapi")
for _fn in (_iph.GetExtendedTcpTable, _iph.GetExtendedUdpTable):
    _fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG,
                    ctypes.c_int, wintypes.ULONG]
    _fn.restype = wintypes.DWORD
_iph.GetUnicastIpAddressTable.argtypes = [wintypes.USHORT, ctypes.POINTER(ctypes.c_void_p)]
_iph.GetUnicastIpAddressTable.restype = wintypes.DWORD
_iph.FreeMibTable.argtypes = [ctypes.c_void_p]
_iph.FreeMibTable.restype = None

_ver = ctypes.WinDLL("version")
_ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
_ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
_ver.GetFileVersionInfoW.restype = wintypes.BOOL
_ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                ctypes.POINTER(wintypes.UINT)]
_ver.VerQueryValueW.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_USER = 1
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_MORE_DATA = 234
TCP_TABLE_OWNER_PID_ALL = 5
UDP_TABLE_OWNER_PID = 1
FILETIME_UNIX = 116444736000000000   # 1601-01-01 to 1970-01-01 in 100 ns ticks


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32FirstW.restype = wintypes.BOOL
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.restype = wintypes.BOOL


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
        (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize",
                                       "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                                       "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                                       "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]


def _port(raw: bytes, off: int) -> int:
    """A port stored in network byte order in the low half of a DWORD."""
    return (raw[off] << 8) | raw[off + 1]


def _ip(raw: bytes, family: int) -> str:
    text = socket.inet_ntop(socket.AF_INET if family == AF_INET else socket.AF_INET6, raw)
    return text[7:] if text.startswith("::ffff:") and "." in text else text


def _table(fn, family: int, table_class: int) -> bytes:
    size = wintypes.DWORD(0)
    fn(None, ctypes.byref(size), False, family, table_class, 0)
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value + 8192)
        size = wintypes.DWORD(len(buf))
        rc = fn(buf, ctypes.byref(size), False, family, table_class, 0)
        if rc == 0:
            return buf.raw
        if rc != ERROR_INSUFFICIENT_BUFFER:
            return b""
    return b""


def connections() -> list[dict]:
    """Every TCP connection and listener, and every UDP endpoint, with its process."""
    out = []
    raw = _table(_iph.GetExtendedTcpTable, AF_INET, TCP_TABLE_OWNER_PID_ALL)
    if raw:
        for i in range(struct.unpack_from("<I", raw)[0]):
            o = 4 + i * 24
            state, = struct.unpack_from("<I", raw, o)
            pid, = struct.unpack_from("<I", raw, o + 20)
            out.append({"proto": TCP, "state": state, "pid": pid,
                        "lip": _ip(raw[o + 4:o + 8], AF_INET), "lport": _port(raw, o + 8),
                        "rip": _ip(raw[o + 12:o + 16], AF_INET), "rport": _port(raw, o + 16)})
    raw = _table(_iph.GetExtendedTcpTable, AF_INET6, TCP_TABLE_OWNER_PID_ALL)
    if raw:
        for i in range(struct.unpack_from("<I", raw)[0]):
            o = 4 + i * 56
            state, pid = struct.unpack_from("<II", raw, o + 48)
            out.append({"proto": TCP, "state": state, "pid": pid,
                        "lip": _ip(raw[o:o + 16], AF_INET6), "lport": _port(raw, o + 20),
                        "rip": _ip(raw[o + 24:o + 40], AF_INET6), "rport": _port(raw, o + 44)})
    raw = _table(_iph.GetExtendedUdpTable, AF_INET, UDP_TABLE_OWNER_PID)
    if raw:
        for i in range(struct.unpack_from("<I", raw)[0]):
            o = 4 + i * 12
            pid, = struct.unpack_from("<I", raw, o + 8)
            out.append({"proto": UDP, "state": 0, "pid": pid,
                        "lip": _ip(raw[o:o + 4], AF_INET), "lport": _port(raw, o + 4),
                        "rip": "", "rport": 0})
    raw = _table(_iph.GetExtendedUdpTable, AF_INET6, UDP_TABLE_OWNER_PID)
    if raw:
        for i in range(struct.unpack_from("<I", raw)[0]):
            o = 4 + i * 28
            pid, = struct.unpack_from("<I", raw, o + 24)
            out.append({"proto": UDP, "state": 0, "pid": pid,
                        "lip": _ip(raw[o:o + 16], AF_INET6), "lport": _port(raw, o + 20),
                        "rip": "", "rport": 0})
    return out


def snapshot() -> dict[int, tuple[int, str, int]]:
    """pid -> (parent pid, exe name, threads) for every running process."""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return {}
    out = {}
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        ok = _k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            out[pe.th32ProcessID] = (pe.th32ParentProcessID, pe.szExeFile, pe.cntThreads)
            ok = _k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        _k32.CloseHandle(snap)
    return out


def _user(h) -> str:
    tok = wintypes.HANDLE()
    if not _adv.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
        return ""
    try:
        need = wintypes.DWORD(0)
        _adv.GetTokenInformation(tok, TOKEN_USER, None, 0, ctypes.byref(need))
        if not need.value:
            return ""
        buf = ctypes.create_string_buffer(need.value)
        if not _adv.GetTokenInformation(tok, TOKEN_USER, buf, need, ctypes.byref(need)):
            return ""
        sid = ctypes.c_void_p.from_buffer(buf).value   # TOKEN_USER.User.Sid
        name, dom = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        nl, dl, use = wintypes.DWORD(256), wintypes.DWORD(256), ctypes.c_int()
        if not _adv.LookupAccountSidW(None, sid, name, ctypes.byref(nl), dom, ctypes.byref(dl),
                                      ctypes.byref(use)):
            return ""
        return f"{dom.value}\\{name.value}" if dom.value else name.value
    finally:
        _k32.CloseHandle(tok)


_version_cache: dict[str, tuple[str, str]] = {}


def file_description(path: str) -> tuple[str, str]:
    """(FileDescription, CompanyName) from an executable's version resource."""
    if path in _version_cache:
        return _version_cache[path]
    out = ("", "")
    try:
        size = _ver.GetFileVersionInfoSizeW(path, None)
        if size:
            buf = ctypes.create_string_buffer(size)
            if _ver.GetFileVersionInfoW(path, 0, size, buf):
                ptr, ln = ctypes.c_void_p(), wintypes.UINT()
                langs = []
                if _ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr),
                                       ctypes.byref(ln)) and ln.value >= 4:
                    data = ctypes.string_at(ptr.value, ln.value)
                    langs = [struct.unpack_from("<HH", data, i) for i in range(0, ln.value - 3, 4)]
                langs += [(0x0409, 0x04B0), (0x0409, 0x04E4)]

                def value(key: str) -> str:
                    for lang, cp in langs:
                        q = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\{key}"
                        if _ver.VerQueryValueW(buf, q, ctypes.byref(ptr), ctypes.byref(ln)) and ln.value:
                            return ctypes.wstring_at(ptr.value, ln.value).rstrip("\0").strip()
                    return ""

                out = (value("FileDescription"), value("CompanyName"))
    except OSError:
        pass
    _version_cache[path] = out
    return out


class Services:
    """pid -> display names of the running services in that process, cached briefly."""

    def __init__(self) -> None:
        self._at = 0.0
        self._map: dict[int, list[str]] = {}

    def get(self, pid: int) -> list[str]:
        if time.monotonic() - self._at > 15:
            self._map = self._read()
            self._at = time.monotonic()
        return self._map.get(pid, [])

    @staticmethod
    def _read() -> dict[int, list[str]]:
        scm = _adv.OpenSCManagerW(None, None, 0x0004)   # SC_MANAGER_ENUMERATE_SERVICE
        if not scm:
            return {}
        out: dict[int, list[str]] = {}
        try:
            need, count, resume = wintypes.DWORD(0), wintypes.DWORD(0), wintypes.DWORD(0)
            _adv.EnumServicesStatusExW(scm, 0, 0x30, 1, None, 0, ctypes.byref(need),
                                       ctypes.byref(count), ctypes.byref(resume), None)
            if not need.value:
                return {}
            buf = ctypes.create_string_buffer(need.value + 4096)
            resume = wintypes.DWORD(0)
            if not _adv.EnumServicesStatusExW(scm, 0, 0x30, 1, buf, len(buf), ctypes.byref(need),
                                              ctypes.byref(count), ctypes.byref(resume), None):
                return {}
            base = ctypes.addressof(buf)
            for i in range(count.value):
                o = base + i * 56   # ENUM_SERVICE_STATUS_PROCESSW
                display = ctypes.wstring_at(ctypes.c_void_p.from_address(o + 8).value)
                pid = ctypes.c_uint32.from_address(o + 16 + 28).value
                if pid:
                    out.setdefault(pid, []).append(display)
        finally:
            _adv.CloseServiceHandle(scm)
        for names in out.values():
            names.sort(key=str.lower)
        return out


class SYSTEM_PROCESS_ID_INFORMATION(ctypes.Structure):
    _fields_ = [("ProcessId", ctypes.c_void_p), ("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p)]


SystemProcessIdInformation = 88
_devices: dict = {"at": -1e9, "map": []}


def dos_path(nt: str) -> str:
    r"""C:\... for \Device\HarddiskVolume3\..., as drive letters are now."""
    mono = time.monotonic()
    if mono - _devices["at"] > 60:
        found, buf = [], ctypes.create_unicode_buffer(1024)
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if _k32.QueryDosDeviceW(letter + ":", buf, len(buf)):
                found.append((buf.value.lower() + "\\", letter + ":\\"))
        _devices.update(at=mono, map=found)
    low = nt.lower()
    for device, drive in _devices["map"]:
        if low.startswith(device):
            return drive + nt[len(device):]
    return nt


def image_path(pid: int) -> str:
    """Any process's program, asked of Windows rather than of the process, so no rights are needed."""
    buf = ctypes.create_unicode_buffer(1024)
    info = SYSTEM_PROCESS_ID_INFORMATION(pid, 0, ctypes.sizeof(buf), ctypes.addressof(buf))
    if _nt.NtQuerySystemInformation(SystemProcessIdInformation, ctypes.byref(info), ctypes.sizeof(info), None):
        return ""
    return dos_path(ctypes.wstring_at(info.Buffer, info.Length // 2)) if info.Buffer and info.Length else ""


_everyone: dict = {"at": -1e9, "map": {}}


def every_process() -> dict[int, tuple[int, int]]:
    """pid -> (when it started, FILETIME ticks; private bytes) for every process, with no rights needed.

    For the processes this program can't open: other accounts', when it runs as one with few rights.
    """
    mono = time.monotonic()
    if mono - _everyone["at"] < 2:
        return _everyone["map"]
    import sysmon                                  # only needed here, and big
    buf, ret = ctypes.create_string_buffer(1 << 19), wintypes.ULONG()
    for _ in range(4):
        st = _nt.NtQuerySystemInformation(5, buf, len(buf), ctypes.byref(ret)) & 0xFFFFFFFF
        if st != 0xC0000004:                       # STATUS_INFO_LENGTH_MISMATCH
            break
        buf = ctypes.create_string_buffer(max(2 * len(buf), ret.value + (1 << 16)))
    out: dict[int, tuple[int, int]] = {}
    if not st:
        off, size = 0, ctypes.sizeof(sysmon.SYSTEM_PROCESS_INFORMATION)
        while off + size <= ret.value:
            p = sysmon.SYSTEM_PROCESS_INFORMATION.from_buffer(buf, off)
            out[p.UniqueProcessId or 0] = (p.CreateTime, p.PrivatePageCount)
            if not p.NextEntryOffset:
                break
            off += p.NextEntryOffset
    _everyone.update(at=mono, map=out)
    return out


def process_info(pid: int, procs: dict, services: Services) -> dict:
    parent, exe, threads = procs.get(pid, (0, "", 0))
    info = {"pid": pid, "name": os.path.splitext(exe)[0] if exe else "", "path": "", "desc": "",
            "company": "", "started": None, "user": "", "mem": None, "threads": threads,
            "parent": None, "services": services.get(pid)}
    if pid == 0:
        info.update(name="System Idle Process")
        return info
    if pid == 4:
        info.update(name="System", desc="NT Kernel & System", user="NT AUTHORITY\\SYSTEM")
        return info
    if parent and parent in procs:
        info["parent"] = {"pid": parent, "name": os.path.splitext(procs[parent][1])[0]}
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:                                      # another account's, say: what can be found out without opening it
        info["path"] = image_path(pid)
        if info["path"]:
            info["desc"], info["company"] = file_description(info["path"])
        ticks, private = every_process().get(pid, (0, None))
        if ticks > FILETIME_UNIX:
            info["started"] = round((ticks - FILETIME_UNIX) / 1e7)
        info["mem"] = private
        return info
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            info["path"] = buf.value
            info["name"] = info["name"] or os.path.splitext(os.path.basename(buf.value))[0]
            info["desc"], info["company"] = file_description(buf.value)
        t = [wintypes.FILETIME() for _ in range(4)]
        if _k32.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
            ticks = (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
            if ticks > FILETIME_UNIX:
                info["started"] = round((ticks - FILETIME_UNIX) / 1e7)
        pmc = PROCESS_MEMORY_COUNTERS_EX()
        pmc.cb = ctypes.sizeof(pmc)
        if _k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            info["mem"] = pmc.PrivateUsage
        info["user"] = _user(h)
    finally:
        _k32.CloseHandle(h)
    return info


# ---------------------------------------------------------------------------
# Port details
# ---------------------------------------------------------------------------

class PortSeen:
    """Which processes have sent or received on each port lately, from capture."""

    KEEP = 120.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[int, dict[int, float]] = {}
        self._pruned = time.monotonic()

    def note(self, ports: dict, now: float) -> None:
        """ports: the accumulator's {port: [..., last pid]} for one tick."""
        with self._lock:
            for p, e in ports.items():
                pid = e[4]
                if pid:
                    d = self._seen.get(p)
                    if d is None:
                        d = self._seen[p] = {}
                    d[pid] = now
            if now - self._pruned > 10:
                self._pruned = now
                old = now - self.KEEP
                for p in list(self._seen):
                    d = {pid: t for pid, t in self._seen[p].items() if t >= old}
                    if d:
                        self._seen[p] = d
                    else:
                        del self._seen[p]

    def pids(self, port: int) -> dict[int, float]:
        """pid -> seconds since that process last used the port."""
        now = time.monotonic()
        with self._lock:
            return {pid: now - t for pid, t in self._seen.get(port, {}).items()}


def _addr(ip: str, port: int) -> str:
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def _conn_row(c: dict, port: int) -> dict:
    return {"proto": "TCP" if c["proto"] == TCP else "UDP",
            "local": _addr(c["lip"], c["lport"]),
            "remote": _addr(c["rip"], c["rport"]) if c["rport"] else "",
            "state": TCP_STATES.get(c["state"], "") if c["proto"] == TCP else "",
            "end": "local" if c["lport"] == port else "remote"}


def _order(c: dict) -> tuple:
    # Established first, then other live states, listeners, then closing ones.
    s = c["state"]
    return (0 if s == 5 else 2 if s == LISTEN else 3 if s in (11, 12) else 1, c["rip"], c["rport"])


def describe(port: int, conns: list[dict], seen: dict[int, float], info) -> dict:
    """The JSON for one port: info(pid) describes a process."""
    on_port = [c for c in conns if c["lport"] == port or c["rport"] == port]
    listening = [{"proto": "TCP" if c["proto"] == TCP else "UDP", "addr": c["lip"], "pid": c["pid"]}
                 for c in on_port if c["lport"] == port and (c["state"] == LISTEN or c["proto"] == UDP)]
    by_pid: dict[int, list[dict]] = {}
    for c in on_port:
        by_pid.setdefault(c["pid"], []).append(c)
    # Processes seen moving data on this port come first, most recent first,
    # then the others with connections on it, most connections first.
    order = sorted(seen, key=lambda pid: seen[pid])
    order += sorted((pid for pid in by_pid if pid not in seen), key=lambda pid: -len(by_pid[pid]))
    procs = []
    for pid in order[:MAX_PROCS]:
        p = info(pid)
        mine = sorted(by_pid.get(pid, []), key=_order)
        p["seen"] = round(seen[pid], 1) if pid in seen else None
        p["nconns"] = len(mine)
        p["conns"] = [_conn_row(c, port) for c in mine[:MAX_CONNS]]
        procs.append(p)
    return {"port": port, "listening": listening, "procs": procs,
            "more": max(0, len(order) - MAX_PROCS), "time": round(time.time())}


class PortDetails:
    def __init__(self, seen: PortSeen) -> None:
        self.seen = seen
        self.services = Services()

    def __call__(self, port: int) -> dict:
        procs = snapshot()
        return describe(port, connections(), self.seen.pids(port),
                        lambda pid: process_info(pid, procs, self.services))


class DemoDetails:
    """Plausible details for the demo's made-up processes and connections."""

    APPS = {
        "chrome": (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "Google Chrome", "Google LLC", 612e6),
        "msedge": (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "Microsoft Edge",
                   "Microsoft Corporation", 388e6),
        "Spotify": (r"C:\Users\demo\AppData\Roaming\Spotify\Spotify.exe", "Spotify", "Spotify Ltd", 240e6),
        "OneDrive": (r"C:\Program Files\Microsoft OneDrive\OneDrive.exe", "Microsoft OneDrive",
                     "Microsoft Corporation", 96e6),
        "svchost": (r"C:\Windows\System32\svchost.exe", "Host Process for Windows Services",
                    "Microsoft Corporation", 22e6),
        "ms-teams": (r"C:\Program Files\WindowsApps\MSTeams\ms-teams.exe", "Microsoft Teams",
                     "Microsoft Corporation", 450e6),
        "Code": (r"C:\Users\demo\AppData\Local\Programs\Microsoft VS Code\Code.exe", "Visual Studio Code",
                 "Microsoft Corporation", 530e6),
        "steam": (r"C:\Program Files (x86)\Steam\steam.exe", "Steam", "Valve Corporation", 180e6),
        "OUTLOOK": (r"C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE", "Microsoft Outlook",
                    "Microsoft Corporation", 310e6),
        "ssh": (r"C:\Windows\System32\OpenSSH\ssh.exe", "OpenSSH SSH client", "Microsoft Corporation", 6e6),
        "TermService": (r"C:\Windows\System32\svchost.exe", "Host Process for Windows Services",
                        "Microsoft Corporation", 31e6),
    }
    SERVICES = {1105: ["DNS Client", "Network Location Awareness", "Windows Time"],
                1113: ["Remote Desktop Services"]}

    def __init__(self, demo, seen: PortSeen) -> None:
        self.demo = demo
        self.seen = seen
        self.started = time.time() - 3600 * 5

    def _remote_ip(self, c: dict) -> str:
        r = random.Random(c["l"] * 7919 + c["r"])
        if c["r"] in (5353, 1900):
            return "224.0.0.251" if c["r"] == 5353 else "239.255.255.250"
        if c["pid"] == 1113:
            return f"10.0.0.{r.randint(20, 60)}"
        return f"{r.choice([142, 151, 20, 13, 104, 172])}.{r.randint(0, 255)}.{r.randint(0, 255)}.{r.randint(1, 254)}"

    def _info(self, pid: int) -> dict:
        name = self.demo.PROCS.get(pid, "")
        if pid == 4:
            return {"pid": 4, "name": "System", "path": "", "desc": "NT Kernel & System", "company": "",
                    "started": round(self.started), "user": "NT AUTHORITY\\SYSTEM", "mem": 160e3,
                    "threads": 286, "parent": None, "services": []}
        path, desc, company, mem = self.APPS.get(name, ("", "", "", 50e6))
        r = random.Random(pid)
        return {"pid": pid, "name": name, "path": path, "desc": desc, "company": company,
                "started": round(self.started + r.randint(60, 3 * 3600)),
                "user": "NT AUTHORITY\\NETWORK SERVICE" if name in ("svchost", "TermService") else "DEMO-PC\\demo",
                "mem": round(mem * r.uniform(0.8, 1.2)), "threads": r.randint(12, 90),
                "parent": {"pid": 888, "name": "services"} if name in ("svchost", "TermService")
                else {"pid": 7404, "name": "explorer"},
                "services": self.SERVICES.get(pid, [])}

    def __call__(self, port: int) -> dict:
        conns = []
        for c in list(self.demo.conns):
            local_ip = "0.0.0.0" if c["l"] in (5353, 1900) else "192.168.1.20"
            conns.append({"proto": c["proto"], "state": 5 if c["proto"] == TCP else 0, "pid": c["pid"],
                          "lip": local_ip, "lport": c["l"], "rip": self._remote_ip(c), "rport": c["r"]})
        conns.append({"proto": TCP, "state": LISTEN, "pid": 1113, "lip": "0.0.0.0", "lport": 3389,
                      "rip": "0.0.0.0", "rport": 0})
        return describe(port, conns, self.seen.pids(port), self._info)


# ---------------------------------------------------------------------------
# Port scan detection
# ---------------------------------------------------------------------------

def ignorable(remote: str, local: str = "") -> bool:
    """Probes not worth counting: from this machine's loopback, to broadcast or multicast."""
    try:
        r = ipaddress.ip_address(remote)
        if r.is_loopback or r.is_unspecified or r.is_multicast:
            return True
        if local:
            loc = ipaddress.ip_address(local)
            if loc.is_multicast or loc.is_unspecified or local == "255.255.255.255":
                return True
    except ValueError:
        return True
    return False


def own_addresses() -> set[str]:
    """This machine's own IP addresses, on every adapter; empty if they can't be read."""
    out: set[str] = set()
    table = ctypes.c_void_p()
    if _iph.GetUnicastIpAddressTable(0, ctypes.byref(table)):
        return out
    try:
        # MIB_UNICASTIPADDRESS_TABLE: a count, then (8-byte aligned) rows of 80
        # bytes that start with the address as a SOCKADDR_INET.
        for i in range(ctypes.c_uint32.from_address(table.value).value):
            row = ctypes.string_at(table.value + 8 + 80 * i, 28)
            family, = struct.unpack_from("<H", row)
            if family == AF_INET:
                out.add(_ip(row[4:8], AF_INET))
            elif family == AF_INET6:
                out.add(_ip(row[8:24], AF_INET6))
    finally:
        _iph.FreeMibTable(table)
    return out


def in_order(ports: list[int], stride: int) -> list[int]:
    """The longest run of ports in order, up or down, at most `stride` apart.

    Ports outside the run may come between, so a few that arrive late, or
    others the same machine probes meanwhile, don't break it.
    """
    best: list[int] = []
    for sign in (1, -1):
        runs: dict[int, tuple[int, int]] = {}    # port -> (run length ending here, port before it)
        top = 0
        for p in ports:
            n, before = max(((runs[q][0], q) for q in range(p - sign, p - sign * (stride + 1), -sign) if q in runs),
                            default=(0, 0))
            runs[p] = (n + 1, before)
            if not top or n + 1 > runs[top][0]:
                top = p
        if top and runs[top][0] > len(best):
            best = [top]
            while runs[best[-1]][0] > 1 and len(best) < runs[top][0]:
                best.append(runs[best[-1]][1])
            best.reverse()
    return best


def ordering(ports: list[int]) -> str:
    """"up", "down" or "mixed": the order these ports were probed in, give or take a few late ones."""
    n = len(ports)
    if n < 3:
        return "mixed"
    rank = {p: i for i, p in enumerate(sorted(ports))}
    rho = 1 - 6 * sum((i - rank[p]) ** 2 for i, p in enumerate(ports)) / (n * (n * n - 1))   # Spearman's
    return "up" if rho >= 0.9 else "down" if rho <= -0.9 else "mixed"


class ScanWatch:
    """Finds port scans of this machine: a methodical search through its ports.

    A probe is a packet to a port nothing listens on (Windows drops it, or
    resets the connection), or to one the firewall blocks, or an accepted
    connection. What makes probes a scan is the pattern:

    - One machine, any order: FAST different ports, or over a long time SLOW.
    - One machine, in port order: RUN ports, each at most STRIDE from the one
      before. Order is good evidence, so fewer do.
    - Several machines together, in port order: MANY_RUN ports between them,
      however few each probes.
    - Several machines together, any order: MANY ports between them, at least
      EACH by each machine, that it hadn't just probed before and hardly any of
      the others did (machines sharing out a search; ones looking for the same
      services overlap, and keep coming back for them).

    Each is (ports, seconds). Machines probing a handful of ports don't make a
    scan, however often they do it: only different ports count. At least half
    must be closed ones: a machine that only connects to what's open here is
    using it (an FTP server opens a new port for each transfer, say).

    Much of what Windows drops isn't a probe at all, and is left out:

    - Anything to or from an address that isn't one machine's: broadcasts, and
      this machine's own addresses.
    - Traceroutes: UDP to a short range of ports, in order.
    - Replies to this machine: late packets of connections it closed, resets
      from services that refuse it, answers to UDP it sent. Capture reports
      each connection this machine starts and each port it sends UDP from, and
      the connection tables fill in what was there before. Which ports Windows
      hands out for outgoing connections is a setting, and can be almost all
      of them, so the port number alone can't tell.
    """

    FAST = (25, 60.0)
    SLOW = (40, 900.0)
    RUN = (15, 300.0)
    MANY_RUN = (20, 300.0)
    MANY = ((40, 60.0), (100, 900.0))
    EACH = 5
    STRIDE = 8
    SHARE = 0.6          # of a machine's ports that must be in order for it to count as in order
    DISTINCT = 0.9       # of several machines' ports that must be different ones
    SAME = 0.7           # of a machine's ports that another scan has too, if it's the same scan

    WINDOW = 900.0       # probes are remembered this long: the longest time above
    # A scan has ended once no probe has come for 4 times its usual gap between
    # probes, within these bounds (seconds). A scan that starts again after that
    # is a new scan, with its own alert.
    QUIET_MIN, QUIET_MAX = 3.0, 120.0
    QUIET_MANY = 30.0    # at least this for several machines, as some may wait for others
    KEEP = 900.0         # ended scans stay available to the view this long
    MAX_PENDING = 50000
    MAX_LISTED = 4096    # ports described to the view per scan
    MAX_REMOTES = 4096   # machines kept in mind at once
    MAX_OWN = 100000
    OWN_KEEP = 900.0     # how long this machine's own connections are remembered
    UDP_KEEP = 300.0     # and the ports it sent UDP from
    SERVICE_MAX = 32768  # ports services usually have, below where systems pick client ports
    TRACEROUTE = range(33434, 33535)   # UDP
    STEP = 0.25          # seconds between looks at new probes

    def __init__(self, hub, live: bool = False) -> None:
        """live: read this machine's addresses and connections, to recognize what isn't a probe."""
        self.hub = hub
        self.live = live
        self._own: dict[tuple[str, int], float] = {}   # (remote address, local port) of own connections -> last seen
        self._ends: dict[tuple[str, int], float] = {}  # (remote address, remote port) this machine connected to
        self._own_udp: dict[int, float] = {}           # local port -> when UDP was last sent from it
        self._addresses: set[str] = set()
        self._own_at = self._addresses_at = self._prune_at = self._many_at = 0.0
        self._many_due = False
        self._lock = threading.Lock()
        self._pending: list[tuple] = []
        self._state: dict[str, dict] = {}   # remote -> {(proto, port): rec}, for those not in a scan
        self._scans: dict[str, dict] = {}   # scan id -> scan
        self._live: dict[str, dict] = {}    # remote -> the scan it's part of now
        self._stop = threading.Event()
        self.dropped = 0
        self._dirty: set[str] = set()   # ids of scans to describe to the view
        self._sent = 0.0
        self.known_names: dict[str, str] = {}   # names to show without a reverse lookup

    # Called by capture, on its thread

    def probe(self, remote: str, lport: int, proto: int, kind: str, rport: int = 0, local: str = "",
              t: float | None = None) -> None:
        """Something came to a port; kind is "closed" (dropped) or "open" (accepted). t: when, if not now."""
        if not lport:
            return
        with self._lock:
            if len(self._pending) < self.MAX_PENDING:
                self._pending.append((time.time() if t is None else t, remote, lport, proto, kind, rport, local))
            else:
                self.dropped += 1

    def connecting(self, remote: str, rport: int, lport: int) -> None:
        """This machine is starting a TCP connection."""
        now = time.time()
        with self._lock:
            if lport:
                self._own[remote, lport] = now
            if rport:
                self._ends[remote, rport] = now

    def sending(self, lport: int) -> None:
        """This machine sent UDP from a port."""
        self._own_udp[lport] = time.time()

    # The rest runs on the watcher's own thread

    def start(self) -> None:
        threading.Thread(target=self._run, name="scanwatch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.STEP):
            self.beat()

    def beat(self) -> None:
        """Take in the probes that came since the last beat, and tell the view what changed."""
        try:
            now = time.time()
            urgent, hits = self._step(now)
            if hits:
                self.hub.publish(json.dumps({"h": hits}, separators=(",", ":")), "probe")
            # Scan summaries go out at most once a second, unless one started or ended.
            if self._dirty and (urgent or now - self._sent >= 1.0):
                ids, self._dirty = self._dirty, set()
                self._sent = now
                self._publish(ids)
        except Exception:   # keep watching whatever one bad probe did
            pass

    def _look(self, now: float) -> None:
        """Every couple of seconds, note this machine's addresses and TCP connections."""
        if not self.live:
            return
        if now - self._addresses_at >= 5:
            self._addresses_at = now
            self._addresses = own_addresses()
        if now - self._own_at >= 2:
            self._own_at = now
            tcp = [c for c in connections() if c["proto"] == TCP]
            listening = {c["lport"] for c in tcp if c["state"] == LISTEN}
            with self._lock:
                for c in tcp:
                    if c["rport"]:
                        self._own[c["rip"], c["lport"]] = now
                        if c["lport"] not in listening:
                            self._ends[c["rip"], c["rport"]] = now

    def _prune(self, now: float) -> None:
        with self._lock:
            for name in ("_own", "_ends"):
                known = getattr(self, name)
                keep = self.OWN_KEEP if len(known) <= self.MAX_OWN else self.OWN_KEEP / 10
                setattr(self, name, {k: t for k, t in known.items() if t >= now - keep})
        for port, t in list(self._own_udp.items()):      # sending() may add to it meanwhile
            if t < now - self.UDP_KEEP:
                self._own_udp.pop(port, None)
        for remote, recs in list(self._state.items()):
            recs = {k: r for k, r in recs.items() if r["last"] >= now - self.WINDOW}
            if recs:
                self._state[remote] = recs
            else:
                del self._state[remote]
        if len(self._state) > self.MAX_REMOTES:
            newest = {remote: max(r["last"] for r in recs.values()) for remote, recs in self._state.items()}
            for remote in sorted(newest, key=newest.get)[:len(newest) - self.MAX_REMOTES]:
                del self._state[remote]
        with self._lock:
            for sid, scan in list(self._scans.items()):
                if scan["ended"] and now - scan["last"] > self.KEEP:
                    del self._scans[sid]

    def _reply(self, remote: str, lport: int, proto: int, rport: int, t: float) -> bool:
        """Does this look like a packet for something this machine started?"""
        if 0 < rport < 1024 <= lport:
            return True                                   # from a well-known service
        if proto == UDP:
            return t - self._own_udp.get(lport, 0.0) < self.UDP_KEEP
        return (remote, lport) in self._own or (lport >= 1024 and (remote, rport) in self._ends)

    def _genuine(self, pending: list[tuple]):
        """The probes that aren't to or from this machine itself, or replies to it. With the lock held."""
        for p in pending:
            t, remote, lport, proto, kind, rport, local = p
            if self._addresses and (remote in self._addresses or (local and local not in self._addresses)):
                continue          # to or from this machine itself, or a subnet's broadcast address
            if kind == "open":
                self._own[remote, lport] = t              # what it sends after closing isn't a probe either
            elif self._reply(remote, lport, proto, rport, t):
                continue
            yield p

    def _step(self, now: float) -> tuple[bool, list[int]]:
        """Take in new probes and update the scans.

        Returns whether a scan started or ended, and the ports probed by
        scanning machines since the last step (for the grid to light up). A
        scan that has just been recognized brings all the ports it probed so far.
        """
        self._look(now)
        with self._lock:
            pending, self._pending = self._pending, []
        fresh: set[str] = set()
        hits: set[int] = set()
        with self._lock:
            for t, remote, lport, proto, kind, rport, local in self._genuine(pending):
                scan = self._live.get(remote) or self._carries_on(lport, proto)
                if scan:
                    self._add(scan, remote, (proto, lport), kind, t, 1)
                    hits.add(lport)
                    continue
                if proto == UDP and lport in self.TRACEROUTE:
                    continue
                recs = self._state.setdefault(remote, {})
                rec = recs.get((proto, lport))
                if rec is None:
                    rec = recs[proto, lport] = {"kind": kind, "first": t, "last": t, "n": 0, "rport": rport}
                rec["n"] += 1
                rec["last"] = max(t, rec["last"])
                if kind == "open":
                    rec["kind"] = "open"
                if rec["rport"] != rport:
                    rec["rport"] = -1                     # several
                fresh.add(remote)

            urgent = False
            for remote in fresh:
                seconds = self._one_scans(remote, now) or self._joins(remote, now)
                if seconds:
                    hits.update(self._begin([remote], now - seconds))
                    urgent = True
            self._many_due = self._many_due or bool(fresh)
            if self._many_due and now - self._many_at >= (1.0 if len(self._state) < 500 else 5.0):
                self._many_due, self._many_at = False, now
                group, seconds, ordered = self._many_scan(now)
                if group:
                    hits.update(self._begin(group, now - seconds, ordered))
                    urgent = True
            for sid, scan in self._scans.items():
                if not scan["ended"] and now - scan["last"] > self._quiet(scan):
                    scan["ended"] = urgent = True
                    self._dirty.add(sid)
                    for remote in scan["remotes"]:
                        if self._live.get(remote) is scan:
                            del self._live[remote]        # what it does next may be another scan
        if now - self._prune_at >= 5 or len(self._state) > 2 * self.MAX_REMOTES:
            self._prune_at = now
            self._prune(now)
        return urgent, sorted(hits)

    # Recognizing scans

    def _probed(self, remote: str, now: float) -> dict:
        """What a machine probed lately, unless it looks like replies.

        Replies come from one service's port, to ports Windows may have handed
        out for connections; a scan's probes come from client ports.
        """
        recs = {k: r for k, r in self._state.get(remote, {}).items() if r["last"] >= now - self.WINDOW}
        ends = {r["rport"] for r in recs.values() if r["kind"] != "open"}
        if len(ends) == 1 and 0 < min(ends) < self.SERVICE_MAX and all(p >= 1024 for _, p in recs):
            return {}
        return recs

    def _one_scans(self, remote: str, now: float) -> float:
        """Is this machine scanning, by itself? The seconds of it that tell, or 0."""
        recs = self._probed(remote, now)
        if len(recs) < min(self.FAST[0], self.SLOW[0], self.RUN[0]):
            return 0
        for need, seconds in (self.FAST, self.SLOW):
            lately = [(p, r["kind"]) for (_, p), r in recs.items() if r["last"] >= now - seconds]
            closed = {p for p, kind in lately if kind != "open"}
            opened = {p for p, kind in lately} - closed
            if len(closed) + min(len(closed), len(opened)) >= need:
                return seconds
        need, seconds = self.RUN
        for proto in (TCP, UDP):
            seq = sorted((r["first"], p) for (pr, p), r in recs.items() if pr == proto and r["first"] >= now - seconds)
            if len(seq) < need:
                continue
            run = in_order([p for _, p in seq], self.STRIDE)
            closed = sum(recs[proto, p]["kind"] != "open" for p in run)
            if len(run) >= max(need, self.SHARE * len(seq)) and closed * 2 >= len(run):
                return seconds
        return 0

    def _joins(self, remote: str, now: float) -> float:
        """Is this machine probing its share of ports, while others share out a scan? The seconds of that, or 0."""
        if len(self._state[remote]) < self.EACH:
            return 0
        scan = next((s for s in self._scans.values() if not s["ended"] and len(s["remotes"]) > 1), None)
        if scan is None:
            return 0
        ports = {p for (_, p), r in self._probed(remote, now).items()
                 if r["kind"] != "open" and r["first"] >= scan["first"]}
        others = {p for _, p in scan["count"]}
        joins = len(ports) >= self.EACH and len(ports - others) >= self.DISTINCT * len(ports)
        return now - scan["first"] if joins else 0

    def _many_scan(self, now: float) -> tuple[list[str], float, bool]:
        """The machines scanning together, if any, though none is by itself.

        With the seconds of it that tell, and whether it's in port order.
        """
        probed = {}
        for remote in self._state:
            recs = {k: r for k, r in self._probed(remote, now).items() if r["kind"] != "open"}
            if recs:
                probed[remote] = recs
        if len(probed) < 2:
            return [], 0, False
        # In port order between them
        need, seconds = self.MANY_RUN
        for proto in (TCP, UDP):
            first: dict[int, tuple[float, str]] = {}      # port -> (when, who) first probed it
            for remote, recs in probed.items():
                for (pr, p), r in recs.items():
                    if pr == proto and r["first"] >= now - seconds and (p not in first or r["first"] < first[p][0]):
                        first[p] = (r["first"], remote)
            if len(first) < need:
                continue
            run = in_order([p for _, p in sorted((t, p) for p, (t, _) in first.items())], self.STRIDE)
            group = {first[p][1] for p in run}
            theirs = sum(who in group for _, who in first.values())
            if len(run) >= max(need, self.SHARE * theirs) and len(group) > 1:
                return sorted(group), seconds, True
        # Sharing out ports in any order
        for need, seconds in self.MANY:
            sets = {}
            for remote, recs in probed.items():
                ports = {p for (_, p), r in recs.items() if r["first"] >= now - seconds}
                if len(ports) >= self.EACH:
                    sets[remote] = ports
            if len(sets) > 1:
                different = len(set().union(*sets.values()))
                if different >= max(need, self.DISTINCT * sum(map(len, sets.values()))):
                    return sorted(sets), seconds, False
        return [], 0, False

    # Keeping track of scans

    def _begin(self, remotes: list[str], since: float, ordered: bool = False) -> set[int]:
        """Start a scan by these machines, or have them join the one under way they seem to be part of.

        That's one that several machines are making, or one of the same ports
        (a scanner can make up more source addresses, to hide its own). It
        begins with what they probed since the given time. Returns those ports.
        """
        probes = sorted(((r["first"], remote, key, r) for remote in remotes
                         for key, r in self._state.pop(remote).items() if r["last"] >= since), key=lambda x: x[0])
        keys = {key for _, _, key, _ in probes}
        scan = next((s for s in self._scans.values() if not s["ended"] and (
            len(s["remotes"]) > 1 or len(keys & s["count"]) >= self.SAME * len(keys))), None)
        if scan is None:
            first = max(since, probes[0][0])
            sid = f"{remotes[0]}@{first:.3f}"
            while sid in self._scans:
                sid += "+"
            scan = self._scans[sid] = {"id": sid, "first": first, "last": first, "ended": False, "n": 0,
                                       "ports": {}, "count": set(), "remotes": {}, "edge": {}, "ordered": False}
        scan["ordered"] = scan["ordered"] or ordered
        for first, remote, key, r in probes:
            self._add(scan, remote, key, r["kind"], r["last"], r["n"], first)
        return {p for _, p in keys}

    def _add(self, scan: dict, remote: str, key: tuple[int, int], kind: str, t: float, n: int,
             first: float | None = None) -> None:
        """Add probes by one machine, of one port, to a scan."""
        who = scan["remotes"].get(remote)
        if who is None:
            who = scan["remotes"][remote] = {"name": self.known_names.get(remote, ""), "ports": set()}
            self._live[remote] = scan
            if not who["name"] and len(scan["remotes"]) <= 16:
                threading.Thread(target=self._resolve, args=(scan, remote), daemon=True).start()
        who["ports"].add(key)
        scan["count"].add(key)
        scan["edge"][key[0]] = key[1]
        rec = scan["ports"].get(key)
        if rec is not None:
            if kind == "open":
                rec["kind"] = "open"
        elif len(scan["ports"]) < self.MAX_LISTED:
            scan["ports"][key] = {"kind": kind, "first": t if first is None else first}
        scan["n"] += n
        scan["last"] = max(scan["last"], t)
        self._dirty.add(scan["id"])

    def _carries_on(self, lport: int, proto: int) -> dict | None:
        """The scan several machines are making in port order that has got to about this port."""
        for scan in self._scans.values():
            if scan["ordered"] and not scan["ended"] and len(scan["remotes"]) < self.MAX_REMOTES:
                edge = scan["edge"].get(proto)
                if edge is not None and 0 < abs(lport - edge) <= 2 * self.STRIDE:
                    return scan
        return None

    def _quiet(self, scan: dict) -> float:
        """How long without probes before this scan counts as over."""
        gap = (scan["last"] - scan["first"]) / max(1, scan["n"] - 1)
        least = self.QUIET_MANY if len(scan["remotes"]) > 1 else self.QUIET_MIN
        return min(self.QUIET_MAX, max(least, 4 * gap))

    def _resolve(self, scan: dict, remote: str) -> None:
        try:
            name = socket.gethostbyaddr(remote)[0]
        except (OSError, UnicodeError):
            return
        with self._lock:
            scan["remotes"][remote]["name"] = name
        self._publish([scan["id"]])

    @staticmethod
    def _view(scan: dict) -> dict:
        ports = sorted(scan["ports"].items(), key=lambda kv: (kv[0][1], kv[0][0]))
        firsts = sorted((r["first"], key) for key, r in scan["ports"].items())
        orders = {ordering([p for _, (pr, p) in firsts if pr == proto]) for proto in {key[0] for _, key in firsts}}
        who = sorted(scan["remotes"].items(), key=lambda kv: -len(kv[1]["ports"]))
        return {"id": scan["id"], "machines": len(who),
                "remotes": [[ip, w["name"], len(w["ports"])] for ip, w in who[:16]],
                "first": round(scan["first"], 1), "last": round(scan["last"], 1), "ended": scan["ended"],
                "probes": scan["n"], "total": len(scan["count"]),
                "order": orders.pop() if len(orders) == 1 else "mixed",
                "ports": [[p, "TCP" if proto == TCP else "UDP", r["kind"]] for (proto, p), r in ports]}

    def views(self, ids=None) -> list[dict]:
        with self._lock:
            return [self._view(s) for sid, s in self._scans.items() if ids is None or sid in ids]

    def _publish(self, ids) -> None:
        scans = self.views(set(ids))
        if scans:
            self.hub.publish(json.dumps({"scans": scans, "now": round(time.time(), 1)},
                                        separators=(",", ":")), "scan")


class ProbeFilter(ScanWatch):
    """An agent's part: the probes a ScanWatch would count, sent on for one elsewhere to judge.

    Leaving out replies takes this machine's own connections, which only it can know. Telling
    scans from the rest is left to the viewer, so that it can change without the agent.
    """

    def __init__(self) -> None:
        super().__init__(None, live=True)

    def take(self) -> list[list]:
        """The genuine probes since the last call: [time, remote, local port, protocol, kind, remote port, local]."""
        now = time.time()
        self._look(now)
        with self._lock:
            pending, self._pending = self._pending, []
            out = [[round(p[0], 3), *p[1:]] for p in self._genuine(pending)]
        if now - self._prune_at >= 5:
            self._prune_at = now
            self._prune(now)
        return out


class DemoScans:
    """Now and then, a made-up scan of the demo machine, or a machine that only looks for a few services."""

    TOP_TCP = [21, 22, 23, 25, 53, 80, 81, 88, 110, 111, 113, 119, 135, 139, 143, 179, 199, 389, 427,
               443, 444, 445, 465, 513, 514, 515, 543, 544, 548, 554, 587, 631, 646, 873, 990, 993, 995,
               1025, 1026, 1027, 1028, 1029, 1110, 1433, 1720, 1723, 1755, 1900, 2000, 2001, 2049, 2121,
               2717, 3000, 3128, 3306, 3389, 3986, 4899, 5000, 5009, 5051, 5060, 5101, 5190, 5357, 5432,
               5631, 5666, 5800, 5900, 6000, 6001, 6646, 7070, 8000, 8008, 8009, 8080, 8081, 8443, 8888,
               9100, 9999, 10000, 32768, 49152, 49153, 49154, 49155, 49156, 49157]
    TOP_UDP = [53, 67, 69, 123, 137, 138, 161, 500, 514, 520, 1434, 1900, 4500, 5353]
    OPEN = {135, 139, 445, 3389, 5357}
    SOURCES = [("10.0.0.66", "scanner.lan"), ("172.16.4.9", "pentest-vm.local"), ("192.168.1.23", "nas.lan")]

    def __init__(self, watch: ScanWatch) -> None:
        self.watch = watch
        self._stop = threading.Event()
        self.rng = random.Random()

    def start(self) -> None:
        threading.Thread(target=self._run, name="demo-scans", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _plan(self, n: int) -> tuple[list[tuple[str, int, int]], float]:
        """What's probed this time, as (address, port, protocol), and over how many seconds."""
        rng = self.rng
        if n % 4 == 0:     # a sweep of every well-known port, in order
            return [(self.SOURCES[0][0], p, TCP) for p in range(1, 1025)], rng.uniform(18, 26)
        if n % 4 == 1:     # a machine looking for a few services: no scan, so no alert
            return [(self.SOURCES[2][0], p, TCP) for p in rng.sample(self.TOP_TCP, 12)], 6.0
        if n % 4 == 2:     # the usual suspects, in random order
            probes = [(p, TCP) for p in self.TOP_TCP] + [(p, UDP) for p in rng.sample(self.TOP_UDP, 6)]
            rng.shuffle(probes)
            return [(self.SOURCES[1][0], p, proto) for p, proto in probes], rng.uniform(8, 14)
        # several machines taking turns, in order
        return [(f"203.0.113.{10 + k % 5}", 8000 + k, TCP) for k in range(200)], rng.uniform(14, 20)

    def _run(self) -> None:
        for remote, name in self.SOURCES:
            self.watch.known_names[remote] = name
        for k in range(5):
            self.watch.known_names[f"203.0.113.{10 + k}"] = f"host{k + 1}.example"
        wait, n = 25.0, 0
        while not self._stop.wait(wait):
            probes, seconds = self._plan(n)
            n += 1
            for remote, p, proto in probes:
                kind = "open" if proto == TCP and p in self.OPEN else "closed"
                self.watch.probe(remote, p, proto, kind, self.rng.randint(40000, 60000))
                if self._stop.wait(seconds / len(probes)):
                    return
            wait = 20.0 if n % 4 == 2 else self.rng.uniform(120, 200)

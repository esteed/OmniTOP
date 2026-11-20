"""What's behind a port, and who's probing this machine.

- PortDetails answers GET /api/port?p=N for the pinned-port bubbles: the
  processes using the port (path, description, user, memory, parent, the
  services a svchost runs) and their connections on it. Everything here is
  what netstat and Task Manager show any user, so the elevated capture process
  doesn't hand anything more private (such as command lines) to the window.
- ScanWatch finds port scans: one address sending to many different ports
  of this machine within a short time. Capture reports each probe (a packet
  Windows dropped because nothing listens on that port, a reset Windows sent
  for the same reason, or an accepted connection) and ScanWatch publishes
  "scan" events for the view.

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
from collections import deque
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
    if not h:
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
        if r.is_loopback or r.is_unspecified:
            return True
        if local:
            loc = ipaddress.ip_address(local)
            if loc.is_multicast or loc.is_unspecified or local == "255.255.255.255":
                return True
    except ValueError:
        return True
    return False


class ScanWatch:
    """Finds one address probing many ports of this machine and reports it as a scan.

    A probe is a packet to a port nothing listens on (Windows drops it, or
    resets the connection) or an accepted connection. An address that probes
    MIN_PORTS different ports within WINDOW seconds is scanning.

    Some of those packets are just late: a server answering a connection this
    machine already closed. Those are left out: packets from a server's
    well-known port (below 1024) to a higher port here, and packets from an
    address to a local port that this machine had a connection on between the
    two within OWN_KEEP seconds. Windows may hand out almost any port for
    outgoing connections (here, often 1024-65535), so the port number alone
    can't tell.
    """

    WINDOW = 30.0
    MIN_PORTS = 10
    # A scan has ended once no probe has come for 4 times its usual gap between
    # probes, within these bounds. A scan that starts again after that is a new
    # scan, with its own alert.
    QUIET_MIN, QUIET_MAX = 3.0, 10.0
    KEEP = 900.0         # ended scans stay available to the view this long
    MAX_PENDING = 50000
    MAX_LISTED = 4096
    OWN_KEEP = 180.0     # how long this machine's own connections are remembered
    STEP = 0.25          # seconds between looks at new probes

    def __init__(self, hub, live: bool = False) -> None:
        """live: read this machine's connections, to recognize late packets for them."""
        self.hub = hub
        self.live = live
        self._own: dict[tuple[str, int], float] = {}   # (remote address, local port) -> last seen
        self._own_at = 0.0
        self._lock = threading.Lock()
        self._pending: list[tuple] = []
        self._state: dict[str, dict] = {}   # remote -> {"ports": {(proto, port): rec}, "recent": deque}
        self._scans: dict[str, dict] = {}   # scan id -> scan
        self._current: dict[str, str] = {}  # remote -> id of its latest scan
        self._stop = threading.Event()
        self.dropped = 0
        self._dirty: set[str] = set()   # ids of scans to describe to the view
        self._sent = 0.0
        self.known_names: dict[str, str] = {}   # names to show without a reverse lookup

    def probe(self, remote: str, lport: int, proto: int, kind: str, rport: int = 0, why=None) -> None:
        """Called by capture for each probe; kind is "closed" or "open"."""
        if not lport:
            return
        with self._lock:
            if len(self._pending) < self.MAX_PENDING:
                self._pending.append((time.time(), remote, lport, proto, kind, rport))
            else:
                self.dropped += 1

    def start(self) -> None:
        threading.Thread(target=self._run, name="scanwatch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.STEP):
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

    def _remember_own(self, now: float) -> None:
        """Note this machine's TCP connections every couple of seconds."""
        if not self.live or now - self._own_at < 2:
            return
        self._own_at = now
        for c in connections():
            if c["proto"] == TCP and c["rport"]:
                self._own[(c["rip"], c["lport"])] = now
        old = now - self.OWN_KEEP
        self._own = {k: t for k, t in self._own.items() if t >= old}

    def _late(self, remote: str, lport: int, rport: int) -> bool:
        """Does this look like a late packet for one of this machine's own connections?"""
        return (0 < rport < 1024 <= lport) or (remote, lport) in self._own

    def _step(self, now: float) -> tuple[bool, list[int]]:
        """Take in new probes and update the scans.

        Returns whether a scan started or ended, and the ports probed by
        scanning addresses since the last step (for the grid to light up). A
        scan that has just been recognized brings all the ports it probed so far.
        """
        self._remember_own(now)
        with self._lock:
            pending, self._pending = self._pending, []
        fresh: dict[str, set[int]] = {}
        counts: dict[str, int] = {}
        for t, remote, lport, proto, kind, rport in pending:
            if kind != "open" and self._late(remote, lport, rport):
                continue   # a late packet for one of this machine's own connections
            st = self._state.get(remote)
            if st is None:
                st = self._state[remote] = {"ports": {}, "recent": deque()}
            key = (proto, lport)
            rec = st["ports"].get(key)
            if rec is None:
                rec = st["ports"][key] = {"kind": kind, "first": t, "last": t, "n": 0}
            rec["n"] += 1
            rec["last"] = t
            if kind == "open":
                rec["kind"] = "open"
            st["recent"].append((t, lport))
            fresh.setdefault(remote, set()).add(lport)
            counts[remote] = counts.get(remote, 0) + 1

        urgent = False
        hits: set[int] = set()
        with self._lock:
            for remote, st in list(self._state.items()):
                rq = st["recent"]
                while rq and rq[0][0] < now - self.WINDOW:
                    rq.popleft()
                scan = self._scans.get(self._current.get(remote, ""))
                live = scan is not None and not scan["ended"]
                if not live and len({p for _, p in rq}) >= self.MIN_PORTS:
                    recs = {k: r for k, r in st["ports"].items() if r["last"] >= now - self.WINDOW}
                    first = min(r["first"] for r in recs.values())
                    sid = f"{remote}@{first:.3f}"
                    scan = {"id": sid, "remote": remote, "name": self.known_names.get(remote, ""),
                            "first": first, "last": first, "ended": False, "ports": recs,
                            "n": sum(r["n"] for r in recs.values())}
                    self._scans[sid] = scan
                    self._current[remote] = sid
                    live = urgent = True
                    self._dirty.add(sid)
                    hits.update(p for _, p in recs)
                    if not scan["name"]:
                        threading.Thread(target=self._resolve, args=(scan,), daemon=True).start()
                elif live and remote in fresh:
                    hits.update(fresh[remote])
                    scan["n"] += counts[remote]
                if live:
                    for k, r in st["ports"].items():
                        if r["last"] >= scan["first"] and len(scan["ports"]) < self.MAX_LISTED:
                            scan["ports"][k] = r
                    scan["last"] = max(r["last"] for r in scan["ports"].values())
                    if remote in fresh:
                        self._dirty.add(scan["id"])
                    if now - scan["last"] > self._quiet(scan):
                        scan["ended"] = urgent = True
                        self._dirty.add(scan["id"])
                        # The scan's probes belong to it: only new ones can start another
                        # scan, and its records stop growing (later probes go to that one).
                        scan["ports"] = dict(scan["ports"])
                        rq.clear()
                        st["ports"] = {}
                    else:
                        continue   # keep this address's records while its scan runs
                # Outside a scan, only the last WINDOW seconds matter.
                st["ports"] = {k: r for k, r in st["ports"].items() if r["last"] >= now - self.WINDOW}
                if not st["ports"] and not rq:
                    del self._state[remote]
            for sid, scan in list(self._scans.items()):
                if scan["ended"] and now - scan["last"] > self.KEEP:
                    del self._scans[sid]
                    if self._current.get(scan["remote"]) == sid:
                        del self._current[scan["remote"]]
        return urgent, sorted(hits)

    def _quiet(self, scan: dict) -> float:
        """How long without probes before this scan counts as over."""
        gap = (scan["last"] - scan["first"]) / max(1, scan["n"] - 1)
        return min(self.QUIET_MAX, max(self.QUIET_MIN, 4 * gap))

    def _resolve(self, scan: dict) -> None:
        try:
            name = socket.gethostbyaddr(scan["remote"])[0]
        except (OSError, UnicodeError):
            return
        with self._lock:
            scan["name"] = name
        self._publish([scan["id"]])
    @staticmethod
    def _view(scan: dict) -> dict:
        ports = sorted(scan["ports"].items(), key=lambda kv: (kv[0][1], kv[0][0]))
        return {"id": scan["id"], "remote": scan["remote"], "name": scan["name"],
                "first": round(scan["first"], 1), "last": round(scan["last"], 1), "ended": scan["ended"],
                "probes": scan["n"],
                "ports": [[p, "TCP" if proto == TCP else "UDP", r["kind"]] for (proto, p), r in ports]}

    def views(self, ids=None) -> list[dict]:
        with self._lock:
            return [self._view(s) for sid, s in self._scans.items() if ids is None or sid in ids]

    def _publish(self, ids) -> None:
        scans = self.views(set(ids))
        if scans:
            self.hub.publish(json.dumps({"scans": scans, "now": round(time.time(), 1)},
                                        separators=(",", ":")), "scan")


class DemoScans:
    """Now and then, a made-up scan of the demo machine."""

    TOP_TCP = [21, 22, 23, 25, 53, 80, 81, 88, 110, 111, 113, 119, 135, 139, 143, 179, 199, 389, 427,
               443, 444, 445, 465, 513, 514, 515, 543, 544, 548, 554, 587, 631, 646, 873, 990, 993, 995,
               1025, 1026, 1027, 1028, 1029, 1110, 1433, 1720, 1723, 1755, 1900, 2000, 2001, 2049, 2121,
               2717, 3000, 3128, 3306, 3389, 3986, 4899, 5000, 5009, 5051, 5060, 5101, 5190, 5357, 5432,
               5631, 5666, 5800, 5900, 6000, 6001, 6646, 7070, 8000, 8008, 8009, 8080, 8081, 8443, 8888,
               9100, 9999, 10000, 32768, 49152, 49153, 49154, 49155, 49156, 49157]
    TOP_UDP = [53, 67, 69, 123, 137, 138, 161, 500, 514, 520, 1434, 1900, 4500, 5353]
    OPEN = {135, 139, 445, 3389, 5357}
    SOURCES = [("10.0.0.66", "scanner.lan"), ("192.168.1.23", ""), ("172.16.4.9", "pentest-vm.local")]

    def __init__(self, watch: ScanWatch) -> None:
        self.watch = watch
        self._stop = threading.Event()
        self.rng = random.Random()

    def start(self) -> None:
        threading.Thread(target=self._run, name="demo-scans", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        wait, n = 25.0, 0
        while not self._stop.wait(wait):
            remote, name = self.SOURCES[n % len(self.SOURCES)]
            self.watch.known_names[remote] = name
            if n % 2 == 0:   # a sweep of every well-known port, in order
                probes = [(p, TCP) for p in range(1, 1025)]
                seconds = self.rng.uniform(18, 26)
            else:            # the usual suspects, in random order
                probes = [(p, TCP) for p in self.TOP_TCP] + [(p, UDP) for p in self.rng.sample(self.TOP_UDP, 6)]
                self.rng.shuffle(probes)
                seconds = self.rng.uniform(8, 14)
            n += 1
            for p, proto in probes:
                kind = "open" if proto == TCP and p in self.OPEN else "closed"
                self.watch.probe(remote, p, proto, kind, self.rng.randint(40000, 60000))
                if self._stop.wait(seconds / len(probes)):
                    return
            wait = self.rng.uniform(120, 200)
"""System memory, GPU and CPU sampling for the Memory & GPU tab.

- System memory: which 1 GB slices of physical address space hold RAM, and how
  the pages in each slice are being used. This reads a sample of the memory
  manager's page database through the same undocumented interface Sysinternals
  RAMMap uses (NtQuerySystemInformation, SystemSuperfetchInformation), which
  needs administrator rights.
- GPU: dedicated memory size (D3DKMT), and memory use and engine utilization
  from the performance counters Task Manager reads ("GPU Adapter Memory",
  "GPU Process Memory", "GPU Engine"). Windows doesn't report where in GPU
  memory anything is stored, so the view fills GPU memory in order.
- CPU: overall utilization (GetSystemTimes).

Run directly (python sysmon.py) to print one sample as JSON.
"""
from __future__ import annotations

import ctypes
import json
import math
import random
import re
import struct
import sys
import threading
import time
from array import array
from ctypes import wintypes

GB = 1 << 30
PAGE = 4096
PAGES_PER_GB = GB // PAGE
SAMPLE_BUDGET = 65536        # page-database entries read per sample, at most
MAX_PER_GB = 1024            # ...and at most this many per GB (one per 1 MB)

ULONG = wintypes.ULONG
DWORD = wintypes.DWORD


def _nt_error(status: int) -> str:
    known = {0xC0000022: "Access is denied", 0xC0000061: "A required privilege is not held",
             0xC0000003: "Not supported by this version of Windows",
             0xC000000D: "The request was rejected as invalid",
             0xC0000002: "Not implemented on this version of Windows"}
    return known.get(status, f"NTSTATUS 0x{status:08X}")


# ---------------------------------------------------------------------------
# Physical memory map (needs administrator rights)
# ---------------------------------------------------------------------------

SystemSuperfetchInformation = 79
SUPERFETCH_VERSION = 45
SUPERFETCH_MAGIC = 0x6B756843          # 'kuhC'
SuperfetchPfnQuery = 6
SuperfetchMemoryRangesQuery = 17
STATUS_BUFFER_TOO_SMALL = 0xC0000023
SE_PROF_SINGLE_PROCESS_PRIVILEGE = 13
PFN_REQUEST_HEADER = 192               # Version, Flags, PfnCount, SYSTEM_MEMORY_LIST_INFORMATION
PFN_ENTRY = 24                         # MMPFN_IDENTITY: u1, PageFrameIndex, u2

# MMPFNLIST_* -> bucket: 0 free, 1 cached (standby), 2 modified, 3 in use, 4 bad
LIST_BUCKET = (0, 0, 1, 2, 2, 4, 3, 3)
# MMPFNUSE_*, for pages in use
USES = ("Process memory", "Mapped files", "Shared memory", "Page tables", "Paged pool",
        "Nonpaged pool", "System", "Session memory", "File system metadata",
        "Locked by apps (AWE)", "Locked by drivers", "Kernel stacks")


class SUPERFETCH_INFORMATION(ctypes.Structure):
    _fields_ = [("Version", ULONG), ("Magic", ULONG), ("InfoClass", ULONG),
                ("Data", ctypes.c_void_p), ("Length", ULONG)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", DWORD), ("dwMemoryLoad", DWORD), ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64), ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64), ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64), ("ullAvailExtendedVirtual", ctypes.c_uint64)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
_k32.GetPhysicallyInstalledSystemMemory.argtypes = [ctypes.POINTER(ctypes.c_uint64)]
_k32.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3


def memory_status() -> tuple[int, int, int]:
    """(installed, usable by Windows, available) bytes."""
    ms = MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(ms)
    _k32.GlobalMemoryStatusEx(ctypes.byref(ms))
    kb = ctypes.c_uint64()
    installed = kb.value * 1024 if _k32.GetPhysicallyInstalledSystemMemory(ctypes.byref(kb)) else ms.ullTotalPhys
    return installed or ms.ullTotalPhys, ms.ullTotalPhys, ms.ullAvailPhys


class RamMap:
    """Samples how each GB of physical address space is used."""

    def __init__(self) -> None:
        nt = ctypes.WinDLL("ntdll")
        self._ntq = nt.NtQuerySystemInformation
        self._ntq.argtypes = [ULONG, ctypes.c_void_p, ULONG, ctypes.POINTER(ULONG)]
        self._ntq.restype = ctypes.c_long
        adjust = nt.RtlAdjustPrivilege
        adjust.argtypes = [ULONG, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte)]
        adjust.restype = ctypes.c_long
        was = ctypes.c_ubyte()
        st = adjust(SE_PROF_SINGLE_PROCESS_PRIVILEGE, 1, 0, ctypes.byref(was)) & 0xFFFFFFFF
        if st:
            raise OSError("Reading the memory map needs administrator rights."
                          if st == 0xC0000061 else f"Couldn't read the memory map: {_nt_error(st)}")
        self.ranges = self._ranges()
        self._plan()

    def _query(self, info_class: int, buf, size: int) -> tuple[int, int]:
        info = SUPERFETCH_INFORMATION(SUPERFETCH_VERSION, SUPERFETCH_MAGIC, info_class,
                                      ctypes.cast(buf, ctypes.c_void_p), size)
        ret = ULONG()
        st = self._ntq(SystemSuperfetchInformation, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(ret))
        return st & 0xFFFFFFFF, ret.value

    def _ranges(self) -> list[tuple[int, int]]:
        """[(first page, page count)] of RAM in physical address space."""
        _, usable, _ = memory_status()
        last = 0
        for version in (2, 1):   # version 2 since Windows 10 1607; version 1 before that
            size = 1 << 16
            for _ in range(4):
                buf = ctypes.create_string_buffer(size)
                struct.pack_into("<I", buf, 0, version)
                st, need = self._query(SuperfetchMemoryRangesQuery, buf, size)
                if st == STATUS_BUFFER_TOO_SMALL and need > size:
                    size = need
                    continue
                break
            if st:
                last = st
                continue
            count, off = ((struct.unpack_from("<I", buf, 8)[0], 16) if version == 2
                          else (struct.unpack_from("<I", buf, 4)[0], 8))
            if not 0 < count <= 4096 or off + 16 * count > size:
                continue
            ranges = sorted(struct.unpack_from("<QQ", buf, off + 16 * i) for i in range(count))
            pages = sum(n for _, n in ranges)
            if all(n > 0 for _, n in ranges) and 0.9 * usable <= pages * PAGE <= 2 * usable + GB:
                return [(int(b), int(n)) for b, n in ranges]
        raise OSError(f"Couldn't read the physical memory layout: {_nt_error(last) if last else 'unexpected format'}")

    def _plan(self) -> None:
        """Pick which pages to sample: evenly spread over the RAM in each GB."""
        end = max(b + n for b, n in self.ranges)
        self.cells = math.ceil(end / PAGES_PER_GB)
        ram = [[] for _ in range(self.cells)]          # per GB: [(first page, count)]
        for base, count in self.ranges:
            p, stop = base, base + count
            while p < stop:
                g = p // PAGES_PER_GB
                seg_end = min(stop, (g + 1) * PAGES_PER_GB)
                ram[g].append((p, seg_end - p))
                p = seg_end
        self.ram_pages = [sum(n for _, n in segs) for segs in ram]
        per_gb = max(64, min(MAX_PER_GB, SAMPLE_BUDGET // max(1, sum(1 for r in self.ram_pages if r))))
        pfns, cell_of = array("Q"), array("H")
        for g, segs in enumerate(ram):
            total = self.ram_pages[g]
            k = min(per_gb, total)
            if not k:
                continue
            step = total / k
            positions = (int(step * (j + 0.5)) for j in range(k))
            seg_i, seg_start = 0, 0                     # position where segs[seg_i] begins
            for pos in positions:
                while pos >= seg_start + segs[seg_i][1]:
                    seg_start += segs[seg_i][1]
                    seg_i += 1
                pfns.append(segs[seg_i][0] + pos - seg_start)
                cell_of.append(g)
        self.cell_of = cell_of
        n = len(pfns)
        entries = array("Q", bytes(8 * 3 * n))
        entries[1::3] = pfns
        self._size = PFN_REQUEST_HEADER + PFN_ENTRY * n
        self._template = struct.pack("<IIQ", 1, 1, n) + bytes(PFN_REQUEST_HEADER - 16) + entries.tobytes()
        self._buf = ctypes.create_string_buffer(self._size)
        self.sampled = n

    def sample(self) -> list:
        """Per GB: None where there's no RAM, else fractions of its RAM by state."""
        ctypes.memmove(self._buf, self._template, self._size)
        st, _ = self._query(SuperfetchPfnQuery, self._buf, self._size)
        if st:
            raise OSError(f"Couldn't read the memory map: {_nt_error(st)}")
        out = array("Q")
        out.frombytes(self._buf.raw[PFN_REQUEST_HEADER:])
        u1 = out[0::3]
        buckets = [[0] * 5 for _ in range(self.cells)]
        uses = [[0] * 16 for _ in range(self.cells)]
        for g, v in zip(self.cell_of, u1):
            b = LIST_BUCKET[(v >> 4) & 7]
            buckets[g][b] += 1
            if b == 3:
                uses[g][v & 15] += 1
        cells = []
        for g in range(self.cells):
            n = sum(buckets[g])
            if not self.ram_pages[g] or not n:
                cells.append(None)
                continue
            free, cached, modified, inuse, _bad = buckets[g]
            top = sorted(((c, i) for i, c in enumerate(uses[g]) if c and i < len(USES)), reverse=True)[:3]
            cells.append({"r": round(self.ram_pages[g] / PAGES_PER_GB, 3),
                          "u": round(inuse / n, 3), "m": round(modified / n, 3),
                          "c": round(cached / n, 3), "f": round(free / n, 3),
                          "t": [[i, round(c / n, 3)] for c, i in top]})
        return cells


# ---------------------------------------------------------------------------
# GPU: size from D3DKMT, use and utilization from performance counters
# ---------------------------------------------------------------------------

class LUID(ctypes.Structure):
    _fields_ = [("LowPart", DWORD), ("HighPart", wintypes.LONG)]


class D3DKMT_ADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("AdapterLuid", LUID), ("NumOfSources", ULONG),
                ("bPrecisePresentRegionsPreferred", wintypes.BOOL)]


class D3DKMT_ENUMADAPTERS2(ctypes.Structure):
    _fields_ = [("NumAdapters", ULONG), ("pAdapters", ctypes.POINTER(D3DKMT_ADAPTERINFO))]


class D3DKMT_QUERYADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("Type", wintypes.UINT),
                ("pPrivateDriverData", ctypes.c_void_p), ("PrivateDriverDataSize", wintypes.UINT)]


class D3DKMT_SEGMENTSIZEINFO(ctypes.Structure):
    _fields_ = [("DedicatedVideoMemorySize", ctypes.c_uint64), ("DedicatedSystemMemorySize", ctypes.c_uint64),
                ("SharedSystemMemorySize", ctypes.c_uint64)]


class D3DKMT_ADAPTERREGISTRYINFO(ctypes.Structure):
    _fields_ = [("AdapterString", ctypes.c_wchar * 260), ("BiosString", ctypes.c_wchar * 260),
                ("DacType", ctypes.c_wchar * 260), ("ChipType", ctypes.c_wchar * 260)]


KMTQAITYPE_GETSEGMENTSIZE = 3
KMTQAITYPE_ADAPTERREGISTRYINFO = 8


def gpu_adapters() -> list[dict]:
    """Display adapters with their own memory: name, LUID and dedicated bytes."""
    gdi = ctypes.WinDLL("gdi32")
    enum = gdi.D3DKMTEnumAdapters2
    enum.argtypes = [ctypes.POINTER(D3DKMT_ENUMADAPTERS2)]
    enum.restype = ctypes.c_long
    query = gdi.D3DKMTQueryAdapterInfo
    query.argtypes = [ctypes.POINTER(D3DKMT_QUERYADAPTERINFO)]
    query.restype = ctypes.c_long
    close = gdi.D3DKMTCloseAdapter
    close.argtypes = [ctypes.POINTER(wintypes.UINT)]
    close.restype = ctypes.c_long

    e = D3DKMT_ENUMADAPTERS2()
    if enum(ctypes.byref(e)) or not e.NumAdapters:
        return []
    infos = (D3DKMT_ADAPTERINFO * e.NumAdapters)()
    e.pAdapters = infos
    if enum(ctypes.byref(e)):
        return []
    found = []
    for a in infos[:e.NumAdapters]:
        seg = D3DKMT_SEGMENTSIZEINFO()
        q = D3DKMT_QUERYADAPTERINFO(a.hAdapter, KMTQAITYPE_GETSEGMENTSIZE,
                                    ctypes.cast(ctypes.byref(seg), ctypes.c_void_p), ctypes.sizeof(seg))
        if query(ctypes.byref(q)) == 0 and seg.DedicatedVideoMemorySize:
            reg = D3DKMT_ADAPTERREGISTRYINFO()
            q = D3DKMT_QUERYADAPTERINFO(a.hAdapter, KMTQAITYPE_ADAPTERREGISTRYINFO,
                                        ctypes.cast(ctypes.byref(reg), ctypes.c_void_p), ctypes.sizeof(reg))
            name = reg.AdapterString.strip() if query(ctypes.byref(q)) == 0 else ""
            found.append({"name": name or "GPU", "luid": (a.AdapterLuid.HighPart & 0xFFFFFFFF, a.AdapterLuid.LowPart),
                          "vram": int(seg.DedicatedVideoMemorySize),
                          "shared": int(seg.SharedSystemMemorySize)})
        close(ctypes.byref(wintypes.UINT(a.hAdapter)))
    return found


class _PdhValue(ctypes.Union):
    _fields_ = [("longValue", wintypes.LONG), ("doubleValue", ctypes.c_double), ("largeValue", ctypes.c_longlong)]


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", DWORD), ("u", _PdhValue)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", PDH_FMT_COUNTERVALUE)]


PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2


class Counters:
    """A PDH query over wildcard counters; values() returns {instance: value}."""

    def __init__(self, paths: dict[str, str]) -> None:
        self.pdh = pdh = ctypes.WinDLL("pdh")
        pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t,
                                              ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        pdh.PdhGetFormattedCounterArrayW.argtypes = [ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD),
                                                     ctypes.POINTER(DWORD), ctypes.c_void_p]
        pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
        for f in (pdh.PdhOpenQueryW, pdh.PdhAddEnglishCounterW, pdh.PdhCollectQueryData,
                  pdh.PdhGetFormattedCounterArrayW, pdh.PdhCloseQuery):
            f.restype = DWORD
        self.query = ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)):
            raise OSError("Couldn't open the performance counters.")
        self.counters = {}
        for key, path in paths.items():
            h = ctypes.c_void_p()
            if pdh.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(h)) == 0:
                self.counters[key] = h
        if not self.counters:
            raise OSError("This version of Windows has no GPU performance counters.")
        pdh.PdhCollectQueryData(self.query)   # rates need two collections

    def collect(self) -> dict[str, dict[str, float]]:
        self.pdh.PdhCollectQueryData(self.query)
        return {key: self._values(h) for key, h in self.counters.items()}

    def _values(self, h) -> dict[str, float]:
        size, count = DWORD(0), DWORD(0)
        rc = self.pdh.PdhGetFormattedCounterArrayW(h, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
        if rc != PDH_MORE_DATA or not size.value:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        if self.pdh.PdhGetFormattedCounterArrayW(h, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buf):
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
        return {items[i].szName: items[i].FmtValue.u.doubleValue
                for i in range(count.value) if items[i].FmtValue.CStatus in (0, 1)}


_INST = re.compile(r"(?:pid_(\d+)_)?luid_0x([0-9a-fA-F]+)_0x([0-9a-fA-F]+)_phys_(\d+)(?:_eng_(\d+)_engtype_(\w+))?")
ENGINE_ORDER = ["3D", "Copy", "Compute", "Video decode", "Video encode", "Video processing"]


def engine_label(kind: str) -> str:
    k = kind.lower()
    if k.startswith(("compute", "cuda")):
        return "Compute"
    return {"3d": "3D", "copy": "Copy", "videodecode": "Video decode", "videoencode": "Video encode",
            "videoprocessing": "Video processing"}.get(k, kind.replace("_", " "))


class GpuMonitor:
    def __init__(self, names) -> None:
        adapters = [a for a in gpu_adapters() if a["vram"] >= 256 << 20]
        if not adapters:
            raise OSError("No graphics card with its own memory was found.")
        self.names = names
        self.counters = Counters({
            "engine": r"\GPU Engine(*)\Utilization Percentage",
            "dedicated": r"\GPU Adapter Memory(*)\Dedicated Usage",
            "shared": r"\GPU Adapter Memory(*)\Shared Usage",
            # Local Usage: what's resident in the card's own memory. (Dedicated
            # Usage counts surfaces shared between processes once per process,
            # so screen capture apps can show more than the card has.)
            "proc": r"\GPU Process Memory(*)\Local Usage",
        })
        # Virtual display adapters can report the same memory as the card
        # they run on; prefer the one the counters report on, then a named one.
        seen = set()
        for inst in self.counters.collect().get("dedicated", {}):
            m = _INST.match(inst)
            if m:
                seen.add((int(m.group(2), 16), int(m.group(3), 16)))
        self.gpu = max(adapters, key=lambda a: (a["luid"] in seen, a["vram"], a["name"] != "GPU"))

    def _ours(self, inst: str):
        m = _INST.match(inst)
        if not m or (int(m.group(2), 16), int(m.group(3), 16)) != self.gpu["luid"]:
            return None
        return m

    def sample(self) -> dict:
        data = self.counters.collect()
        engines: dict[tuple, list] = {}
        for inst, v in data.get("engine", {}).items():
            m = self._ours(inst)
            if m and m.group(6):
                e = engines.setdefault((m.group(4), m.group(5)), [m.group(6), 0.0])
                e[1] += v
        by_type: dict[str, float] = {}
        for kind, v in engines.values():
            label = engine_label(kind)
            by_type[label] = max(by_type.get(label, 0.0), min(100.0, v))
        order = {k: i for i, k in enumerate(ENGINE_ORDER)}
        # The usual engines always; odd ones (Security, LegacyOverlay...) only while busy
        eng = sorted(((k, v) for k, v in by_type.items() if k in order or v >= 0.5),
                     key=lambda kv: (order.get(kv[0], 99), kv[0]))
        used = sum(v for inst, v in data.get("dedicated", {}).items() if self._ours(inst))
        shared = sum(v for inst, v in data.get("shared", {}).items() if self._ours(inst))
        procs: dict[str, float] = {}
        for inst, v in data.get("proc", {}).items():
            m = self._ours(inst)
            if m and m.group(1) and v > 0:
                name = self.names(int(m.group(1))) or f"PID {m.group(1)}"
                procs[name] = procs.get(name, 0) + v
        top = sorted(procs.items(), key=lambda kv: kv[1], reverse=True)[:8]
        # The rest belongs to the driver and to surfaces shared between apps
        unowned = max(0, used - sum(procs.values()))
        return {"name": self.gpu["name"], "vram": self.gpu["vram"], "used": int(used), "shared": int(shared),
                "engines": [[k, round(v, 1)] for k, v in eng],
                "procs": [[k, int(v)] for k, v in top], "unowned": int(unowned)}


class CpuMonitor:
    def __init__(self) -> None:
        self._last = self._times()

    @staticmethod
    def _times() -> tuple[int, int]:
        t = [wintypes.FILETIME() for _ in range(3)]
        _k32.GetSystemTimes(*(ctypes.byref(x) for x in t))
        idle, kernel, user = ((x.dwHighDateTime << 32) | x.dwLowDateTime for x in t)
        return idle, kernel + user          # kernel time includes idle time

    def sample(self) -> float:
        idle, total = self._times()
        di, dt = idle - self._last[0], total - self._last[1]
        self._last = (idle, total)
        return round(100.0 * max(0.0, 1 - di / dt), 1) if dt > 0 else 0.0


# ---------------------------------------------------------------------------
# Live and simulated sources
# ---------------------------------------------------------------------------

class LiveSystem:
    def __init__(self, names) -> None:
        self.names = names
        self.ram = self.gpu = None
        self.ram_err = self.gpu_err = None
        self._ram_retry = self._gpu_retry = 0.0
        self.cpu = CpuMonitor()

    def _ensure(self) -> None:
        now = time.monotonic()
        if self.ram is None and now >= self._ram_retry:
            try:
                self.ram, self.ram_err = RamMap(), None
            except OSError as exc:
                self.ram_err, self._ram_retry = str(exc), now + 60
        if self.gpu is None and now >= self._gpu_retry:
            try:
                self.gpu, self.gpu_err = GpuMonitor(self.names), None
            except OSError as exc:
                self.gpu_err, self._gpu_retry = str(exc), now + 60

    def sample(self) -> dict:
        self._ensure()
        installed, usable, avail = memory_status()
        ram = {"installed": installed, "total": usable, "avail": avail, "cells": None, "err": self.ram_err}
        if self.ram:
            try:
                ram["cells"] = self.ram.sample()
            except OSError as exc:
                ram["err"] = str(exc)
        out = {"ram": ram, "cpu": self.cpu.sample(), "gpu": None, "gpuErr": self.gpu_err}
        if self.gpu:
            try:
                out["gpu"] = self.gpu.sample()
            except OSError as exc:
                out["gpuErr"] = str(exc)
        return out


class DemoSystem:
    """Plausible made-up numbers: 32 GB of RAM with the usual gap below 4 GB, a 24 GB GPU."""

    def __init__(self) -> None:
        self.rng = rng = random.Random()
        self.t0 = time.monotonic()
        # RAM from 0 to 3.25 GB, then from 4 GB (the gap is where devices are mapped)
        self.ram_frac = [1, 1, 1, 0.25] + [1] * 28 + [0.75]
        self.level = [min(0.97, max(0.05, (0.9 if g < 3 else 0.55) + rng.uniform(-0.3, 0.3)))
                      for g in range(len(self.ram_frac))]
        self.vram_used = 7.5 * GB
        self.cpu = 15.0

    def sample(self) -> dict:
        rng, t = self.rng, time.monotonic() - self.t0
        cells = []
        for g, r in enumerate(self.ram_frac):
            lvl = self.level[g] = min(0.98, max(0.03, self.level[g] + rng.gauss(0, 0.012)))
            mod = min(1 - lvl, 0.02 + rng.random() * 0.02)
            cached = (1 - lvl - mod) * (0.85 if g < 24 else 0.4)
            free = max(0.0, 1 - lvl - mod - cached)
            top = [[0, round(lvl * 0.6, 3)], [1, round(lvl * 0.25, 3)], [5, round(lvl * 0.1, 3)]]
            if g < 2:
                top = [[5, round(lvl * 0.45, 3)], [6, round(lvl * 0.3, 3)], [0, round(lvl * 0.2, 3)]]
            cells.append({"r": r, "u": round(lvl, 3), "m": round(mod, 3), "c": round(cached, 3),
                          "f": round(free, 3), "t": top})
        total = int(31.8 * GB)
        used = sum((c["u"] + c["m"]) * c["r"] for c in cells) * GB
        self.vram_used = min(23 * GB, max(3 * GB, self.vram_used + rng.gauss(0, 0.15) * GB))
        burst = 0.5 + 0.5 * math.sin(t / 7)
        g3d = min(100.0, max(0.0, 30 + 55 * burst + rng.gauss(0, 6)))
        self.cpu = min(100.0, max(2.0, self.cpu + rng.gauss(0, 4) + (18 - self.cpu) * 0.1))
        game = self.vram_used * 0.7
        return {
            "ram": {"installed": 32 * GB, "total": total, "avail": int(max(0, total - used)), "cells": cells, "err": None},
            "cpu": round(self.cpu, 1),
            "gpu": {"name": "Simulated GPU", "vram": 24 * GB, "used": int(self.vram_used), "shared": int(0.4 * GB),
                    "engines": [["3D", round(g3d, 1)], ["Copy", round(max(0, rng.gauss(4, 2)), 1)],
                                ["Compute", round(max(0, rng.gauss(2, 1.5)), 1)],
                                ["Video decode", round(12 + 10 * math.sin(t / 3), 1)],
                                ["Video encode", 0.0]],
                    "procs": [["game", int(game)], ["chrome", int(self.vram_used * 0.07)],
                              ["dwm", int(self.vram_used * 0.05)], ["msedgewebview2", int(self.vram_used * 0.03)],
                              ["explorer", int(self.vram_used * 0.02)]],
                    "unowned": int(self.vram_used * 0.13)},
            "gpuErr": None,
        }


class SystemMonitor:
    """Publishes a sample about once a second while anyone is watching."""

    def __init__(self, hub, mode: str, names) -> None:
        self.hub, self.mode, self.names = hub, mode, names
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="sysmon", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            source = DemoSystem() if self.mode == "demo" else LiveSystem(self.names)
        except Exception as exc:  # noqa: BLE001 - reported to the view
            source, err = None, str(exc)
        while not self._stop.is_set():
            start = time.perf_counter()
            if self.hub.viewers():
                try:
                    msg = source.sample() if source else {"err": err}
                except Exception as exc:  # noqa: BLE001
                    msg = {"err": f"{type(exc).__name__}: {exc}"}
                self.hub.publish(json.dumps(msg, separators=(",", ":")), "sys")
            self._stop.wait(max(0.2, 1.0 - (time.perf_counter() - start)))


if __name__ == "__main__":
    # One live sample (two for the rate counters), printed as JSON.
    # python sysmon.py [output.json]
    src = LiveSystem(lambda pid: "")
    src.sample()
    time.sleep(1)
    t = time.perf_counter()
    sample = src.sample()
    sample["sampleMs"] = round((time.perf_counter() - t) * 1000, 1)
    if src.ram:
        sample["ramRanges"] = [[b * PAGE, n * PAGE] for b, n in src.ram.ranges]
        sample["ramSampled"] = src.ram.sampled
    text = json.dumps(sample, indent=1)
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)

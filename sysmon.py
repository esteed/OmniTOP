"""System memory, GPU and CPU sampling for the Memory & GPU tab.

- System memory: how much RAM is in use, modified, standby and free, and what
  the memory in use holds, from the performance counters Resource Monitor
  reads ("Memory"). For a finer breakdown, and for where in physical address
  space each kind sits, this also reads a sample of the memory manager's page
  database through the same undocumented interface Sysinternals RAMMap uses
  (NtQuerySystemInformation, SystemSuperfetchInformation), which needs
  administrator rights.
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
SAMPLE_BUDGET = 65536        # page-database entries read per sample, at most
MAX_PER_GB = 1024            # ...and at most this many per GB (one per 1 MB)
MIN_SLICE = 64 << 20         # the address map's slices: at least 64 MB,
MAX_SLICES = 600             # ...doubled until there are at most this many
MAP_EVERY = 0.25             # seconds between address maps (and in-use splits) sent to the view
TOP_PROGRAMS = 30            # programs listed by memory use; the rest are added up
DIMMS_EVERY = 2.0            # seconds between copies of the memory module list (it doesn't change)

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


class PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [("cb", DWORD), ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t),
                ("PhysicalAvailable", ctypes.c_size_t), ("SystemCache", ctypes.c_size_t),
                ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t),
                ("HandleCount", DWORD), ("ProcessCount", DWORD), ("ThreadCount", DWORD)]


_k32.K32GetPerformanceInfo.argtypes = [ctypes.POINTER(PERFORMANCE_INFORMATION), DWORD]


def commit_status() -> dict | None:
    """Committed memory (promised to programs, backed by RAM or the page file) and its limit."""
    pi = PERFORMANCE_INFORMATION()
    pi.cb = ctypes.sizeof(pi)
    if not _k32.K32GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        return None
    page = pi.PageSize
    return {"used": pi.CommitTotal * page, "limit": pi.CommitLimit * page, "peak": pi.CommitPeak * page}


def memory_status() -> tuple[int, int, int]:
    """(installed, usable by Windows, available) bytes."""
    ms = MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(ms)
    _k32.GlobalMemoryStatusEx(ctypes.byref(ms))
    kb = ctypes.c_uint64()
    installed = kb.value * 1024 if _k32.GetPhysicallyInstalledSystemMemory(ctypes.byref(kb)) else ms.ullTotalPhys
    return installed or ms.ullTotalPhys, ms.ullTotalPhys, ms.ullAvailPhys


class RamMap:
    """Samples how each slice of physical address space is used."""

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
        """Pick which pages to sample: evenly spread over the RAM in each slice."""
        end = max(b + n for b, n in self.ranges)
        slice_bytes = MIN_SLICE
        while end * PAGE > slice_bytes * MAX_SLICES:
            slice_bytes *= 2
        self.slice = slice_bytes
        self.pps = pps = slice_bytes // PAGE            # pages per slice
        self.cells = math.ceil(end / pps)
        ram = [[] for _ in range(self.cells)]          # per slice: [(first page, count)]
        for base, count in self.ranges:
            p, stop = base, base + count
            while p < stop:
                g = p // pps
                seg_end = min(stop, (g + 1) * pps)
                ram[g].append((p, seg_end - p))
                p = seg_end
        self.ram_pages = [sum(n for _, n in segs) for segs in ram]
        cap = max(16, MAX_PER_GB * slice_bytes // GB)
        per_slice = max(16, min(cap, SAMPLE_BUDGET // max(1, sum(1 for r in self.ram_pages if r))))
        pfns, cell_of = array("Q"), array("H")
        self.weight = [0.0] * self.cells               # pages of RAM each sampled page stands for
        for g, segs in enumerate(ram):
            total = self.ram_pages[g]
            k = min(per_slice, total)
            if k:
                self.weight[g] = total / k
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
        self._pfns = pfns
        self.sampled = len(pfns)
        self._k = 0
        self.set_parts(1)

    def set_parts(self, k: int) -> None:
        """Read the sampled pages in k interleaved parts, one part per sample.

        Every part covers every GB, so each sample updates the whole map from
        1/k of its pages, and k samples (a second, at any rate) read them all:
        sampling faster costs no more than once a second. The totals are kept
        per part and swapped as each part is read again.
        """
        k = max(1, min(int(k), self.sampled // 256 or 1))
        if k == self._k:
            return
        self._k = k
        self._parts = []
        for j in range(k):
            pfns, cells = self._pfns[j::k], self.cell_of[j::k]
            n = len(pfns)
            entries = array("Q", bytes(8 * 3 * n))
            entries[1::3] = pfns
            size = PFN_REQUEST_HEADER + PFN_ENTRY * n
            self._parts.append({
                "cells": cells, "size": size, "buf": ctypes.create_string_buffer(size),
                "template": struct.pack("<IIQ", 1, 1, n) + bytes(PFN_REQUEST_HEADER - 16) + entries.tobytes(),
                "b": None, "u": None})
        self._tb = [0] * (5 * self.cells)    # totals over all parts: page list per slice
        self._tu = [0] * (16 * self.cells)   # and what in-use pages are used for
        self._next = 0
        self._full = False

    def _read(self, part: dict) -> None:
        ctypes.memmove(part["buf"], part["template"], part["size"])
        st, _ = self._query(SuperfetchPfnQuery, part["buf"], part["size"])
        if st:
            raise OSError(f"Couldn't read the memory map: {_nt_error(st)}")
        out = array("Q")
        out.frombytes(part["buf"].raw[PFN_REQUEST_HEADER:part["size"]])
        b, u = [0] * (5 * self.cells), [0] * (16 * self.cells)
        for g, v in zip(part["cells"], out[0::3]):
            k = LIST_BUCKET[(v >> 4) & 7]
            b[g * 5 + k] += 1
            if k == 3:
                u[g * 16 + (v & 15)] += 1
        if part["b"] is None:
            self._tb = [t + n for t, n in zip(self._tb, b)]
            self._tu = [t + n for t, n in zip(self._tu, u)]
        else:
            self._tb = [t - o + n for t, o, n in zip(self._tb, part["b"], b)]
            self._tu = [t - o + n for t, o, n in zip(self._tu, part["u"], u)]
        part["b"], part["u"] = b, u

    def sample(self, detail: bool = True) -> dict:
        """Estimates over all of RAM, and with detail how each slice is used.

        "states" is pages by list (free, cached, modified, in use, bad) and
        "uses" is pages in use by USES index, over all of RAM. Each call reads
        the next part of the sample; the totals and cells are worked out only
        with detail (they change slowly), and the totals are kept between.
        "cells" (with detail, else None) has one entry per slice: None where
        it has no RAM, else permille ints: the share of the slice that is RAM,
        the shares of its RAM in use, modified, cached and free, then up to
        three (USES index, share) pairs for what most of its pages in use hold.
        """
        if not self._full:   # the first sample reads every part, so it starts complete
            for part in self._parts:
                self._read(part)
            self._full = True
            detail = True
        else:
            self._read(self._parts[self._next])
            self._next = (self._next + 1) % len(self._parts)
        if not detail:
            return {"states": self._states, "uses": self._uses, "cells": None}
        tb, tu, w = self._tb, self._tu, self.weight
        self._states = [sum(t * x for t, x in zip(tb[i::5], w)) for i in range(5)]
        self._uses = [sum(t * x for t, x in zip(tu[i::16], w)) for i in range(len(USES))]
        cells = []
        for g in range(self.cells):
            bk = tb[g * 5:g * 5 + 5]
            n = sum(bk)
            if not self.ram_pages[g] or not n:
                cells.append(None)
                continue
            free, cached, modified, inuse, _bad = bk
            use = tu[g * 16:g * 16 + len(USES)]
            top = sorted(((c, i) for i, c in enumerate(use) if c), reverse=True)[:3]
            cell = [round(1000 * self.ram_pages[g] / self.pps), round(1000 * inuse / n),
                    round(1000 * modified / n), round(1000 * cached / n), round(1000 * free / n)]
            for c, i in top:
                cell += (i, round(1000 * c / n))
            cells.append(cell)
        return {"states": self._states, "uses": self._uses, "cells": cells}


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

    def __init__(self, paths: dict[str, str], what: str = "GPU") -> None:
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
            raise OSError(f"This version of Windows has no {what} performance counters.")
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
# System memory totals: performance counters (no rights needed), the page
# files, and the memory compression store
# ---------------------------------------------------------------------------

MEM_COUNTERS = {
    "free": r"\Memory\Free & Zero Page List Bytes",
    "modified": r"\Memory\Modified Page List Bytes",
    "core": r"\Memory\Standby Cache Core Bytes",
    "normal": r"\Memory\Standby Cache Normal Priority Bytes",
    "reserve": r"\Memory\Standby Cache Reserve Bytes",
    "paged": r"\Memory\Pool Paged Resident Bytes",
    "pagedAll": r"\Memory\Pool Paged Bytes",
    "nonpaged": r"\Memory\Pool Nonpaged Bytes",
    "drivers": r"\Memory\System Driver Resident Bytes",
    "code": r"\Memory\System Code Resident Bytes",
    "cache": r"\Memory\System Cache Resident Bytes",
}
LIST_KEYS = ("free", "modified", "core", "normal", "reserve")
# USES indexes shown together as "other kernel memory": system PTEs, session
# memory, file system metadata, pages locked by drivers, kernel stacks
KERNEL_USES = (6, 7, 8, 10, 11)


class MemoryCounters:
    """RAM by page list, and kernel memory, in bytes, from the "Memory" counters."""

    def __init__(self) -> None:
        self.counters = Counters(MEM_COUNTERS, "memory")
        if not all(k in self.counters.counters for k in LIST_KEYS):
            raise OSError("This version of Windows doesn't report standby memory.")

    def sample(self) -> dict[str, int]:
        values = {k: int(sum(v.values())) for k, v in self.counters.collect().items()}
        return {k: values.get(k, 0) for k in MEM_COUNTERS}


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]


class SYSTEM_PAGEFILE_INFORMATION(ctypes.Structure):
    _fields_ = [("NextEntryOffset", ULONG), ("TotalSize", ULONG), ("TotalInUse", ULONG),
                ("PeakUsage", ULONG), ("PageFileName", UNICODE_STRING)]


SystemPageFileInformation = 18
_ntq = ctypes.WinDLL("ntdll").NtQuerySystemInformation
_ntq.argtypes = [ULONG, ctypes.c_void_p, ULONG, ctypes.POINTER(ULONG)]
_ntq.restype = ctypes.c_long


def page_files() -> list[list]:
    """[[path, size, in use, peak use]] for each page file, sizes in bytes."""
    buf = ctypes.create_string_buffer(1 << 14)
    ret = ULONG()
    if _ntq(SystemPageFileInformation, buf, len(buf), ctypes.byref(ret)) or not ret.value:
        return []
    files, off = [], 0
    while off + ctypes.sizeof(SYSTEM_PAGEFILE_INFORMATION) <= ret.value:
        info = SYSTEM_PAGEFILE_INFORMATION.from_buffer(buf, off)
        name = info.PageFileName
        path = ctypes.wstring_at(name.Buffer, name.Length // 2) if name.Buffer and name.Length else ""
        files.append([path.removeprefix("\\??\\"), info.TotalSize * PAGE, info.TotalInUse * PAGE,
                      info.PeakUsage * PAGE])
        if not info.NextEntryOffset:
            break
        off += info.NextEntryOffset
    return files


# ---------------------------------------------------------------------------
# Memory modules, from the firmware's SMBIOS tables (no rights needed)
# ---------------------------------------------------------------------------

MEMORY_TYPES = {0x0F: "SDRAM", 0x12: "DDR", 0x13: "DDR2", 0x14: "DDR2 FB-DIMM", 0x18: "DDR3", 0x19: "FBD2",
                0x1A: "DDR4", 0x1B: "LPDDR", 0x1C: "LPDDR2", 0x1D: "LPDDR3", 0x1E: "LPDDR4",
                0x1F: "Non-volatile", 0x20: "HBM", 0x21: "HBM2", 0x22: "DDR5", 0x23: "LPDDR5", 0x24: "HBM3"}
FORM_FACTORS = {0x05: "Card", 0x07: "SIMM", 0x08: "SIP", 0x09: "DIMM", 0x0B: "Row of chips", 0x0C: "Chip",
                0x0D: "SODIMM", 0x0F: "FB-DIMM", 0x10: "Die", 0x11: "CAMM"}
# Module brands by part number: the firmware often names the DRAM maker or a
# code instead. Longer prefixes first.
PART_BRANDS = [
    ("CMSX", "Corsair"), ("CMK", "Corsair"), ("CMW", "Corsair"), ("CMT", "Corsair"), ("CMH", "Corsair"),
    ("CMG", "Corsair"), ("CMR", "Corsair"), ("CMD", "Corsair"), ("CMN", "Corsair"), ("CMP", "Corsair"),
    ("CMS", "Corsair"), ("F3-", "G.Skill"), ("F4-", "G.Skill"), ("F5-", "G.Skill"),
    ("KHX", "Kingston HyperX"), ("KF", "Kingston FURY"), ("KVR", "Kingston"), ("KSM", "Kingston"),
    ("KCP", "Kingston"), ("ACR", "Kingston"), ("9905", "Kingston"),
    ("BLS", "Crucial Ballistix"), ("BLM", "Crucial Ballistix"), ("BLT", "Crucial Ballistix"),
    ("BL", "Crucial Ballistix"), ("CT", "Crucial"), ("CP", "Crucial"), ("MTA", "Micron"), ("MTC", "Micron"),
    ("M378", "Samsung"), ("M471", "Samsung"), ("M393", "Samsung"), ("M391", "Samsung"), ("M425", "Samsung"),
    ("M321", "Samsung"), ("M323", "Samsung"), ("HMA", "SK hynix"), ("HMT", "SK hynix"), ("HMC", "SK hynix"),
    ("TLZ", "TeamGroup"), ("TDZ", "TeamGroup"), ("TED", "TeamGroup"), ("TF", "TeamGroup"), ("FF", "TeamGroup"),
    ("PSD", "Patriot"), ("PSP", "Patriot"), ("PVS", "Patriot"), ("PVB", "Patriot"), ("PV", "Patriot"),
    ("AX4", "ADATA XPG"), ("AX5", "ADATA XPG"), ("AD4", "ADATA"), ("AD5", "ADATA"),
]
# JEDEC manufacturer codes, as some firmware reports them (both byte orders)
JEDEC = {"80CE": "Samsung", "CE00": "Samsung", "00CE": "Samsung", "80AD": "SK hynix", "AD00": "SK hynix",
         "00AD": "SK hynix", "802C": "Micron", "2C00": "Micron", "002C": "Micron", "0198": "Kingston",
         "9801": "Kingston", "04CD": "G.Skill", "CD04": "G.Skill", "029E": "Corsair", "9E02": "Corsair",
         "859B": "Crucial", "9B85": "Crucial", "059B": "Crucial", "04EF": "TeamGroup", "EF04": "TeamGroup",
         "04CB": "ADATA", "CB04": "ADATA"}
PLACEHOLDERS = re.compile(r"^(|0+|f+|unknown|undefined|not specified|none|no dimm|empty|to be filled by o\.e\.m\.|"
                          r"manufacturer\s*\d*|partnum\s*\d*|modulepartnumber\s*\d*|sernum\s*\d*)$", re.I)


def _firmware_smbios() -> bytes:
    fn = _k32.GetSystemFirmwareTable
    fn.argtypes = [DWORD, DWORD, ctypes.c_void_p, DWORD]
    fn.restype = wintypes.UINT
    sig = int.from_bytes(b"RSMB", "big")
    n = fn(sig, 0, None, 0)
    if not n:
        raise OSError("The firmware has no SMBIOS tables.")
    buf = ctypes.create_string_buffer(n)
    if fn(sig, 0, buf, n) != n:
        raise OSError("Couldn't read the SMBIOS tables.")
    length = struct.unpack_from("<I", buf.raw, 4)[0]   # after the 8-byte RawSMBIOSData header
    return buf.raw[8:8 + length]


def _smbios_structures(data: bytes):
    """(type, handle, formatted bytes, strings) for each structure."""
    off = 0
    while off + 4 <= len(data):
        typ, ln, handle = struct.unpack_from("<BBH", data, off)
        if ln < 4 or off + ln > len(data):
            break
        end = data.find(b"\0\0", off + ln)
        if end < 0:
            break
        raw = data[off + ln:end]
        strings = [s.decode("latin-1").strip() for s in raw.split(b"\0")] if raw else []
        yield typ, handle, data[off:off + ln], strings
        if typ == 127:   # end of table
            break
        off = end + 2


def _field(f: bytes, off: int, fmt: str):
    size = struct.calcsize(fmt)
    return struct.unpack_from(fmt, f, off)[0] if off + size <= len(f) else None


def _clean(s: str) -> str:
    return "" if PLACEHOLDERS.match(s.strip()) else s.strip()


def module_brand(maker: str, part: str) -> str:
    """The brand to show: from the part number when it's a known one, else the maker."""
    p = part.upper()
    for prefix, brand in PART_BRANDS:
        if p.startswith(prefix):
            return brand
    return maker


def _maker(raw: str) -> str:
    m = _clean(raw)
    if re.fullmatch(r"[0-9A-Fa-f]{4,16}", m):
        return JEDEC.get(m[:4].upper(), "")
    return m


def _channel(bank: str, locator: str) -> str:
    """The memory channel a slot is on, from its labels, or "" if they don't say."""
    text = f"{bank} {locator}"
    for pattern in (r"CHANNEL\s*[-_ ]?([A-Z0-9])\b", r"\bCH[-_ ]?([A-Z])\b", r"DIMM[-_ ]?([A-Z])\d"):
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1).upper()
    m = re.fullmatch(r"([A-Z])\d", locator.strip(), re.I)   # slots labelled just "A1", "B2"
    return m.group(1).upper() if m else ""


def memory_modules() -> dict | None:
    """The memory slots and modules, as the firmware describes them.

    {"slots": slots for system memory, "max": largest supported (bytes) or
    None, "ecc": error correction in use, "arrays": [[start, end]] address
    ranges holding RAM, "mapping": "ranges" (each module has its own address
    range), "interleaved" (they share ranges) or "unknown" (not reported),
    "modules": [{locator, bank, channel, socket, size (bytes; 0 = empty slot,
    None = unknown), type, form, speed, configured (MT/s), brand, maker, part,
    rank, volts (mV), width (data bits), ecc, ranges: [[start, end]]}]} in
    the firmware's (slot) order, or None without SMBIOS memory information.
    """
    arrays, modules, maps = {}, [], {}
    ranges = []
    for typ, handle, f, strs in _smbios_structures(_firmware_smbios()):
        s = lambda i: strs[i - 1] if i and i <= len(strs) else ""   # noqa: E731 - string fields are 1-based
        if typ == 16:   # physical memory array
            use, ecc = _field(f, 0x05, "B"), _field(f, 0x06, "B")
            cap = _field(f, 0x07, "<I")
            if cap == 0x80000000:
                cap = _field(f, 0x0F, "<Q")
            elif cap is not None:
                cap *= 1024
            arrays[handle] = {"system": use in (None, 3), "ecc": ecc not in (None, 1, 2, 3),
                              "slots": _field(f, 0x0D, "<H") or 0, "max": cap or None}
        elif typ == 17:   # memory device (a slot)
            size = _field(f, 0x0C, "<H")
            if size == 0x7FFF:
                ext = _field(f, 0x1C, "<I")
                size = (ext & 0x7FFFFFFF) << 20 if ext else None
            elif size == 0xFFFF or size is None:
                size = None
            else:
                size = (size & 0x7FFF) << (10 if size & 0x8000 else 20)
            speed, configured = _field(f, 0x15, "<H"), _field(f, 0x20, "<H")
            if speed == 0xFFFF:
                speed = _field(f, 0x54, "<I")
            if configured == 0xFFFF:
                configured = _field(f, 0x58, "<I")
            total_w, data_w = _field(f, 0x08, "<H"), _field(f, 0x0A, "<H")
            locator, bank = _clean(s(_field(f, 0x10, "B"))), _clean(s(_field(f, 0x11, "B")))
            maker = _maker(s(_field(f, 0x17, "B")))
            part = _clean(s(_field(f, 0x1A, "B")))
            rank = _field(f, 0x1B, "B")
            sock = re.search(r"\b(?:P|CPU|SOCKET|NODE)\s*[-_]?(\d)", f"{bank} {locator}", re.I)
            modules.append({
                "handle": handle, "array": _field(f, 0x04, "<H"),
                "locator": locator, "bank": bank, "channel": _channel(bank, locator),
                "socket": sock.group(1) if sock else "", "size": size,
                "type": MEMORY_TYPES.get(_field(f, 0x12, "B"), ""),
                "form": FORM_FACTORS.get(_field(f, 0x0E, "B"), ""),
                "speed": speed or 0, "configured": configured or 0,
                "brand": module_brand(maker, part), "maker": maker, "part": part,
                "rank": (rank & 0x0F) if rank else 0, "volts": _field(f, 0x26, "<H") or 0,
                "width": data_w if data_w not in (None, 0xFFFF) else 0,
                "ecc": bool(total_w and data_w and total_w not in (0xFFFF,) and total_w > data_w)})
        elif typ == 19:   # where an array's memory sits in address space
            start, end = _field(f, 0x04, "<I"), _field(f, 0x08, "<I")
            if start == 0xFFFFFFFF:
                start, end = _field(f, 0x0F, "<Q"), _field(f, 0x17, "<Q")
                if start is not None and end is not None:
                    ranges.append([_field(f, 0x0C, "<H"), start, end + 1])
            elif start is not None and end is not None:
                ranges.append([_field(f, 0x0C, "<H"), start * 1024, (end + 1) * 1024])
        elif typ == 20:   # where a device's memory sits
            start, end = _field(f, 0x04, "<I"), _field(f, 0x08, "<I")
            if start == 0xFFFFFFFF:
                start, end = _field(f, 0x13, "<Q"), _field(f, 0x1B, "<Q")
                span = [start, end + 1] if start is not None and end is not None else None
            else:
                span = [start * 1024, (end + 1) * 1024] if start is not None and end is not None else None
            dev = _field(f, 0x0C, "<H")
            if span and span[1] > span[0]:
                maps.setdefault(dev, []).append(span)
    system = {h for h, a in arrays.items() if a["system"]}
    if system:
        modules = [m for m in modules if m["array"] in system]
    if not modules:
        return None
    for m in modules:
        m["ranges"] = maps.get(m.pop("handle"), [])
        m.pop("array")
    filled = [m for m in modules if m["size"]]
    if filled and all(m["ranges"] for m in filled):
        spans = [r for m in filled for r in m["ranges"]]
        shared = any(a[0] < b[1] and b[0] < a[1] for i, a in enumerate(spans) for b in spans[i + 1:])
        mapping = "interleaved" if shared else "ranges"
    else:
        mapping = "unknown"
    slots = sum(arrays[h]["slots"] for h in system) if system else 0
    caps = [arrays[h]["max"] for h in system if arrays[h]["max"]]
    return {"slots": max(slots, len(modules)), "max": sum(caps) if caps else None,
            "ecc": any(arrays[h]["ecc"] for h in system), "mapping": mapping,
            "arrays": sorted([s, e] for a, s, e in ranges if not system or a in system),
            "modules": modules}


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):   # the start of it; ctypes sizes match the OS's
    _fields_ = [("NextEntryOffset", ULONG), ("NumberOfThreads", ULONG), ("WorkingSetPrivateSize", ctypes.c_int64),
                ("HardFaultCount", ULONG), ("NumberOfThreadsHighWatermark", ULONG), ("CycleTime", ctypes.c_uint64),
                ("CreateTime", ctypes.c_int64), ("UserTime", ctypes.c_int64), ("KernelTime", ctypes.c_int64),
                ("ImageName", UNICODE_STRING), ("BasePriority", wintypes.LONG),
                ("UniqueProcessId", ctypes.c_void_p), ("InheritedFromUniqueProcessId", ctypes.c_void_p),
                ("HandleCount", ULONG), ("SessionId", ULONG), ("UniqueProcessKey", ctypes.c_size_t),
                ("PeakVirtualSize", ctypes.c_size_t), ("VirtualSize", ctypes.c_size_t), ("PageFaultCount", ULONG),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivatePageCount", ctypes.c_size_t)]


SystemProcessInformation = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


class Programs:
    """RAM each program uses, with its processes added up by name.

    The size is the private working set: RAM only that program's processes
    use, as in Task Manager's Memory column, so the sizes add up. One call
    lists every process (no rights needed); it runs once a second, since
    programs' memory changes slowly.
    """
    EVERY = 1.0

    def __init__(self) -> None:
        self._buf = ctypes.create_string_buffer(1 << 19)
        self._next = 0.0
        self.value = None

    def sample(self, now: float) -> dict | None:
        """{"top": [[name, private working set, working set, committed, processes]]
        for the TOP_PROGRAMS biggest, "other": [private working set, processes,
        programs] for the rest, "total": private working set of them all}."""
        if now < self._next:
            return self.value
        self._next = now + self.EVERY
        ret = ULONG()
        for _ in range(4):
            st = _ntq(SystemProcessInformation, self._buf, len(self._buf), ctypes.byref(ret)) & 0xFFFFFFFF
            if st != STATUS_INFO_LENGTH_MISMATCH:
                break
            self._buf = ctypes.create_string_buffer(max(2 * len(self._buf), ret.value + (1 << 16)))
        if st:
            self.value = None
            return None
        progs: dict[str, list[int]] = {}
        off, end = 0, ret.value
        while off + ctypes.sizeof(SYSTEM_PROCESS_INFORMATION) <= end:
            p = SYSTEM_PROCESS_INFORMATION.from_buffer(self._buf, off)
            pid = p.UniqueProcessId or 0
            if pid:   # not the idle process
                img = p.ImageName
                name = ctypes.wstring_at(img.Buffer, img.Length // 2) if img.Buffer and img.Length else f"PID {pid}"
                if name.lower().endswith(".exe"):
                    name = name[:-4]
                g = progs.setdefault(name, [0, 0, 0, 0])
                g[0] += p.WorkingSetPrivateSize
                g[1] += p.WorkingSetSize
                g[2] += p.PagefileUsage
                g[3] += 1
            if not p.NextEntryOffset:
                break
            off += p.NextEntryOffset
        rows = sorted(progs.items(), key=lambda kv: kv[1][0], reverse=True)
        rest = rows[TOP_PROGRAMS:]
        self.value = {"top": [[n, *v] for n, v in rows[:TOP_PROGRAMS]],
                      "other": [sum(v[0] for _, v in rest), sum(v[3] for _, v in rest), len(rest)],
                      "total": sum(v[0] for _, v in rows)}
        return self.value


def breakdown(usable: int, avail: int, lists: dict | None, rm: dict | None, comp: int) -> dict:
    """The RAM bar: what the memory in use holds, and the page lists, in bytes.

    lists: MemoryCounters values (exact), or None. rm: a RamMap sample, whose
    page-database sample splits the memory in use more finely, or None.
    comp: bytes of RAM holding compressed pages.
    Returns "lists" {free, modified, standby: [core, normal, reserve]} or None
    (then only "available" is known), "uses" [[kind, bytes]] in the order
    they're drawn, "kernel" [[USES index, bytes]] for other kernel memory,
    and "pagedAll" (the paged pool, including what's in the page file).
    """
    out = {"lists": None, "uses": [], "kernel": [], "pagedAll": lists["pagedAll"] if lists else 0}
    if lists:
        free, modified = lists["free"], lists["modified"]
        standby = [lists["core"], lists["normal"], lists["reserve"]]
    elif rm:
        st = rm["states"]
        free, modified = int(st[0] * PAGE), int(st[2] * PAGE)
        standby = [0, int(st[1] * PAGE), 0]   # the priorities aren't known
    else:
        free = modified = None
        standby = []
    if free is None:
        inuse = max(0, usable - avail)
    else:
        inuse = max(0, usable - free - modified - sum(standby))
        out["lists"] = {"free": free, "modified": modified, "standby": standby}

    if rm and sum(rm["uses"]):
        u = rm["uses"]
        tot = sum(u)
        share = {"proc": u[0] + u[9], "mapped": u[1], "shared": u[2], "paged": u[4], "nonpaged": u[5],
                 "ptables": u[3], "kernel": sum(u[i] for i in KERNEL_USES)}
        parts = {k: inuse * v / tot for k, v in share.items()}
        kernel = [[i, inuse * u[i] / tot] for i in KERNEL_USES]
        if lists:   # the counters are exact for the pools: use them, and share the rest as sampled
            pools = min(inuse, lists["paged"] + lists["nonpaged"])
            rest = sum(v for k, v in parts.items() if k not in ("paged", "nonpaged"))
            scale = (inuse - pools) / rest if rest else 0
            for k in parts:
                parts[k] *= scale
            kernel = [[i, b * scale] for i, b in kernel]
            parts["paged"] = min(lists["paged"], inuse)
            parts["nonpaged"] = pools - parts["paged"]
        c = min(comp, parts["proc"])
        parts["proc"] -= c
        order = [("proc", parts["proc"]), ("comp", c), ("mapped", parts["mapped"]), ("shared", parts["shared"]),
                 ("paged", parts["paged"]), ("nonpaged", parts["nonpaged"]), ("ptables", parts["ptables"]),
                 ("kernel", parts["kernel"])]
        out["kernel"] = [[i, int(b)] for i, b in kernel if b >= 1]
    elif lists:
        known = [("comp", comp), ("cache", lists["cache"]), ("paged", lists["paged"]),
                 ("nonpaged", lists["nonpaged"]), ("drivers", lists["drivers"] + lists["code"])]
        total = sum(b for _, b in known)
        scale = min(1.0, inuse / total) if total else 0
        known = [(k, b * scale) for k, b in known]
        order = [("apps", inuse - sum(b for _, b in known))] + known
    else:
        order = [("apps", inuse)]
    out["uses"] = [[k, int(b)] for k, b in order]
    return out


class CompressionStore:
    """RAM holding compressed pages: the working set of the Memory Compression process.

    Reading the Process counters takes several milliseconds, so this reads
    them every few seconds; the store changes slowly.
    """
    EVERY = 2.0

    def __init__(self) -> None:
        self.counters = None
        self.value = 0
        self._next = 0.0

    def sample(self, now: float) -> int:
        if now >= self._next:
            self._next = now + self.EVERY
            try:
                if self.counters is None:
                    self.counters = Counters({"ws": r"\Process(Memory Compression)\Working Set"}, "process")
                self.value = int(sum(self.counters.collect().get("ws", {}).values()))
            except OSError:   # no compression store, or no counters: try again much later
                self.value, self._next = 0, now + 60
        return self.value


# ---------------------------------------------------------------------------
# Live and simulated sources
# ---------------------------------------------------------------------------

class LiveSystem:
    def __init__(self, names) -> None:
        self.names = names
        self.parts = 1   # memory-map parts: samples a second
        self.ram = self.gpu = self.memc = None
        self.ram_err = self.gpu_err = self.memc_err = None
        self._ram_retry = self._gpu_retry = self._memc_retry = 0.0
        self._map_at = self._dimms_at = 0.0
        self._files, self._files_at = [], 0.0
        self.comp = CompressionStore()
        self.progs = Programs()
        try:
            self.dimms = memory_modules()
        except Exception:  # noqa: BLE001 - odd firmware tables just mean no module view
            self.dimms = None
        self.cpu = CpuMonitor()

    def _ensure(self, now: float) -> None:
        if self.ram is None and now >= self._ram_retry:
            try:
                self.ram, self.ram_err = RamMap(), None
                self.ram.set_parts(self.parts)
            except OSError as exc:
                self.ram_err, self._ram_retry = str(exc), now + 60
        if self.memc is None and now >= self._memc_retry:
            try:
                self.memc, self.memc_err = MemoryCounters(), None
            except OSError as exc:
                self.memc_err, self._memc_retry = str(exc), now + 60
        if self.gpu is None and now >= self._gpu_retry:
            try:
                self.gpu, self.gpu_err = GpuMonitor(self.names), None
            except OSError as exc:
                self.gpu_err, self._gpu_retry = str(exc), now + 60

    def set_rate(self, per_second: int) -> None:
        self.parts = per_second
        if self.ram:
            self.ram.set_parts(per_second)

    def sample(self, dt: float = 1.0) -> dict:
        now = time.monotonic()
        self._ensure(now)
        installed, usable, avail = memory_status()
        lists = rm = None
        err = self.ram_err
        if self.memc:
            try:
                lists = self.memc.sample()
            except OSError:
                lists = None
        detail = False
        if self.ram:
            detail = now >= self._map_at   # the address map goes out a few times a second at most
            try:
                rm = self.ram.sample(detail)
            except OSError as exc:
                err, detail = str(exc), False
            if detail:
                self._map_at = now + MAP_EVERY
        ram = {"installed": installed, "total": usable, "avail": avail, "err": err, "countersErr": self.memc_err}
        ram.update(breakdown(usable, avail, lists, rm, self.comp.sample(now)))
        if detail and rm["cells"] is not None:
            ram["map"] = {"slice": self.ram.slice, "cells": rm["cells"]}
        ram["progs"] = self.progs.sample(now)
        if self.dimms and now >= self._dimms_at:
            ram["dimms"], self._dimms_at = self.dimms, now + DIMMS_EVERY
        commit = commit_status()
        if commit:
            if now >= self._files_at:
                self._files, self._files_at = page_files(), now + 1.0
            commit["files"] = self._files
        out = {"ram": ram, "commit": commit, "cpu": self.cpu.sample(), "gpu": None, "gpuErr": self.gpu_err}
        if self.gpu:
            try:
                out["gpu"] = self.gpu.sample()
            except OSError as exc:
                out["gpuErr"] = str(exc)
        return out


DEMO_PROGRAMS = [   # name, share of process memory to start from, processes
    ("game", 4.2, 1), ("chrome", 2.6, 24), ("Code", 1.1, 9), ("msedgewebview2", 0.9, 8), ("firefox", 0.72, 11),
    ("svchost", 0.66, 82), ("Discord", 0.62, 6), ("Teams", 0.55, 5), ("steamwebhelper", 0.45, 7),
    ("obs64", 0.33, 1), ("explorer", 0.31, 1), ("steam", 0.28, 1), ("Spotify", 0.27, 5), ("python", 0.24, 2),
    ("MsMpEng", 0.21, 1), ("node", 0.19, 3), ("dwm", 0.18, 1), ("SearchHost", 0.12, 1), ("pwsh", 0.11, 2),
    ("OneDrive", 0.09, 1), ("WindowsTerminal", 0.08, 1), ("Widgets", 0.07, 1), ("RuntimeBroker", 0.06, 9),
    ("PhoneExperienceHost", 0.05, 1), ("nvcontainer", 0.04, 3), ("Registry", 0.04, 1), ("audiodg", 0.03, 1),
    ("sihost", 0.03, 1), ("ctfmon", 0.02, 1), ("lsass", 0.02, 1), ("System", 0.02, 1), ("csrss", 0.01, 2),
    ("Memory Compression", 0, 1)]


def _demo_module(locator: str, channel: str, size: int) -> dict:
    filled = {"type": "DDR5", "form": "DIMM", "speed": 6000, "configured": 6000, "brand": "G.Skill",
              "maker": "G.Skill", "part": "F5-6000J3038F16G", "rank": 1, "volts": 1350, "width": 64,
              "ranges": [[0, 32 * GB]]} if size else {
              "type": "", "form": "DIMM", "speed": 0, "configured": 0, "brand": "", "maker": "", "part": "",
              "rank": 0, "volts": 0, "width": 0, "ranges": []}
    return {"locator": locator, "bank": f"CHANNEL {channel}", "channel": channel, "socket": "", "size": size,
            "ecc": False, **filled}


# Two 16 GB modules in the second slot of each channel, the first slots empty
DEMO_DIMMS = {"slots": 4, "max": 192 * GB, "ecc": False, "mapping": "interleaved",
              "arrays": [[0, int(3.25 * GB)], [4 * GB, int(32.75 * GB)]],
              "modules": [_demo_module("DIMM 0", "A", 0), _demo_module("DIMM 1", "A", 16 * GB),
                          _demo_module("DIMM 0", "B", 0), _demo_module("DIMM 1", "B", 16 * GB)]}


class DemoSystem:
    """Plausible made-up numbers: 32 GB of RAM with the usual gap below 4 GB, a 24 GB GPU."""

    SLICE = 128 << 20

    def __init__(self) -> None:
        self.rng = rng = random.Random()
        self.t0 = time.monotonic()
        self.level = 0.5                                             # share of usable RAM in use
        self.mix = [0.50, 0.03, 0.20, 0.06, 0.06, 0.05, 0.02, 0.08]   # what the memory in use holds
        self.progs = [list(p) for p in DEMO_PROGRAMS]
        # 128 MB slices of address space up to 32.75 GB. RAM from 0 to 3.25 GB,
        # then from 4 GB: the gap is where devices are mapped.
        per_gb = GB // self.SLICE
        self.has_ram = [not (3.25 * per_gb <= s < 4 * per_gb) for s in range(int(32.75 * per_gb))]
        self.slice_level = [min(0.97, max(0.05, (0.9 if s < 3 * per_gb else 0.45) + rng.uniform(-0.3, 0.3)))
                            for s in range(len(self.has_ram))]
        self.map_at = self.dimms_at = 0.0
        self.vram_used = 7.5 * GB
        self.cpu = 15.0
        self.commit_extra = 6 * GB    # committed but not (yet) using RAM
        self.commit_peak = 0

    def set_rate(self, per_second: int) -> None:
        pass

    def _cells(self, w: float) -> list:
        """The address map, like RamMap.sample's cells."""
        per_gb = GB // self.SLICE
        cells = []
        for s, ram in enumerate(self.has_ram):
            if not ram:
                cells.append(None)
                continue
            lvl = self.slice_level[s] = min(0.98, max(0.03, self.slice_level[s] + self.rng.gauss(0, 0.02) * w))
            mod = min(1 - lvl, 0.01 + self.rng.random() * 0.02)
            cache = (1 - lvl - mod) * (0.85 if s < 24 * per_gb else 0.4)
            free = max(0.0, 1 - lvl - mod - cache)
            top = [[5, 0.45], [6, 0.3], [0, 0.2]] if s < 2 * per_gb else [[0, 0.6], [1, 0.25], [2, 0.1]]
            cell = [1000, round(lvl * 1000), round(mod * 1000), round(cache * 1000), round(free * 1000)]
            for i, f in top:
                cell += (i, round(lvl * f * 1000))
            cells.append(cell)
        return cells

    def sample(self, dt: float = 1.0) -> dict:
        # Random walks step by sqrt(dt), so they wander as fast at any sample rate.
        rng, t, w = self.rng, time.monotonic() - self.t0, math.sqrt(max(0.01, min(1.0, dt)))
        usable = int(31.8 * GB)
        self.level = min(0.85, max(0.25, self.level + rng.gauss(0, 0.004) * w))
        inuse = usable * self.level
        modified = usable * (0.015 + 0.005 * math.sin(t / 11))
        cached = (usable - inuse - modified) * 0.72
        free = usable - inuse - modified - cached
        for i in range(len(self.mix)):
            self.mix[i] = max(0.005, self.mix[i] * (1 + rng.gauss(0, 0.01) * w))
        mix = [m / sum(self.mix) for m in self.mix]
        names = ("proc", "comp", "mapped", "shared", "paged", "nonpaged", "ptables", "kernel")
        kernel = inuse * mix[7]
        ram = {"installed": 32 * GB, "total": usable, "avail": int(cached + free), "err": None, "countersErr": None,
               "lists": {"free": int(free), "modified": int(modified),
                         "standby": [int(cached * 0.02), int(cached * 0.9), int(cached * 0.08)]},
               "uses": [[n, int(inuse * f)] for n, f in zip(names, mix)],
               "kernel": [[6, int(kernel * 0.35)], [7, int(kernel * 0.1)], [8, int(kernel * 0.2)],
                          [10, int(kernel * 0.2)], [11, int(kernel * 0.15)]],
               "pagedAll": int(inuse * mix[4] * 1.08)}
        # Programs share process memory; Memory Compression is the compressed memory.
        for p in self.progs:
            p[1] = max(0.005, p[1] * (1 + rng.gauss(0, 0.02) * w))
        k = inuse * mix[0] / sum(p[1] for p in self.progs if p[0] != "Memory Compression")
        rows = sorted(((n, inuse * mix[1] if n == "Memory Compression" else s * k, c) for n, s, c in self.progs),
                      key=lambda r: r[1], reverse=True)
        rest = rows[TOP_PROGRAMS:]
        ram["progs"] = {"top": [[n, int(b), int(b * 1.5), int(b * 1.2), c] for n, b, c in rows[:TOP_PROGRAMS]],
                        "other": [int(sum(r[1] for r in rest)), sum(r[2] for r in rest), len(rest)],
                        "total": int(sum(r[1] for r in rows))}
        now = time.monotonic()
        if now >= self.map_at:
            self.map_at = now + MAP_EVERY
            ram["map"] = {"slice": self.SLICE, "cells": self._cells(w)}
        if now >= self.dimms_at:
            ram["dimms"], self.dimms_at = DEMO_DIMMS, now + DIMMS_EVERY
        used = inuse + modified
        self.vram_used = min(23 * GB, max(3 * GB, self.vram_used + rng.gauss(0, 0.15) * w * GB))
        burst = 0.5 + 0.5 * math.sin(t / 7)
        g3d = min(100.0, max(0.0, 30 + 55 * burst + rng.gauss(0, 6)))
        self.cpu = min(100.0, max(2.0, self.cpu + rng.gauss(0, 4) * w + (18 - self.cpu) * 0.1 * dt))
        game = self.vram_used * 0.7
        self.commit_extra = min(14 * GB, max(2 * GB, self.commit_extra + rng.gauss(0, 0.25) * w * GB))
        committed = int(used * 0.8 + self.commit_extra)
        self.commit_peak = max(self.commit_peak, committed, int(26 * GB))
        pagefile = 12 * GB
        return {
            "ram": ram,
            "commit": {"used": committed, "limit": usable + pagefile, "peak": self.commit_peak,
                       "files": [["C:\\pagefile.sys", pagefile, int(0.6 * GB), int(1.4 * GB)]]},
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


INTERVALS = (50, 100, 250, 500, 1000)   # ms between samples: 20, 10, 4, 2 or 1 a second


class SystemMonitor:
    """Publishes a sample at the chosen rate while anyone is watching.

    Each message carries "t" (seconds, this process's monotonic clock) and
    "iv" (the interval in ms), so the view can place samples in time and show
    the rate in use.
    """

    def __init__(self, hub, mode: str, names) -> None:
        self.hub, self.mode, self.names = hub, mode, names
        self.interval = 1000
        self._stop = threading.Event()
        self._wake = threading.Event()   # set when the interval changes
        self._source = None

    def start(self) -> None:
        threading.Thread(target=self._run, name="sysmon", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def set(self, interval: int) -> None:
        if interval not in INTERVALS:
            raise ValueError(f"interval must be one of {INTERVALS}")
        if interval != self.interval:
            self.interval = interval
            self._wake.set()

    def _run(self) -> None:
        try:
            source = DemoSystem() if self.mode == "demo" else LiveSystem(self.names)
        except Exception as exc:  # noqa: BLE001 - reported to the view
            source, err = None, str(exc)
        applied = None
        last = time.monotonic()
        deadline = last
        while not self._stop.is_set():
            iv = self.interval
            if source and iv != applied:
                source.set_rate(round(1000 / iv))
                applied = iv
            now = time.monotonic()
            dt, last = now - last, now
            if self.hub.viewers():
                try:
                    msg = source.sample(dt) if source else {"err": err}
                except Exception as exc:  # noqa: BLE001
                    msg = {"err": f"{type(exc).__name__}: {exc}"}
                msg["t"] = round(now, 3)
                msg["iv"] = iv
                self.hub.publish(json.dumps(msg, separators=(",", ":")), "sys")
            # Absolute deadlines keep the rate steady; after a stall, start afresh.
            deadline = max(deadline + iv / 1000, time.monotonic())
            if self._wake.wait(max(0.0, deadline - time.monotonic())):
                self._wake.clear()
                deadline = time.monotonic()


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

"""Which processes do disk I/O, per drive letter: Windows' Microsoft-Windows-Kernel-Disk events (ETW).

Each read and write a disk completes comes as an event with the disk's number, the offset on it
and the bytes; its header names the process that asked for it. The offset says which volume
(drive letter) it was on, from the volumes' extents. DiskTrace runs a real-time session of its
own while it's wanted: the app's for this PC (it runs elevated), the Windows agent's for its viewers.

take() gives what happened since the last time, as rates: {letter: [[name, pid, read B/s,
written B/s, reads/s, writes/s]...]}, the busiest PROCS of each and the rest as "(other)".
"""
from __future__ import annotations

import ctypes
import os
import struct
import threading
import time
from ctypes import wintypes

import netwatch
import server

KERNEL_DISK_GUID = server.GUID.parse("C7BDE69A-E1E0-4177-B6EF-283AD1525271")
DISK_READ, DISK_WRITE = 10, 11
# The start of their payload: DiskNumber u32, IrpFlags u32, TransferSize u32, Reserved u32, ByteOffset u64
# (then FileObject, Irp and HighResResponseTime: 48 bytes in all on 64-bit Windows 11)
PAYLOAD = struct.Struct("<IIIIQ")
OFF_PID = server.EVENT_RECORD.EventHeader.offset + server.EVENT_HEADER.ProcessId.offset
PROCS = 16

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             wintypes.DWORD, wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
INVALID_HANDLE = wintypes.HANDLE(-1).value


def extents(letter: str) -> list[tuple[int, int, int]]:
    """A drive letter's volume as [(disk number, first byte, bytes)] on the disks it's on."""
    h = _k32.CreateFileW(f"\\\\.\\{letter.rstrip(':')}:", 0, 3, None, 3, 0, None)   # no access needed; share read/write
    if not h or h == INVALID_HANDLE:
        return []
    try:
        buf = ctypes.create_string_buffer(8 + 24 * 32)
        got = wintypes.DWORD()
        if not _k32.DeviceIoControl(h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, 0, buf, len(buf), ctypes.byref(got), None):
            return []
        n = struct.unpack_from("<I", buf.raw, 0)[0]
        return [struct.unpack_from("<I4xqq", buf.raw, 8 + 24 * i) for i in range(min(n, 32))]
    finally:
        _k32.CloseHandle(h)


class DiskTrace(server.EtwCapture):
    """Kernel-Disk's reads and writes, per drive letter and process, while it runs."""

    def __init__(self, letters, session: tuple[str, server.GUID]) -> None:
        super().__init__(None, session=session)
        self.watch_own = False
        self.lock = threading.Lock()
        self.letters = [l.rstrip(":").upper() for l in letters]
        self.where: list[tuple[int, int, int, str]] = []   # (disk, first byte, end, letter)
        self.counts: dict[str, dict[int, list]] = {}       # letter -> pid -> [read B, written B, reads, writes]
        self.since = time.monotonic()
        self.names: dict[int, str] = {}
        self.names_at = 0.0
        self.look()

    def look(self) -> None:
        """Where each letter is: now and then, as volumes come and go."""
        where = []
        for letter in self.letters:
            for disk, first, size in extents(letter):
                where.append((disk, first, first + size, letter))
        self.where = where

    def _enable(self) -> None:
        rc = self.adv.EnableTraceEx2(self.session, ctypes.byref(KERNEL_DISK_GUID), server.EVENT_CONTROL_CODE_ENABLE_PROVIDER,
                                     server.TRACE_LEVEL_VERBOSE, 0xFFFFFFFFFFFFFFFF, 0, 0, None)
        if rc:
            self.stop()
            raise OSError(rc, f"Couldn't enable Kernel-Disk: {ctypes.FormatError(rc).strip()}")

    def _on_event(self, rec: int) -> None:
        try:
            eid = ctypes.c_uint16.from_address(rec + server.OFF_EVENT_ID).value
            if eid not in (DISK_READ, DISK_WRITE):
                return
            n = ctypes.c_uint16.from_address(rec + server.OFF_USER_LEN).value
            if n < PAYLOAD.size:
                return
            data = ctypes.string_at(ctypes.c_void_p.from_address(rec + server.OFF_USER_DATA).value, PAYLOAD.size)
            disk, _, size, _, offset = PAYLOAD.unpack(data)
            letter = next((w[3] for w in self.where if w[0] == disk and w[1] <= offset < w[2]), None)
            if letter is None:
                return
            pid = ctypes.c_uint32.from_address(rec + OFF_PID).value   # the process that asked for it
            with self.lock:
                c = self.counts.setdefault(letter, {}).setdefault(pid, [0, 0, 0, 0])
                write = eid == DISK_WRITE
                c[1 if write else 0] += size
                c[3 if write else 2] += 1
        except Exception:  # never into ETW's thread
            pass

    def _name(self, pid: int) -> str:
        if pid not in self.names and time.monotonic() - self.names_at > 1:
            self.names = {p: os.path.splitext(v[1])[0] for p, v in netwatch.snapshot().items()}
            self.names[0] = "System"
            self.names_at = time.monotonic()
        return self.names.get(pid) or f"PID {pid}"

    def take(self) -> dict[str, list]:
        """What happened since the last time, as rates per letter: [[name, pid, read B/s, written B/s, reads/s, writes/s]]."""
        with self.lock:
            counts, self.counts = self.counts, {}
        mono = time.monotonic()
        dt, self.since = max(0.05, mono - self.since), mono
        out = {}
        for letter in self.letters:
            rows = sorted(counts.get(letter, {}).items(), key=lambda kv: -(kv[1][0] + kv[1][1]))
            top = [[self._name(pid), pid] + [round(x / dt, 1) for x in c] for pid, c in rows[:PROCS]]
            rest = [sum(c[i] for _, c in rows[PROCS:]) for i in range(4)]
            if any(rest):
                top.append(["(other)", 0] + [round(x / dt, 1) for x in rest])
            out[letter] = top
        return out


class ProcIO:
    """Processes' disk I/O for whoever wants it, and only while someone does: a DiskTrace, started when
    first wanted, ended a while after the last stops wanting it."""

    IDLE = 10.0

    def __init__(self, session: tuple[str, server.GUID], letters) -> None:
        self.session, self.letters = session, letters
        self.trace: DiskTrace | None = None
        self.error, self.retry, self.idle_since, self.looked = "", 0.0, 0.0, 0.0

    def take(self, wanted: bool) -> tuple[dict, str] | None:
        """While wanted: ({letter: rows}, error) since the last call; None when not wanted."""
        mono = time.monotonic()
        if not wanted:
            if self.trace is not None:
                self.idle_since = self.idle_since or mono
                if mono - self.idle_since > self.IDLE:
                    self.trace.stop()
                    self.trace = None
            return None
        self.idle_since = 0.0
        letters = [l.rstrip(":").upper() for l in self.letters()]
        if self.trace is not None and letters != self.trace.letters:
            self.trace.letters = letters
            self.trace.look()
        if self.trace is None and mono >= self.retry:
            try:
                t = DiskTrace(letters, self.session)
                t.start()
                self.trace, self.error = t, ""
            except OSError as exc:
                self.error, self.retry = f"Windows won't say which processes do disk I/O: {exc.strerror or exc}", mono + 30
        if self.trace is None:
            return {}, self.error
        if mono - self.looked > 10:
            self.looked = mono
            self.trace.look()
        return self.trace.take(), ""

    def stop(self) -> None:
        if self.trace is not None:
            self.trace.stop()
            self.trace = None

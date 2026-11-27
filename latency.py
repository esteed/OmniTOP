"""Latency and jitter of network adapters: round trips to each one's default gateway.

While a machine's Network page shows, each adapter with a default gateway (IPv4) has it
pinged a few times a second, from that adapter's own address. Its page shows the average
round trip of the last WINDOW seconds, and the jitter: how much one round trip differs from
the next, on average. The first hop is the part of the path the adapter's own link decides,
so that's what is measured, and it says no more about the rest of the way.

Here: Window, which both the app and its agents' figures go through, and for Windows,
the gateways and addresses of adapters and Echoes, which pings them (this PC's, and the
Windows agent's). The Linux agent has its own Echoes.
"""
from __future__ import annotations

import collections
import ctypes
import socket
import struct
import sys
import threading
import time

WINDOW = 3.0                 # seconds averaged
TIMEOUT_MS = 1000            # a reply that takes longer is lost
MIN_MS, MAX_MS = 100, 10000  # between pings of one gateway
LOOK_EVERY = 2.0             # seconds between looks at which gateways there are
MAX_TARGETS = 16
PAYLOAD = b"OmniTOP latency." * 2   # 32 bytes, like ping's


class Window:
    """The round trips of the last WINDOW seconds of each adapter, and what they add up to."""

    def __init__(self, seconds: float = WINDOW) -> None:
        self.seconds = seconds
        self.rtts: dict[str, collections.deque] = {}

    def add(self, key: str, t: float, rtt: float | None) -> None:
        """A round trip in ms, or None for one that was lost."""
        self.rtts.setdefault(key, collections.deque(maxlen=4096)).append((t, rtt))

    def summary(self, t: float) -> dict[str, list]:
        """key -> [average ms, jitter ms, lost, pings]: None for what there's nothing to tell of."""
        out = {}
        for key, d in list(self.rtts.items()):
            while d and d[0][0] <= t - self.seconds:
                d.popleft()
            if not d:
                del self.rtts[key]
                continue
            got = [r for _, r in d if r is not None]
            avg = sum(got) / len(got) if got else None
            jitter = sum(abs(b - a) for a, b in zip(got, got[1:])) / (len(got) - 1) if len(got) > 1 else None
            out[key] = [None if avg is None else round(avg, 3), None if jitter is None else round(jitter, 3),
                        len(d) - len(got), len(d)]
        return out

    def clear(self) -> None:
        self.rtts.clear()


if sys.platform == "win32":
    from ctypes import wintypes

    _ip = ctypes.WinDLL("iphlpapi", use_last_error=True)
    _ip.GetIpForwardTable2.argtypes = [ctypes.c_ushort, ctypes.POINTER(ctypes.c_void_p)]
    _ip.GetIpForwardTable2.restype = wintypes.ULONG
    _ip.GetUnicastIpAddressTable.argtypes = [ctypes.c_ushort, ctypes.POINTER(ctypes.c_void_p)]
    _ip.GetUnicastIpAddressTable.restype = wintypes.ULONG
    _ip.FreeMibTable.argtypes = [ctypes.c_void_p]
    _ip.IcmpCreateFile.restype = wintypes.HANDLE
    _ip.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
    _ip.IcmpSendEcho2Ex.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.WORD,
                                    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    _ip.IcmpSendEcho2Ex.restype = wintypes.DWORD
    AF_INET = 2
    ROUTE_ROW, ADDRESS_ROW = 104, 80              # MIB_IPFORWARD_ROW2, MIB_UNICASTIPADDRESS_ROW
    INVALID_HANDLE = wintypes.HANDLE(-1).value

    def _table(get, row_size: int) -> list[bytes]:
        table = ctypes.c_void_p()
        if get(AF_INET, ctypes.byref(table)):
            return []
        try:
            count = ctypes.c_ulong.from_address(table.value).value
            return [ctypes.string_at(table.value + 8 + i * row_size, row_size) for i in range(count)]
        finally:
            _ip.FreeMibTable(table)

    def gateways() -> dict[int, str]:
        """Adapter LUID -> its default gateway (IPv4), the one with the lowest metric."""
        best: dict[int, tuple[int, str]] = {}
        for row in _table(_ip.GetIpForwardTable2, ROUTE_ROW):
            luid = struct.unpack_from("<Q", row, 0)[0]
            dest, length = row[16:20], row[40]
            if struct.unpack_from("<H", row, 12)[0] != AF_INET or length or dest != b"\0\0\0\0":
                continue                                  # not a default route
            hop = row[48:52]
            if struct.unpack_from("<H", row, 44)[0] != AF_INET or hop == b"\0\0\0\0":
                continue                                  # on-link, no gateway
            metric = struct.unpack_from("<I", row, 84)[0]
            if luid not in best or metric < best[luid][0]:
                best[luid] = (metric, socket.inet_ntoa(hop))
        return {luid: gw for luid, (_, gw) in best.items()}

    def addresses() -> dict[int, list[tuple[str, int]]]:
        """Adapter LUID -> its IPv4 addresses that can be sent from, with their prefix lengths."""
        out: dict[int, list[tuple[str, int]]] = {}
        for row in _table(_ip.GetUnicastIpAddressTable, ADDRESS_ROW):
            if struct.unpack_from("<H", row, 0)[0] != AF_INET or row[61]:   # SkipAsSource
                continue
            out.setdefault(struct.unpack_from("<Q", row, 32)[0], []).append((socket.inet_ntoa(row[4:8]), row[60]))
        return out

    def _same_net(a: str, b: str, length: int) -> bool:
        mask = (0xFFFFFFFF << (32 - length)) & 0xFFFFFFFF if length else 0
        return (struct.unpack("!I", socket.inet_aton(a))[0] & mask) == (struct.unpack("!I", socket.inet_aton(b))[0] & mask)

    def targets(luids) -> dict[int, tuple[str, str]]:
        """LUID -> (address to send from, gateway), for those of the adapters that have a gateway."""
        gws, addrs, out = gateways(), addresses(), {}
        for luid in luids:
            gw = gws.get(luid)
            if not gw:
                continue
            mine = addrs.get(luid, [])
            src = next((a for a, n in mine if _same_net(a, gw, n)), mine[0][0] if mine else "")
            out[luid] = (src, gw)
        return out

    class Pinger:
        """ICMP echo through Windows' own API, which needs no rights."""

        def __init__(self) -> None:
            self.h = _ip.IcmpCreateFile()
            if not self.h or self.h == INVALID_HANDLE:
                raise ctypes.WinError(ctypes.get_last_error())
            self.reply = ctypes.create_string_buffer(256 + len(PAYLOAD))
            self.data = ctypes.create_string_buffer(PAYLOAD, len(PAYLOAD))

        def ping(self, source: str, dest: str, timeout_ms: int = TIMEOUT_MS) -> float | None:
            """The round trip in ms, or None if no answer came in time."""
            src = struct.unpack("<I", socket.inet_aton(source))[0] if source else 0
            dst = struct.unpack("<I", socket.inet_aton(dest))[0]
            start = time.perf_counter()
            n = _ip.IcmpSendEcho2Ex(self.h, None, None, None, src, dst, self.data, len(PAYLOAD), None,
                                    self.reply, len(self.reply), timeout_ms)
            took = (time.perf_counter() - start) * 1000
            if not n:
                return None
            address, status = struct.unpack_from("<II", self.reply.raw, 0)
            if status != 0 or address != dst:             # not an answer from the gateway
                return None
            return took

        def close(self) -> None:
            if self.h:
                _ip.IcmpCloseHandle(self.h)
                self.h = None


class Echoes:
    """Pings some gateways every `ms`, each on a thread of its own, until stop().

    find() -> {key: (address to send from, gateway)} says what to ping; it's asked every
    LOOK_EVERY seconds. Every `ms`, report(gateways, results, error) gets {key: gateway} and the
    round trips that ended since the last report, as [(key, time, ms or None)].
    """

    def __init__(self, find, report, ms: int = 250, pinger=None) -> None:
        self.find, self.report = find, report
        self.ms = max(MIN_MS, min(MAX_MS, int(ms)))
        self.pinger = pinger or Pinger
        self.lock = threading.Lock()
        self.results: list = []
        self.workers: dict[str, list] = {}             # key -> [(source, gateway), stop event]
        self.stopping = threading.Event()
        self.error = ""
        self.thread = threading.Thread(target=self._run, name="latency", daemon=True)

    def start(self) -> "Echoes":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        with self.lock:
            for w in self.workers.values():
                w[1].set()
            self.workers = {}

    def _look(self) -> dict[str, str]:
        try:
            want = dict(list(self.find().items())[:MAX_TARGETS])
            self.error = ""
        except OSError as exc:
            want, self.error = {}, f"Couldn't read the gateways: {exc.strerror or exc}"
        with self.lock:
            for key, w in list(self.workers.items()):
                if want.get(key) != w[0]:
                    w[1].set()
                    del self.workers[key]
            for key, target in want.items():
                if key not in self.workers:
                    stop = threading.Event()
                    self.workers[key] = [target, stop]
                    threading.Thread(target=self._ping, args=(key, target, stop), name=f"ping {target[1]}",
                                     daemon=True).start()
        return {key: target[1] for key, target in want.items()}

    def _ping(self, key: str, target: tuple[str, str], stop: threading.Event) -> None:
        try:
            p = self.pinger()
        except OSError as exc:
            self.error = f"Can't ping: {exc.strerror or exc}"
            return
        try:
            due = time.perf_counter()
            while not stop.is_set():
                sent = time.time()
                try:
                    rtt = p.ping(*target)
                except OSError as exc:
                    self.error, rtt = f"Can't ping {target[1]}: {exc.strerror or exc}", None
                    stop.wait(1.0)
                if stop.is_set():
                    return
                with self.lock:
                    self.results.append((key, sent, rtt))
                due += self.ms / 1000
                wait = due - time.perf_counter()
                if wait < 0:
                    due, wait = time.perf_counter(), 0
                stop.wait(wait)
        finally:
            p.close()

    def _run(self) -> None:
        gws, look_at = {}, 0.0
        while not self.stopping.is_set():
            if time.monotonic() >= look_at:
                gws, look_at = self._look(), time.monotonic() + LOOK_EVERY
            self.stopping.wait(self.ms / 1000)
            with self.lock:
                done, self.results = self.results, []
            if not self.stopping.is_set():
                try:
                    self.report(gws, done, self.error)
                except Exception:  # noqa: BLE001 - one bad report mustn't end the pinging
                    pass


if __name__ == "__main__":
    # What this PC's adapters would show: python latency.py
    import perfmon
    names = {a["luid"]: a["alias"] for a in perfmon.adapters()}
    found = targets(names)
    print({names.get(k, k): v for k, v in found.items()})
    w = Window()

    def report(gws, done, error):
        for key, t, rtt in done:
            w.add(key, t, rtt)
        if error:
            print("error:", error)

    e = Echoes(lambda: {str(k): v for k, v in targets(names).items()}, report).start()
    time.sleep(4)
    e.stop()
    for key, (avg, jitter, lost, n) in w.summary(time.time()).items():
        print(f"{names.get(int(key), key)}: {avg} ms, jitter {jitter} ms, {lost} of {n} lost")

"""Latency and jitter of network adapters: round trips to Google, Microsoft and AT&T.

While a machine's Network page shows, each adapter that has a default gateway (IPv4), the
way to the internet, times round trips from its own address to SITES: Google's, Microsoft's
and AT&T's own networks, twice a second each. Its page shows, for the last WINDOW seconds,
each site's average round trip, stacked, with the average of the sites on top; and the
jitter: how much one round trip to a site differs from the next to that site, on average
over the sites.

A round trip is an ICMP echo (ping). Where a site doesn't answer those (some networks
block them), it's timed by the TCP handshake with its HTTPS port instead: connecting, which
takes one round trip, then closing at once.

Here: Window, which both the app and its agents' figures go through; and, used by the app
for this PC and by the Windows agent, resolve, Prober and Echoes, with Windows' own ping
functions. The Linux agent has its own.

Agents are told what to time: "echo <ms> google=google.com microsoft=microsoft.com ...";
plain "echo <ms>" times each adapter's gateway, as 2.2's agents did, and their rows say
nothing of a site.
"""
from __future__ import annotations

import collections
import ctypes
import ipaddress
import select
import socket
import struct
import sys
import threading
import time

WINDOW = 3.0                 # seconds averaged
TIMEOUT_MS = 1000            # a reply that takes longer is lost
MIN_MS, MAX_MS = 100, 10000  # between round trips to one site, from one adapter
SITE_MS = 500                # what the app asks for
SITES = (("google", "Google", "google.com"), ("microsoft", "Microsoft", "microsoft.com"), ("att", "AT&T", "att.com"))
GATEWAY = "gateway"          # the site of 2.2's agents' rows
LOOK_EVERY = 2.0             # seconds between looks at which adapters and gateways there are
RESOLVE_EVERY = 300.0        # and between looking up the sites' addresses
TCP_AFTER = 6                # pings unanswered in a row before a site is timed by TCP handshakes
TCP_PORT = 443
MAX_TARGETS = 32
MAX_SITES = 4
PAYLOAD = b"OmniTOP latency." * 2   # 32 bytes, like ping's


def command(ms: int = SITE_MS) -> str:
    """What an agent is told: time SITES every ms."""
    return f"echo {ms} " + " ".join(f"{name}={host}" for name, _, host in SITES) + "\n"


class Window:
    """The round trips of the last WINDOW seconds, per adapter and site, and what they add up to."""

    def __init__(self, seconds: float = WINDOW) -> None:
        self.seconds = seconds
        self.rtts: dict[tuple[str, str], collections.deque] = {}
        self.methods: dict[tuple[str, str], str] = {}

    def add(self, adapter: str, site: str, t: float, rtt: float | None, method: str = "icmp") -> None:
        """A round trip in ms, or None for one that was lost."""
        self.rtts.setdefault((adapter, site), collections.deque(maxlen=4096)).append((t, rtt))
        self.methods[(adapter, site)] = method

    def summary(self, t: float) -> dict[str, dict]:
        """adapter -> {"sites": {site: [average, jitter, lost, round trips, method]}, "avg": the sites'
        averages averaged, "jitter": their jitters averaged, "lost", "pings"}. None for what there's
        nothing to tell of."""
        out: dict[str, dict] = {}
        for (adapter, site), d in list(self.rtts.items()):
            while d and d[0][0] <= t - self.seconds:
                d.popleft()
            if not d:
                del self.rtts[(adapter, site)]
                continue
            got = [r for _, r in d if r is not None]
            avg = sum(got) / len(got) if got else None
            jitter = sum(abs(b - a) for a, b in zip(got, got[1:])) / (len(got) - 1) if len(got) > 1 else None
            a = out.setdefault(adapter, {"sites": {}})
            a["sites"][site] = [None if avg is None else round(avg, 3), None if jitter is None else round(jitter, 3),
                                len(d) - len(got), len(d), self.methods.get((adapter, site), "icmp")]
        for a in out.values():
            avgs = [s[0] for s in a["sites"].values() if s[0] is not None]
            jits = [s[1] for s in a["sites"].values() if s[1] is not None]
            a["avg"] = round(sum(avgs) / len(avgs), 3) if avgs else None
            a["jitter"] = round(sum(jits) / len(jits), 3) if jits else None
            a["lost"] = sum(s[2] for s in a["sites"].values())
            a["pings"] = sum(s[3] for s in a["sites"].values())
        return out

    def clear(self) -> None:
        self.rtts.clear()


_resolved: dict[str, tuple[str, float]] = {}


def resolve(host: str) -> str:
    """A site's IPv4 address, looked up now and then. OSError if it has none, or none on the internet."""
    hit = _resolved.get(host)
    if hit and time.monotonic() < hit[1]:
        return hit[0]
    try:
        addrs = [i[4][0] for i in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)]
    except (OSError, UnicodeError) as exc:
        raise OSError(f"{host} can't be looked up ({getattr(exc, 'strerror', None) or exc})") from None
    addrs = [a for a in addrs if ipaddress.IPv4Address(a).is_global]
    if not addrs:
        raise OSError(f"{host} isn't on the internet")
    _resolved[host] = (addrs[0], time.monotonic() + RESOLVE_EVERY)
    return addrs[0]


def tcp_rtt(source: str, dest: str, port: int = TCP_PORT, timeout: float = TIMEOUT_MS / 1000) -> float | None:
    """The time a TCP handshake takes, in ms: one round trip. A refusal is an answer too."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # closed at once, nothing left over
        if source:
            s.bind((source, 0))
        s.setblocking(False)
        start = time.perf_counter()
        s.connect_ex((dest, port))
        _, done, failed = select.select([], [s], [s], timeout)
        took = (time.perf_counter() - start) * 1000
        if not done and not failed:
            return None
        err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        return took if err in (0, 111, 10061) else None    # connected, or refused
    except OSError:
        return None
    finally:
        s.close()


class Prober:
    """Round trips from one address to another: pings, or TCP handshakes once pings go unanswered."""

    def __init__(self, source: str, dest: str, pinger) -> None:
        self.source, self.dest, self.pinger = source, dest, pinger
        self.misses, self.method = 0, "icmp"

    def ping(self) -> tuple[float | None, str]:
        if self.method == "icmp":
            rtt = self.pinger.ping(self.source, self.dest)
            if rtt is not None:
                self.misses = 0
            else:
                self.misses += 1
                if self.misses >= TCP_AFTER and tcp_rtt(self.source, self.dest) is not None:
                    self.method = "tcp"
            return rtt, "icmp"
        return tcp_rtt(self.source, self.dest), "tcp"

    def close(self) -> None:
        self.pinger.close()


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

    def _table(get, row_size: int, family: int = AF_INET) -> list[bytes]:
        table = ctypes.c_void_p()
        if get(family, ctypes.byref(table)):
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

    def address_keys() -> dict[bytes, str]:
        """Every local address (IPv4 and IPv6, as bytes) -> its adapter's LUID, as text: the key the app knows it by."""
        out: dict[bytes, str] = {}
        for row in _table(_ip.GetUnicastIpAddressTable, ADDRESS_ROW, 0):
            family = struct.unpack_from("<H", row, 0)[0]
            luid = str(struct.unpack_from("<Q", row, 32)[0])
            if family == AF_INET:
                out[row[4:8]] = luid
            elif family == 23:                            # AF_INET6: sin6_addr
                out[row[8:24]] = luid
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

    def plan(adapters: dict[int, str], sites) -> tuple[dict[str, tuple[str, str]], dict, str]:
        """What to time: ({"adapter|site": (from, to)}, {"g": {adapter: gateway}}, problem) for adapters by
        LUID -> name, and sites as [(name, host)], or none for the gateways."""
        found, problems = targets(adapters), []
        out = {}
        for luid, (src, gw) in found.items():
            for name, host in sites or [(GATEWAY, gw)]:
                try:
                    out[f"{adapters[luid]}|{name}"] = (src, resolve(host) if host != gw else gw)
                except OSError as exc:
                    if str(exc) not in problems:
                        problems.append(str(exc))
        return dict(list(out.items())[:MAX_TARGETS]), {"g": {adapters[luid]: gw for luid, (_, gw) in found.items()}}, "; ".join(problems)

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
            if status != 0 or address != dst:             # not an answer from there
                return None
            return took

        def close(self) -> None:
            if self.h:
                _ip.IcmpCloseHandle(self.h)
                self.h = None


class Echoes:
    """Times round trips to some addresses every `ms`, each on a thread of its own, until stop().

    find() -> ({key: (address to send from, address to time)}, info, problem) says what; it's asked
    every LOOK_EVERY seconds. Every `ms`, report(info, results, error) gets the round trips that ended
    since the last report, as [(key, time, ms or None, "icmp" | "tcp")].
    """

    def __init__(self, find, report, ms: int = SITE_MS, pinger=None) -> None:
        self.find, self.report = find, report
        self.ms = max(MIN_MS, min(MAX_MS, int(ms)))
        self.pinger = pinger or Pinger
        self.lock = threading.Lock()
        self.results: list = []
        self.workers: dict[str, list] = {}             # key -> [(source, dest), stop event]
        self.stopping = threading.Event()
        self.error = self.problem = ""
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

    def _look(self) -> dict:
        try:
            want, info, self.problem = self.find()
            self.error = ""
        except OSError as exc:
            want, info, self.problem = {}, {}, f"Couldn't read the adapters' gateways: {exc.strerror or exc}"
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
        return info

    def _ping(self, key: str, target: tuple[str, str], stop: threading.Event) -> None:
        try:
            p = Prober(target[0], target[1], self.pinger())
        except OSError as exc:
            self.error = f"Can't ping: {exc.strerror or exc}"
            return
        try:
            due = time.perf_counter()
            while not stop.is_set():
                sent = time.time()
                try:
                    rtt, method = p.ping()
                except OSError as exc:
                    self.error, rtt, method = f"Can't ping {target[1]}: {exc.strerror or exc}", None, "icmp"
                    stop.wait(1.0)
                if stop.is_set():
                    return
                with self.lock:
                    self.results.append((key, sent, rtt, method))
                due += self.ms / 1000
                wait = due - time.perf_counter()
                if wait < 0:
                    due, wait = time.perf_counter(), 0
                stop.wait(wait)
        finally:
            p.close()

    def _run(self) -> None:
        info, look_at = {}, 0.0
        while not self.stopping.is_set():
            if time.monotonic() >= look_at:
                info, look_at = self._look(), time.monotonic() + LOOK_EVERY
            self.stopping.wait(self.ms / 1000)
            with self.lock:
                done, self.results = self.results, []
            if not self.stopping.is_set():
                try:
                    self.report(info, done, "; ".join(x for x in (self.error, self.problem) if x))
                except Exception:  # noqa: BLE001 - one bad report mustn't end the timing
                    pass


if __name__ == "__main__":
    # What this PC's adapters would show: python latency.py
    import perfmon
    names = {a["luid"]: a["alias"] for a in perfmon.adapters()}
    w = Window()

    def report(info, done, error):
        for key, t, rtt, method in done:
            adapter, site = key.rsplit("|", 1)
            w.add(adapter, site, t, rtt, method)
        if error:
            print("error:", error)

    e = Echoes(lambda: plan(names, [(n, h) for n, _, h in SITES]), report).start()
    time.sleep(4)
    e.stop()
    for adapter, a in w.summary(time.time()).items():
        print(f"{adapter}: average {a['avg']} ms, jitter {a['jitter']} ms, {a['lost']} of {a['pings']} lost")
        for site, (avg, jitter, lost, n, method) in a["sites"].items():
            print(f"   {site}: {avg} ms, jitter {jitter} ms, {lost} of {n} lost ({method})")
